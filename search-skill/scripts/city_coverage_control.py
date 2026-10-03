#!/usr/bin/env python3
"""城市级搜索覆盖、收敛、冻结和全量重算控制器。仅使用标准库。

写操作（init/ingest-batch/set-task/set-keyword/register-keyword/set-gap/evaluate/freeze/recalculate）
必须提供 --caller-token，并通过 state_guard 进行令牌校验、flock 排他锁和只读文件保护。
读操作（status）不需要令牌。
"""

from __future__ import annotations
from b2b_qualification import BUSINESS_FITS

from b2b_qualification import seed_eligible, validate_qualification as qualify, QualificationError

from b2b_config import policy_for, check_if_bound, asset_path, profile_for, binding_for, same_binding, bind_profile, ConfigError

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
import copy
import search_worker_control as discovery
import system_search_plan_control as system_search
import keyword_deepening_control as keyword_deepening

from state_guard import StateGuardError, protected_write, verify_caller_token


TASK_STATUSES = {"PLANNED", "IN_PROGRESS", "COMPLETED", "BLOCKED_ALLOWED", "BLOCKED_CRITICAL"}
COMPLETED_TASK_STATUSES = {"COMPLETED", "BLOCKED_ALLOWED"}
CANDIDATE_STATUSES = {"UNVERIFIED", "IN_WORKSET", "FINAL", "FOLLOWUP", "EXCLUDED", "CARRYOVER"}
RESOLVED_CANDIDATE_STATUSES = {"FINAL", "FOLLOWUP", "EXCLUDED"}
KEYWORD_STATUSES = {"PENDING_TEST", "APPROVED", "RESTRICTED", "DISABLED"}
KEYWORD_ORIGINS = {"DISCOVERED", "BASE_AUTHORITY"}
GAP_SEVERITIES = {"BLOCKING", "NON_BLOCKING"}
GAP_STATUSES = {"OPEN", "IN_PROGRESS", "CLOSED", "ACCEPTED_LIMITATION"}
REQUIRED_ROLES = []  # Explicit customer roles come from the bound profile.
ROUND_STATUSES = {"OPEN", "COMPLETED", "EARLY_STOPPED"}
TASK_CLASSES = {"MANDATORY_COVERAGE", "YIELD_EXPANSION", "CONVERGENCE_PROBE"}
VALID_TERMINATION_REASONS = {"BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED", "SOURCE_BLOCKED", "STRATEGY_STOP"}
QUERY_PURPOSES = {
    "BASE_KEYWORD_TRAVERSAL", "GAP_SEARCH", "KEYWORD_TEST",
    "EXPANSION_DISCOVERY", "COMPANY_VERIFICATION", "SYSTEM_SEARCH", "KEYWORD_DEEPENING",
}
BASE_TRAVERSAL_PURPOSE = "BASE_KEYWORD_TRAVERSAL"
DEFAULT_BASE_KEYWORDS_PATH = Path(__file__).resolve().parent.parent / "assets" / "base-keywords.json"


class CoverageError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict:
    if not path.exists():
        raise CoverageError(f"JSON不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoverageError(f"无法读取JSON {path}：{exc}") from exc
    if not isinstance(data, dict):
        raise CoverageError(f"JSON顶层必须为对象：{path}")
    return check_if_bound(data)


def save_json(path: Path, data: dict) -> None:
    data["updated_at"] = now()
    with protected_write(path):
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(6):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if tmp.exists():
                tmp.unlink()


def require_token(args: argparse.Namespace) -> None:
    """校验调用方令牌。所有写命令必须在执行前调用。"""
    verify_caller_token(
        getattr(args, "caller_token", ""),
        state_path=getattr(args, "state", None) or getattr(args, "output", None),
    )


def canonical_path(path: Path) -> str:
    return str(path.resolve())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_signature(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def sha256_json(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def unique_strings(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def normalize_task(raw: dict) -> dict:
    task_id = str(raw.get("task_id", "")).strip()
    if not task_id:
        raise CoverageError("搜索任务缺少 task_id")
    status = str(raw.get("status", raw.get("完成状态", "PLANNED"))).upper()
    if status not in TASK_STATUSES:
        status = "PLANNED"
    task_class = str(raw.get("task_class", "MANDATORY_COVERAGE")).upper()
    if task_class not in TASK_CLASSES:
        raise CoverageError(f"搜索任务 {task_id} task_class 非法：{task_class}")
    return {
        "task_id": task_id,
        "district": str(raw.get("district", raw.get("区县", ""))).strip(),
        "role": str(raw.get("role", raw.get("目标角色", ""))).strip(),
        "industry": str(raw.get("industry", raw.get("产业", ""))).strip(),
        "keyword_family": str(raw.get("keyword_family", raw.get("关键词族", ""))).strip(),
        "source_type": str(raw.get("source_type", raw.get("来源类型", raw.get("source", "")))).strip(),
        "required": bool(raw.get("required", raw.get("是否必需", True))),
        "task_class": task_class,
        "minimum_results_examined": int(raw.get("minimum_results_examined", 10) or 0),
        "status": status,
        "query_ids": unique_strings(raw.get("query_ids", [])),
        "completion_reason": str(raw.get("completion_reason", raw.get("完成或阻断原因", ""))).strip(),
    }


def normalize_keyword(raw: dict) -> dict:
    keyword_id = str(raw.get("keyword_id", "")).strip()
    if not keyword_id:
        raise CoverageError("扩展词缺少 keyword_id")
    status = str(raw.get("status", "PENDING_TEST")).upper()
    if status not in KEYWORD_STATUSES:
        raise CoverageError(f"扩展词 {keyword_id} 状态非法：{status}")
    keyword_origin = str(raw.get("keyword_origin", "DISCOVERED")).strip().upper()
    if keyword_origin not in KEYWORD_ORIGINS:
        raise CoverageError(f"扩展词 {keyword_id} keyword_origin 非法：{keyword_origin}")
    return {
        "keyword_id": keyword_id,
        "keyword": str(raw.get("keyword", "")).strip(),
        "keyword_origin": keyword_origin,
        "status": status,
        "test_query_ids": unique_strings(raw.get("test_query_ids", [])),
        "replay_task_ids": unique_strings(raw.get("replay_task_ids", [])),
        "replay_completed": bool(raw.get("replay_completed", False)),
        "test_history": list(raw.get("test_history", [])) if isinstance(raw.get("test_history", []), list) else [],
        "evaluated_test_query_ids": unique_strings(raw.get("evaluated_test_query_ids", [])),
    }


def normalize_base_keyword(raw: dict) -> dict:
    keyword_id = str(raw.get("keyword_id", "")).strip()
    keyword = str(raw.get("keyword", raw.get("关键词", ""))).strip()
    if not keyword_id or not keyword:
        raise CoverageError("基础关键词必须同时提供 keyword_id 和 keyword")
    match_terms = unique_strings(raw.get("match_terms", raw.get("approved_match_terms", [])))
    if not match_terms:
        match_terms = [keyword]
    return {
        "keyword_id": keyword_id,
        "keyword": keyword,
        "keyword_family": str(raw.get("keyword_family", raw.get("关键词族", ""))).strip(),
        "direct_search": bool(raw.get("direct_search", True)),
        "requires_combination": bool(raw.get("requires_combination", False)),
        "match_terms": match_terms,
    }


def load_authoritative_base_keywords(path: Path | None = None) -> dict:
    """读取并校验包内基础关键词权威清单。"""
    if path is None:
        raise CoverageError("必须指定本次任务绑定的词库路径，不能使用公共示例作为默认策略")
    authority_path = path.resolve()
    document = load_json(authority_path)
    raw_items = document.get("base_keywords")
    if not isinstance(raw_items, list) or not raw_items:
        raise CoverageError(f"基础关键词权威清单缺少非空 base_keywords：{authority_path}")
    items = [normalize_base_keyword(item) for item in raw_items]
    declared_total = document.get("total_keywords")
    if declared_total != len(items):
        raise CoverageError(
            f"基础关键词权威清单计数不一致：声明={declared_total}，实际={len(items)}"
        )
    keyword_ids = [item["keyword_id"] for item in items]
    if len(keyword_ids) != len(set(keyword_ids)):
        raise CoverageError("基础关键词权威清单存在重复 keyword_id")
    return {
        "path": authority_path,
        "sha256": sha256_file(authority_path),
        "schema_version": str(document.get("schema_version", "")).strip(),
        "total_keywords": len(items),
        "base_keywords": items,
        "deepening_policy": copy.deepcopy(document.get("deepening_policy", {})),
    }


def base_keyword_registration_status(
    registered_items: list[dict],
    authority: dict,
) -> dict:
    """比较城市状态中的注册表与包内权威清单，返回可持久化的完整性状态。"""
    registered = [normalize_base_keyword(item) for item in registered_items]
    expected = authority["base_keywords"]
    registered_ids = [item["keyword_id"] for item in registered]
    expected_ids = [item["keyword_id"] for item in expected]
    duplicate_ids = sorted({
        keyword_id for keyword_id in registered_ids if registered_ids.count(keyword_id) > 1
    })
    registered_by_id = {item["keyword_id"]: item for item in registered}
    expected_by_id = {item["keyword_id"]: item for item in expected}
    missing_ids = [keyword_id for keyword_id in expected_ids if keyword_id not in registered_by_id]
    unexpected_ids = [keyword_id for keyword_id in registered_ids if keyword_id not in expected_by_id]
    mismatched_ids = [
        keyword_id for keyword_id in expected_ids
        if keyword_id in registered_by_id
        and registered_by_id[keyword_id] != expected_by_id[keyword_id]
    ]
    order_matches = registered_ids == expected_ids
    passed = not duplicate_ids and not missing_ids and not unexpected_ids and not mismatched_ids and order_matches
    return {
        "status": "COMPLETE" if passed else "INCOMPLETE",
        "passed": passed,
        "authority_path": canonical_path(authority["path"]),
        "authority_sha256": authority["sha256"],
        "authority_schema_version": authority["schema_version"],
        "expected_count": len(expected),
        "registered_count": len(registered),
        "duplicate_keyword_ids": duplicate_ids,
        "missing_keyword_ids": missing_ids,
        "unexpected_keyword_ids": unexpected_ids,
        "mismatched_keyword_ids": mismatched_ids,
        "order_matches": order_matches,
    }


def normalized_query_text(value: str) -> str:
    """Normalize harmless spacing/case differences without inventing synonyms."""
    return "".join(str(value or "").casefold().split())


def base_keyword_match_term(query: dict, base_keyword: dict) -> str:
    query_text = normalized_query_text(query.get("query_text", ""))
    for term in base_keyword.get("match_terms", [base_keyword.get("keyword", "")]):
        if normalized_query_text(term) in query_text:
            return term
    return ""


def qualifies_for_base_traversal(
    query: dict, base_keyword: dict, allow_source_blocked_record: bool = False
) -> bool:
    if query.get("query_purpose") != BASE_TRAVERSAL_PURPOSE:
        return False
    if not query.get("query_id") or not str(query.get("query_text", "")).strip():
        return False
    if query.get("termination_reason") not in VALID_TERMINATION_REASONS:
        return False
    match_term = base_keyword_match_term(query, base_keyword)
    if not match_term:
        return False
    # BASE_KEYWORD_TRAVERSAL 使用城市＋关键词宽查询；组合要求用于后续定向查询。
    # SOURCE_BLOCKED 可以作为结构合法的执行记录落盘，但默认不计入遍历完成。
    if allow_source_blocked_record:
        return True  # preserve honest incomplete/blocked execution without counting it complete
    return not discovery.execution_problem(query)


def normalize_gap(raw: dict) -> dict:
    gap_id = str(raw.get("gap_id", "")).strip()
    if not gap_id:
        raise CoverageError("覆盖缺口缺少 gap_id")
    severity = str(raw.get("severity", "BLOCKING")).upper()
    status = str(raw.get("status", "OPEN")).upper()
    if severity not in GAP_SEVERITIES or status not in GAP_STATUSES:
        raise CoverageError(f"覆盖缺口 {gap_id} 的等级或状态非法")
    return {
        "gap_id": gap_id,
        "dimension": str(raw.get("dimension", "")).strip(),
        "value": str(raw.get("value", "")).strip(),
        "severity": severity,
        "status": status,
        "resolution_task_ids": unique_strings(raw.get("resolution_task_ids", [])),
    }


def ensure_minimum_coverage_tasks(space: dict) -> list[dict]:
    """Generate a compact coverage set instead of a full Cartesian product."""
    tasks = list(space.get("tasks", []))
    signatures = {
        tuple(t.get(k, "") for k in DIMENSION_FIELDS) for t in tasks
    }
    used_ids = {t["task_id"] for t in tasks}

    def add(**dimensions):
        signature = tuple(dimensions.get(k, "") for k in DIMENSION_FIELDS)
        if signature in signatures:
            return
        base_id = "AUTO-SPACE-" + stable_signature(signature)
        task_id = base_id
        suffix = 2
        while task_id in used_ids:
            task_id = f"{base_id}-{suffix}"
            suffix += 1
        tasks.append(normalize_task({
            "task_id": task_id, "task_class": "MANDATORY_COVERAGE",
            "required": True, "minimum_results_examined": 10, **dimensions,
        }))
        signatures.add(signature)
        used_ids.add(task_id)

    sources = space.get("source_types", []) or ["公开网页"]
    for district in space.get("districts", []):
        if not any(t.get("district") == district for t in tasks):
            add(district=district, source_type=sources[0])
    for role in space.get("roles", []):
        if not any(t.get("role") == role for t in tasks):
            add(role=role, source_type=sources[0])
    for family in space.get("keyword_families", []):
        if not any(t.get("keyword_family") == family for t in tasks):
            add(keyword_family=family, source_type=sources[0])
    for source_type in sources:
        if not any(t.get("source_type") == source_type for t in tasks):
            add(source_type=source_type)
    for industry in space.get("industries", []):
        existing_sources = {t.get("source_type") for t in tasks if t.get("industry") == industry and t.get("source_type")}
        needed_sources = list(sources[:2])
        if len(needed_sources) == 1:
            needed_sources.append("公开名单/项目来源")
        for source_type in needed_sources:
            if source_type not in existing_sources:
                add(industry=industry, source_type=source_type)
    return tasks


def validate_space(space: dict, authority: dict | None = None) -> dict:
    city = str(space.get("city", "")).strip()
    if not city:
        raise CoverageError("搜索空间缺少 city")
    if authority is None:
        raise CoverageError("搜索空间必须使用显式B2B配置词库")
    supplied_base_keywords = space.get("base_keywords")
    if supplied_base_keywords is None:
        normalized_base_keywords = authority["base_keywords"]
    elif not isinstance(supplied_base_keywords, list):
        raise CoverageError("搜索空间 base_keywords 必须为数组")
    else:
        normalized_base_keywords = [normalize_base_keyword(item) for item in supplied_base_keywords]
    registration = base_keyword_registration_status(normalized_base_keywords, authority)
    if not registration["passed"]:
        raise CoverageError(
            "搜索空间基础关键词与权威清单不一致："
            f"缺失={registration['missing_keyword_ids']}，"
            f"多余={registration['unexpected_keyword_ids']}，"
            f"内容不符={registration['mismatched_keyword_ids']}，"
            f"重复ID={registration['duplicate_keyword_ids']}，"
            f"顺序一致={registration['order_matches']}"
        )
    normalized = {
        "city": city,
        "rules_version": str(space.get("rules_version", "1.0")).strip() or "1.0",
        "districts": unique_strings(space.get("districts", [])),
        "roles": unique_strings(space.get("roles", REQUIRED_ROLES)) or list(REQUIRED_ROLES),
        "industries": unique_strings(space.get("industries", [])),
        "keyword_families": unique_strings(space.get("keyword_families", [])),
        "source_types": unique_strings(space.get("source_types", [])),
        "tasks": [normalize_task(item) for item in space.get("tasks", [])],
        "base_keywords": list(authority["base_keywords"]),
        "keywords": [normalize_keyword(item) for item in space.get("keywords", [])],
        "gaps": [normalize_gap(item) for item in space.get("gaps", [])],
    }
    normalized["tasks"] = ensure_minimum_coverage_tasks(normalized)
    for key in ("districts", "roles", "industries", "keyword_families", "source_types", "tasks", "base_keywords"):
        if not normalized[key]:
            raise CoverageError(f"搜索空间 {key} 不能为空")
    task_ids = [item["task_id"] for item in normalized["tasks"]]
    if len(task_ids) != len(set(task_ids)):
        raise CoverageError("搜索空间存在重复 task_id")
    if any(item["keyword_origin"] == "DISCOVERED" for item in normalized["keywords"]):
        raise CoverageError("初始化基础检索空间时禁止预登记DISCOVERED新词")
    return normalized


def find_by_id(items: list[dict], key: str, value: str) -> dict | None:
    for item in items:
        if item.get(key) == value:
            return item
    return None


def metric(
    name: str,
    passed_ids: list[str],
    all_ids: list[str],
    threshold: float = 1.0,
    allow_empty: bool = False,
) -> dict:
    denominator = len(all_ids)
    numerator = len(passed_ids)
    result = None if denominator == 0 else numerator / denominator
    passed = allow_empty if denominator == 0 else result is not None and result >= threshold
    failed = [item_id for item_id in all_ids if item_id not in set(passed_ids)]
    return {
        "name": name,
        "numerator": numerator,
        "denominator": denominator,
        "result": result,
        "threshold": threshold,
        "passed": passed,
        "failed_item_ids": failed,
    }


def expansion_low_yield_reason(round_item: dict) -> str:
    """返回扩展轮次可审计的低增量原因；空字符串表示不计入收敛。"""
    queries_executed = int(round_item.get("queries_executed", 0) or 0)
    normal_queries = int(round_item.get("normal_termination_query_count", 0) or 0)
    blocked_queries = int(round_item.get("source_blocked_query_count", 0) or 0)
    unresolved = int(round_item.get("unresolved_candidate_count", 0) or 0)
    common = (
        round_item.get("round_status") in {"COMPLETED", "EARLY_STOPPED"}
        and round_item.get("budget_complete", False)
        and queries_executed > 0
        and normal_queries == queries_executed
        and blocked_queries == 0
        and unresolved == 0
        and round_item.get("new_types_added", 0) == 0
        and round_item.get("gaps_closed", 0) == 0
    )
    if not common:
        return ""
    unique_count = int(round_item.get("unique_candidates_added", 0) or 0)
    duplicate_count = int(round_item.get("duplicate_count", 0) or 0)
    if unique_count == 0 and duplicate_count > 0:
        return "HIGH_DUPLICATION"
    if unique_count == 0 and duplicate_count == 0:
        return "NO_CANDIDATES"
    if (
        unique_count > 0
        and int(round_item.get("new_pool_candidates_added", 0) or 0) == 0
        and int(round_item.get("excluded_candidate_count", 0) or 0) == unique_count
    ):
        return "ALL_NEW_CANDIDATES_EXCLUDED"
    return ""


DIMENSION_FIELDS = ("district", "role", "industry", "keyword_family", "source_type")


def dimension_matches(field, actual, target):
    # Coverage uses canonical family IDs from the bound profile, without a default sector dictionary.
    return bool(actual and target and actual == target)


def query_supports_task(query: dict, task: dict) -> bool:
    if discovery.execution_problem(query) or query.get("task_id") != task.get("task_id"):
        return False
    if task.get("task_class") == "MANDATORY_COVERAGE" and query.get("query_purpose") not in {"GAP_SEARCH", "SYSTEM_SEARCH"}:
        return False
    if query.get("results_examined", 0) < task.get("minimum_results_examined", 10) and query.get("termination_reason") != "RESULTS_EXHAUSTED":
        return False
    for field in DIMENSION_FIELDS:
        target = task.get(field)
        if not target:
            continue
        actual = query.get(field)
        if not dimension_matches(field, actual, target):
            return False
    return True


def round_kind(state: dict, round_item: dict) -> str:
    """从底层查询识别轮次类型，兼容尚无round_kind字段的历史状态。"""
    stored = str(round_item.get("round_kind", "")).strip().upper()
    queries = [
        q for q in state.get("queries", [])
        if q.get("round_id") == round_item.get("round_id")
    ]
    purposes = {
        str(q.get("query_purpose", "")).strip().upper()
        for q in queries if str(q.get("query_purpose", "")).strip()
    }
    if len(purposes) > 1:
        return "MIXED"
    inferred = next(iter(purposes), "")
    if stored and inferred and stored != inferred:
        return "MIXED"
    if stored:
        return stored
    if inferred:
        return inferred
    if str(round_item.get("discovery_mode", "")).strip() == "expansion":
        return "EXPANSION_DISCOVERY"
    return "UNKNOWN"


def task_completion_audit(state: dict, task: dict) -> dict:
    by_id = {q["query_id"]: q for q in state.get("queries", [])}
    ids = [qid for qid in task.get("query_ids", []) if qid in by_id and query_supports_task(by_id[qid], task)]
    return {"qualified": bool(ids), "qualified_query_ids": ids,
            "status": "VERIFIED" if ids else "PENDING_EVIDENCE",
            "reason": "" if ids else "无有效关联查询、缺执行依据或维度依据不匹配"}


def sync_coverage_followups(state: dict, result: dict) -> None:
    current = {g["gap_id"]: g for g in result.get("coverage_followups", [])}
    for gap in state.setdefault("gaps", []):
        if gap.get("origin") == "COVERAGE_AUDIT":
            if gap["gap_id"] in current:
                gap.update(current.pop(gap["gap_id"]))
            else:
                gap["status"] = "CLOSED"
    state["gaps"].extend(current.values())


def _next_coverage_task_id(state: dict) -> str:
    """Return a collision-free deterministic task id for generated gap work."""
    used = {str(item.get("task_id", "")) for item in state.get("tasks", [])}
    number = 1
    while f"AUTO-COV-{number:04d}" in used:
        number += 1
    return f"AUTO-COV-{number:04d}"


def refresh_materialized_gaps(state: dict) -> list[str]:
    """Close generated coverage gaps once all linked evidence-backed tasks pass."""
    closed = []
    tasks = {item["task_id"]: item for item in state.get("tasks", [])}
    for gap in state.get("gaps", []):
        resolution_ids = unique_strings(gap.get("resolution_task_ids", []))
        if not resolution_ids or gap.get("status") == "CLOSED":
            continue
        if all(
            task_id in tasks
            and tasks[task_id].get("status") in COMPLETED_TASK_STATUSES
            and task_completion_audit(state, tasks[task_id])["qualified"]
            for task_id in resolution_ids
        ):
            gap["status"] = "CLOSED"
            closed.append(gap["gap_id"])
    return closed


def materialize_blocking_gap_tasks(state: dict) -> dict:
    """Turn calculated coverage follow-ups into executable mandatory tasks.

    The calculator may discover a missing dimension that was absent from the
    original city-space task list.  Previously this produced a blocking gap
    without any legal way to close it.  This function creates the minimum
    evidence-backed task set and links it to the gap.  Industry gaps receive
    two source routes because industry coverage requires two independent
    sources; other dimensions receive one task.
    """
    calculation = calculate(state)
    sync_coverage_followups(state, calculation)
    created = []
    source_types = list(state.get("search_space", {}).get("source_types", [])) or ["公开网页"]
    for followup in calculation.get("coverage_followups", []):
        gap = find_by_id(state.get("gaps", []), "gap_id", followup["gap_id"])
        if gap is None or gap.get("status") == "CLOSED":
            continue
        existing = unique_strings(gap.get("resolution_task_ids", []))
        if existing:
            continue
        dimension = followup.get("dimension", "")
        value = followup.get("value", "")
        routes = source_types[:2] if dimension == "industry" else source_types[:1]
        if dimension == "industry" and len(routes) == 1:
            routes.append("公开名单/项目来源")
        task_ids = []
        for source_type in routes:
            task_id = _next_coverage_task_id(state)
            raw = {
                "task_id": task_id,
                "task_class": "MANDATORY_COVERAGE",
                "required": True,
                "source_type": source_type,
                "minimum_results_examined": 10,
                "status": "PLANNED",
                "query_ids": [],
                "completion_reason": "",
            }
            if dimension in DIMENSION_FIELDS:
                raw[dimension] = value
            task = normalize_task(raw)
            state.setdefault("tasks", []).append(task)
            task_ids.append(task_id)
            created.append(task_id)
        gap["resolution_task_ids"] = task_ids
        gap["related_task_ids"] = task_ids
    return {"created_task_ids": created, "created_count": len(created)}


def calculate(state: dict) -> dict:
    policy = policy_for(state)
    min_queries = policy["system_search"]["min_queries"]
    zero_rounds = policy["system_search"]["zero_candidate_rounds"]
    expansion_budget = policy["expansion"]["query_budget"]
    duplicate_streak = policy["expansion"]["duplicate_streak"]
    space = state["search_space"]
    authority = load_authoritative_base_keywords(asset_path(state, "keywords"))
    registration = base_keyword_registration_status(
        state.get("base_keywords", []), authority
    )
    base_keyword_registration_complete = registration["passed"]
    tasks = state.get("tasks", [])
    gaps = [g for g in state.get("gaps", []) if g.get("origin") != "COVERAGE_AUDIT"]
    task_audits = {t["task_id"]: task_completion_audit(state, t) for t in tasks}
    def completed(task):
        return task.get("status") in COMPLETED_TASK_STATUSES and task_audits[task["task_id"]]["qualified"]
    required_tasks = [item for item in tasks if item.get("required", True)]
    completed_ids = [item["task_id"] for item in required_tasks if completed(item)]
    metrics = [metric("任务完成率", completed_ids, [item["task_id"] for item in required_tasks])]

    def dimension_passed(values: list[str], field: str) -> tuple[list[str], list[str]]:
        passed = []
        for value in values:
            related = [item for item in required_tasks if item.get(field) == value]
            if related and all(completed(item) for item in related):
                blocking = [
                    gap for gap in gaps
                    if gap.get("severity") == "BLOCKING"
                    and gap.get("status") in {"OPEN", "IN_PROGRESS"}
                    and gap.get("value") == value
                ]
                if not blocking:
                    passed.append(value)
        return passed, values

    for name, values, field in (
        ("区县覆盖率", space["districts"], "district"),
        ("角色覆盖率", space["roles"], "role"),
        ("来源覆盖率", space["source_types"], "source_type"),
        ("关键词族覆盖率", space["keyword_families"], "keyword_family"),
    ):
        passed, all_values = dimension_passed(values, field)
        metrics.append(metric(name, passed, all_values))

    industry_passed = []
    for industry in space["industries"]:
        related = [item for item in required_tasks if item.get("industry") == industry]
        completed_tasks = [item for item in related if completed(item)]
        independent_sources = {item.get("source_type") for item in completed_tasks if item.get("source_type")}
        blocking = [
            gap for gap in gaps
            if gap.get("severity") == "BLOCKING"
            and gap.get("status") in {"OPEN", "IN_PROGRESS"}
            and gap.get("value") == industry
        ]
        if related and len(completed_tasks) == len(related) and len(independent_sources) >= 2 and not blocking:
            industry_passed.append(industry)
    metrics.append(metric("产业覆盖率", industry_passed, space["industries"]))

    coverage_followups = []
    fields = {"区县覆盖率": "district", "角色覆盖率": "role", "来源覆盖率": "source_type", "关键词族覆盖率": "keyword_family", "产业覆盖率": "industry"}
    for item in metrics:
        if item["name"] not in fields:
            continue
        field = fields[item["name"]]
        for value in item["failed_item_ids"]:
            related = [t["task_id"] for t in required_tasks if t.get(field) == value]
            related_task_ids = related
            coverage_followups.append({"gap_id": "P0-COV-" + stable_signature([field, value]),
                "origin": "COVERAGE_AUDIT", "severity": "BLOCKING", "status": "OPEN",
                "dimension": field, "value": value, "related_task_ids": related_task_ids,
                "resolution_task_ids": related_task_ids,
                "reason": "覆盖指标未通过；须补有效查询及维度依据，不能只改任务标签"})
    gaps = gaps + coverage_followups
    candidates = state.get("candidates", [])
    from batch_context_control import candidate_source_problem
    query_map = {q["query_id"]: q for q in state.get("queries", [])}
    source_audits = {c["candidate_id"]: candidate_source_problem(c, state.get("discovery_records", {}), query_map) for c in candidates}
    metrics.append(metric("候选来源可追溯率", [cid for cid, problem in source_audits.items() if not problem],
                          [c["candidate_id"] for c in candidates], allow_empty=True))
    ledger = state.get("discovery_records", {})
    metrics.append(metric("发现线索处置完整率", [rid for rid, row in ledger.items() if row.get("disposition") != "PENDING"], list(ledger), allow_empty=True))
    unresolved_carryover_ids = [
        item["candidate_id"] for item in candidates if item.get("status") == "CARRYOVER"
    ]
    resolved_ids = [item["candidate_id"] for item in candidates if item.get("status") in RESOLVED_CANDIDATE_STATUSES]
    metrics.append(metric(
        "候选处理完整率",
        resolved_ids,
        [item["candidate_id"] for item in candidates],
        allow_empty=True,
    ))
    expansion_candidates = [
        item for item in candidates if item.get("discovery_method") == "YIELD_EXPANSION"
    ]
    expansion_traceable_ids = [
        item["candidate_id"] for item in expansion_candidates
        if all(item.get(field) for field in (
            "round_id", "expansion_task_id", "seed_id", "seed_name",
            "feature_chain_id", "feature_chain", "feature_values",
            "similarity_dimensions", "similarity_basis", "coverage_gap", "source_route",
        ))
    ]
    expansion_traceability_metric = metric(
        "扩展来源追溯完整率",
        expansion_traceable_ids,
        [item["candidate_id"] for item in expansion_candidates],
        allow_empty=True,
    )
    metrics.append(expansion_traceability_metric)

    approved_pending = [
        item["keyword_id"] for item in state.get("keywords", [])
        if item.get("status") == "APPROVED" and not item.get("replay_completed")
    ]
    pending_discovered_keyword_ids = [
        item["keyword_id"] for item in state.get("keywords", [])
        if item.get("keyword_origin", "DISCOVERED") == "DISCOVERED"
        and item.get("status") == "PENDING_TEST"
    ]
    blocking_gaps = [
        item["gap_id"] for item in gaps
        if item.get("severity") == "BLOCKING" and item.get("status") in {"OPEN", "IN_PROGRESS"}
    ]
    base_coverage_complete = all(item["passed"] for item in metrics) and not blocking_gaps
    coverage_complete = (
        all(item["passed"] for item in metrics)
        and not pending_discovered_keyword_ids and not approved_pending and not blocking_gaps
    )

    required_base_keyword_ids = [
        item["keyword_id"] for item in state.get("base_keywords", []) if item.get("direct_search", True)
    ]
    base_keyword_by_id = {item["keyword_id"]: item for item in state.get("base_keywords", [])}
    completed_base_keyword_ids = []
    base_keyword_query_ids = {}
    for query in state.get("queries", []):
        keyword_id = query.get("keyword_id")
        base_keyword = base_keyword_by_id.get(keyword_id)
        if not base_keyword or keyword_id not in required_base_keyword_ids:
            continue
        if not qualifies_for_base_traversal(query, base_keyword):
            continue
        completed_base_keyword_ids.append(keyword_id)
        base_keyword_query_ids.setdefault(keyword_id, []).append(query["query_id"])
    completed_base_keyword_ids = list(dict.fromkeys(completed_base_keyword_ids))
    base_keyword_metric = metric(
        "基础关键词遍历率", completed_base_keyword_ids, required_base_keyword_ids
    )
    base_keyword_traversal_complete = (
        base_keyword_registration_complete and base_keyword_metric["passed"]
    )
    deepening = keyword_deepening.calculate(state, load_authoritative_base_keywords(asset_path(state, "keywords")))
    keyword_deepening_complete = deepening["keyword_deepening_complete"]

    rounds = [item for item in state.get("rounds", []) if item.get("round_status") in {"COMPLETED", "EARLY_STOPPED"}]
    independent_low = []
    previous_signature = None
    for item in rounds:
        signature = item.get("strategy_signature")
        low = (
            item.get("budget_complete", False)
            and item.get("final_per_query", 0) < 0.02
            and item.get("final_per_verified_candidate", 0) < 0.05
            and item.get("duplicate_rate", 0) >= 0.80
            and item.get("new_types_added", 0) == 0
            and item.get("gaps_closed", 0) == 0
        )
        if low and signature and signature != previous_signature:
            independent_low.append(item["round_id"])
        else:
            independent_low = []
        previous_signature = signature
    expansion_low = []
    expansion_low_reasons = {}
    previous_expansion_signature = None
    for item in rounds:
        if item.get("discovery_mode") != "expansion":
            continue
        signature = item.get("strategy_signature")
        low_reason = expansion_low_yield_reason(item)
        if low_reason and signature and signature != previous_expansion_signature:
            expansion_low.append(item["round_id"])
            expansion_low_reasons[item["round_id"]] = low_reason
        else:
            expansion_low = []
            expansion_low_reasons = {}
        previous_expansion_signature = signature

    trailing_zero_candidate = []
    system_round_audits = {}
    # 只有真实SYSTEM_SEARCH查询构成的轮次进入受控零新增轮次饱和审计。
    # BASE_KEYWORD_TRAVERSAL、GAP_SEARCH及其他search模式批次保留轮次统计，
    # 但不得产生MISSING_VALIDATED_PLAN等SYSTEM_SEARCH错误。
    system_rounds = [item for item in rounds if round_kind(state, item) == "SYSTEM_SEARCH"]
    for item in system_rounds:
        signature = item.get("strategy_signature")
        audit = system_search.round_audit(state, item)
        system_round_audits[item["round_id"]] = {k: v for k, v in audit.items() if k != "queries"}
        is_complete_system_round = (
            item.get("round_status") in {"COMPLETED", "EARLY_STOPPED"}
            and item.get("budget_complete", False)
            and int(item.get("queries_executed", 0)) >= min_queries
            and int(item.get("new_pool_candidates_added", item.get("unique_candidates_added", 0))) == 0
            and item.get("all_discovery_records_disposed", False)
            and bool(signature)
            and audit["eligible"]
            and not item.get("unresolved_candidate_count", 0)
        )
        if is_complete_system_round and any(not system_search.independent_round(
                audit["queries"], system_search.round_audit(state, previous)["queries"])
                for previous in trailing_zero_candidate[-(zero_rounds-1):]):
            is_complete_system_round = False
            system_round_audits[item["round_id"]]["reasons"].append("OVERLAPPING_DISCOVERY_ROUTE")
            system_round_audits[item["round_id"]]["eligible"] = False
        if is_complete_system_round:
            trailing_zero_candidate.append(item)
        else:
            trailing_zero_candidate = []
    latest_zero_rounds = trailing_zero_candidate[-zero_rounds:]
    signatures = [item.get("strategy_signature") for item in latest_zero_rounds]
    zero_candidate_round_ids = (
        [item["round_id"] for item in latest_zero_rounds]
        if len(latest_zero_rounds) == zero_rounds
        and len(set(signatures)) == zero_rounds else []
    )
    # 关键词清单提供查询建议，不构成发现或停止的前置完成条件。
    # A later coverage or expansion round that adds a new company invalidates the
    # preceding discovery convergence; resume candidate discovery before stopping.
    if zero_candidate_round_ids:
        last_zero_index = next((i for i, row in enumerate(rounds)
                                if row.get("round_id") == zero_candidate_round_ids[-1]), -1)
        if any(int(row.get("new_pool_candidates_added", 0) or 0) > 0
               for row in rounds[last_zero_index + 1:]):
            zero_candidate_round_ids = []

    expansion_allowed = any(seed_eligible(c) for c in candidates)
    all_batches_closed = bool(state.get("batches")) and all(item.get("batch_status") == "CLOSED" for item in state["batches"])
    next_task_ids = [item["task_id"] for item in required_tasks if not completed(item)]
    base_search_saturated = len(zero_candidate_round_ids) == zero_rounds
    expansion_started = any(item.get("discovery_mode") == "expansion" for item in rounds)
    expansion_traceability_complete = expansion_traceability_metric["passed"]
    # One completed expansion cycle with three distinct, disposed duplicate
    # companies is sufficient; capped zero-result cycles also terminate.
    expansion_rounds = [r for r in rounds if r.get("discovery_mode") == "expansion"]
    last_expansion = expansion_rounds[-1] if expansion_rounds else None
    expansion_stopped = bool(last_expansion and last_expansion.get("budget_complete")
        and not last_expansion.get("source_blocked_query_count")
        and not last_expansion.get("unresolved_candidate_count")
        and (len(last_expansion.get("expansion_no_new_streak_keys", [])) >= duplicate_streak
             or (last_expansion.get("queries_executed", 0) >= expansion_budget
                 and last_expansion.get("new_pool_candidates_added", 0) == 0)))
    if expansion_stopped:
        last_index = next(i for i, row in enumerate(rounds) if row["round_id"] == last_expansion["round_id"])
        expansion_stopped = not any(int(r.get("new_pool_candidates_added", 0) or 0) > 0
                                    for r in rounds[last_index + 1:])
    expansion_complete = (expansion_stopped and not pending_discovered_keyword_ids
                          and not approved_pending and not blocking_gaps
                          and expansion_traceability_complete)
    convergence_complete = expansion_complete
    pending_work = (
        bool(next_task_ids) or bool(pending_discovered_keyword_ids)
        or bool(approved_pending) or bool(blocking_gaps)
        or bool(unresolved_carryover_ids)
        or any(row.get("disposition") == "PENDING" for row in state.get("discovery_records", {}).values())
        or any(c.get("status") in {"UNVERIFIED", "IN_WORKSET", "CARRYOVER"} for c in candidates)
    )
    paired_search_round_ids = {r.get("paired_search_round_id") for r in rounds
                               if r.get("discovery_mode") == "expansion" and r.get("round_status") in {"COMPLETED", "EARLY_STOPPED"}}
    latest_search_round = next((r for r in reversed(rounds)
                                if r.get("discovery_mode") == "search"
                                and r.get("round_kind") in {"BASE_KEYWORD_TRAVERSAL", "KEYWORD_DEEPENING", "SYSTEM_SEARCH"}
                                and r.get("round_status") in {"COMPLETED", "EARLY_STOPPED"}), None)
    pair_target_round_id = (latest_search_round.get("round_id") if latest_search_round
                            and latest_search_round.get("round_id") not in paired_search_round_ids else None)
    has_seed = expansion_allowed
    # 没有可用种子且各独立发现轮次已经收敛时，不制造扩展死锁。
    expansion_complete = expansion_complete or (base_search_saturated and not has_seed)
    convergence_complete = expansion_complete
    stop_candidate = (
        coverage_complete and base_search_saturated and expansion_complete
        and not pending_work and all_batches_closed and not (pair_target_round_id and has_seed and not expansion_stopped)
    )
    last_round = rounds[-1] if rounds else {}
    last_mode = last_round.get("discovery_mode")
    active_keyword_ids = state.get("active_keyword_ids", [])
    active_deepening_ids = [keyword_id for keyword_id in active_keyword_ids
                            if keyword_id in deepening["pending_keyword_ids"]]
    open_round = next((r for r in reversed(state.get("rounds", []))
                       if r.get("round_status") == "OPEN"), None)
    # 一批基础检索完成后优先插入扩展；扩展后先处理词测试与回流。
    if stop_candidate:
        next_strategy, strategy_budget, priority, reason = None, 0, "STOP", "覆盖、发现收敛、单轮扩展截停与待办状态全部通过"
    elif open_round:
        next_strategy = {"BASE_KEYWORD_TRAVERSAL": "BASE_KEYWORD_TRAVERSAL",
                         "KEYWORD_DEEPENING": "KEYWORD_DEEPENING",
                         "SYSTEM_SEARCH": "SYSTEM_SEARCH",
                         "EXPANSION_DISCOVERY": "YIELD_EXPANSION"}.get(open_round.get("round_kind"), "MANDATORY_COVERAGE")
        strategy_budget, priority, reason = int(open_round.get("budget", 0) or 20), "HIGH", "当前发现轮次未结束，先完成该轮剩余查询与候选处置"
    elif last_mode == "expansion" and pending_discovered_keyword_ids:
        next_strategy, strategy_budget, priority, reason = "TEST_DISCOVERED_KEYWORDS", 10, "HIGH", "先测试本批扩展发现的新词"
    elif last_mode == "expansion" and approved_pending:
        next_strategy, strategy_budget, priority, reason = "REPLAY_APPROVED_KEYWORDS", 10, "HIGH", "已批准新词在下一基础批次回流"
    elif pair_target_round_id and has_seed and not expansion_stopped:
        next_strategy, strategy_budget, priority, reason = "YIELD_EXPANSION", expansion_budget, "HIGH", f"本搜索轮次后执行有界扩展；{duplicate_streak}家不同主体无新增即可截停"
    elif pending_discovered_keyword_ids:
        next_strategy, strategy_budget, priority, reason = "TEST_DISCOVERED_KEYWORDS", 10, "HIGH", "先测试本批扩展发现的新词"
    elif approved_pending:
        next_strategy, strategy_budget, priority, reason = "REPLAY_APPROVED_KEYWORDS", 10, "HIGH", "已批准新词在下一基础批次回流"
    elif not base_search_saturated:
        next_strategy, strategy_budget, priority, reason = "SYSTEM_SEARCH", policy["system_search"]["budget"], "HIGH", "优先沿具体线索和高产路径发现候选；低增量后进行独立探查"
    elif next_task_ids or blocking_gaps:
        next_strategy, strategy_budget, priority, reason = "MANDATORY_COVERAGE", max(min_queries, len(next_task_ids)), "HIGH", "发现增量收敛后补查覆盖缺口"
    elif not expansion_complete:
        next_strategy, strategy_budget, priority, reason = "YIELD_EXPANSION", expansion_budget, "MEDIUM", f"继续扩展至{duplicate_streak}家不同主体无新增或完成{expansion_budget}条查询"
    else:
        next_strategy, strategy_budget, priority, reason = None, 0, "STOP", "基础检索与同类扩展均已收敛，可以冻结并交付"
    return {
        "through_batch": state["batches"][-1]["batch_id"] if state.get("batches") else None,
        "metrics": metrics,
        "approved_keyword_pending_ids": approved_pending,
        "pending_discovered_keyword_ids": pending_discovered_keyword_ids,
        "blocking_gap_ids": blocking_gaps,
        "unresolved_carryover_ids": unresolved_carryover_ids,
        "independent_low_output_round_ids": independent_low[-3:],
        "independent_low_output_expansion_round_ids": expansion_low[-3:],
        "independent_low_output_expansion_round_reasons": {
            round_id: expansion_low_reasons[round_id] for round_id in expansion_low[-3:]
        },
        "base_keyword_registration": registration,
        "base_keyword_registration_complete": base_keyword_registration_complete,
        "base_keyword_traversal": base_keyword_metric,
        "base_keyword_traversal_query_ids": base_keyword_query_ids,
        "base_keyword_traversal_complete": base_keyword_traversal_complete,
        "keyword_deepening": deepening,
        "keyword_deepening_complete": keyword_deepening_complete,
        "independent_zero_candidate_system_round_ids": zero_candidate_round_ids,
        "system_round_audits": system_round_audits,
        "expansion_allowed": expansion_allowed,
        "base_search_saturated": base_search_saturated,
        "expansion_started": expansion_started,
        "expansion_traceability": expansion_traceability_metric,
        "expansion_traceability_complete": expansion_traceability_complete,
        "expansion_complete": expansion_complete,
        "expansion_cycle_stopped": expansion_stopped,
        "coverage_complete": coverage_complete,
        "base_coverage_complete": base_coverage_complete,
        "convergence_complete": convergence_complete,
        "all_batches_closed": all_batches_closed,
        "stop_candidate": stop_candidate,
        "pending_work": pending_work,
        "active_keyword_ids": active_keyword_ids,
        "active_deepening_keyword_ids": active_deepening_ids,
        "pair_target_round_id": pair_target_round_id if has_seed else None,
        "paired_search_round_ids": sorted(x for x in paired_search_round_ids if x),
        "next_task_ids": next_task_ids,
        "task_completion_audits": task_audits,
        "coverage_followups": coverage_followups,
        "candidate_source_pending": {cid: reason for cid, reason in source_audits.items() if reason},
        "next_strategy": next_strategy,
        "strategy_budget": strategy_budget,
        "priority": priority,
        "stop_or_continue_reason": reason,
        "rules_version": space["rules_version"],
        "calculated_at": now(),
    }


def query_items(data: dict | None, batch_id: str) -> list[dict]:
    if not data:
        return []
    raw_items = data.get("tasks") or data.get("queries") or []
    result = []
    for index, raw in enumerate(raw_items, start=1):
        if isinstance(raw, str):
            raw = {"query": raw}
        if not isinstance(raw, dict):
            continue
        query_id = str(raw.get("query_id", f"{batch_id}-Q{index:03d}"))
        result.append({
            "query_id": query_id,
            **{k: copy.deepcopy(raw[k]) for k in ("visited_source_urls",) if k in raw},
            "keyword_id": str(raw.get("keyword_id", "")).strip(),
            "query_purpose": str(raw.get("query_purpose", "")).strip().upper(),
            "task_id": str(raw.get("task_id", "")).strip(),
            "batch_id": batch_id,
            "query_text": str(raw.get("query_text", raw.get("query", ""))).strip(),
            "source_type": str(raw.get("source_type", "")).strip(),
            "results_examined": int(raw.get("results_examined", raw.get("result_count", 0)) or 0),
            "unique_candidates": int(raw.get("unique_candidates", raw.get("unique_hits", 0)) or 0),
            "duplicate_candidates": int(raw.get("duplicate_candidates", raw.get("duplicate_hits", 0)) or 0),
            "termination_reason": str(raw.get("termination_reason", raw.get("result", ""))).strip(),
            "district": str(raw.get("district", "")).strip(),
            "role": str(raw.get("role", "")).strip(),
            "industry": str(raw.get("industry", "")).strip(),
            "keyword_family": str(raw.get("keyword_family", "")).strip(),
            "discovery_method": str(raw.get("discovery_method", "")).strip().upper(),
            "round_id": str(raw.get("round_id", "")).strip(),
            "expansion_task_id": str(raw.get("expansion_task_id", "")).strip(),
            "seed_id": str(raw.get("seed_id", "")).strip(),
            "seed_name": str(raw.get("seed_name", "")).strip(),
            "seed_role": str(raw.get("seed_role", "")).strip(),
            "feature_chain_id": str(raw.get("feature_chain_id", "")).strip(),
            "feature_chain": unique_strings(raw.get("feature_chain", [])),
            "feature_values": raw.get("feature_values", {}),
            "similarity_dimensions": unique_strings(raw.get("similarity_dimensions", [])),
            "coverage_gap": str(raw.get("coverage_gap", "")).strip(),
            "source_route": str(raw.get("source_route", "")).strip(),
            **{key: raw[key] for key in system_search.FIELDS + system_search.EXECUTION_FIELDS if key in raw},
        })
    return result


def ingest_closed_batch(
    city_state_path: Path,
    batch_state_path: Path,
    query_log_path: Path | None = None,
    evidence_path: Path | None = None,
    strategy_signature: str = "",
    new_types_added: int = 0,
    gaps_closed: int = 0,
    round_id: str = "",
    round_status: str = "OPEN",
    strategy_id: str = "",
    budget: int = 0,
) -> dict:
    state = load_json(city_state_path)
    before_role_types = {
        str(item.get("role_hypothesis", "")).strip()
        for item in state.get("candidates", []) if str(item.get("role_hypothesis", "")).strip()
    }
    before_open_gaps = {
        item["gap_id"] for item in state.get("gaps", [])
        if item.get("status") in {"OPEN", "IN_PROGRESS"}
    }
    batch = load_json(batch_state_path)
    same_binding(state, batch)
    if batch.get("batch_status") != "CLOSED":
        raise CoverageError("只有CLOSED批次可以汇入城市覆盖状态")
    batch_id = str(batch.get("batch_id", "")).strip()
    if not batch_id:
        raise CoverageError("批次状态缺少 batch_id")
    if find_by_id(state.get("batches", []), "batch_id", batch_id):
        existing = find_by_id(state["batches"], "batch_id", batch_id)
        if existing.get("batch_state_sha256") == sha256_file(batch_state_path):
            return state.get("latest_calculation", calculate(state))
        raise CoverageError(f"批次 {batch_id} 已汇入且内容发生变化；必须先解除冻结或修复冲突")

    # Delivery-only batch: carries existing FINAL records forward for the
    # fixed-size handoff queue, without representing a discovery round.
    if (int(batch.get("query_budget", 0) or 0) == 0
            and not batch.get("queries") and not batch.get("candidates")
            and not batch.get("discovery_records")
            and batch.get("inherited_final_queue")):
        state.setdefault("batches", []).append({
            "batch_id": batch_id, "batch_status": "CLOSED",
            "batch_state_path": canonical_path(batch_state_path),
            "batch_state_sha256": sha256_file(batch_state_path),
            "query_log_path": "", "evidence_path": "",
            "batch_kind": "DELIVERY_ONLY",
        })
        calculation = calculate(state)
        state.setdefault("calculations", []).append(calculation)
        state["latest_calculation"] = calculation
        save_json(city_state_path, state)
        return calculation

    from batch_context_control import audit_discovery, ControlError, candidate_source_problem, refresh_candidate_sources
    try:
        payloads, pending_leads = audit_discovery(batch, allow_pending=bool(batch.get("discovery_carryover")))
    except ControlError as exc:
        raise CoverageError(str(exc)) from exc
    if pending_leads and set(pending_leads) != set(batch.get("discovery_carryover", {}).get("record_ids", [])):
        raise CoverageError("未处置线索未完整登记结转")
    query_data = load_json(query_log_path) if query_log_path else {
        "queries": batch.get("queries", []), "system_search_plan": batch.get("system_search_plan")}
    raw_queries = query_data.get("queries") or query_data.get("tasks") or []
    if raw_queries != batch.get("queries", []):
        raise CoverageError("城市汇入查询与批次已登记查询不一致")
    expected_queries = {q["query_id"]: q for payload in payloads for q in payload["queries"]}
    if any(q != expected_queries.get(q["query_id"]) for q in raw_queries):
        raise CoverageError("城市汇入查询与已校验原始发现结果不一致")
    for rid, row in batch.get("discovery_records", {}).items():
        old = state.setdefault("discovery_records", {}).get(rid)
        if old and (old["record"] != row["record"] or old["result_sha256"] != row["result_sha256"] or (old["disposition"] != "PENDING" and old != row)):
            raise CoverageError("已登记处置记录发生冲突")
        state["discovery_records"][rid] = copy.deepcopy(row)
    state.setdefault("discovery_inputs", {}).update(copy.deepcopy(batch.get("discovery_inputs", {})))
    if query_log_path and str(query_data.get("batch", batch_id)) != batch_id:
        raise CoverageError("查询日志批次号与批次状态不一致")
    queries = query_items(query_data, batch_id)
    system_queries = [q for q in queries if q.get("query_purpose") == "SYSTEM_SEARCH"]
    query_purposes = {q.get("query_purpose") for q in queries}
    carryover_only = not queries and bool(batch.get("discovery_records") or batch.get("candidates"))
    prior_round = find_by_id(state.get("rounds", []), "round_id", round_id.strip() or str(batch.get("round_id", "")).strip())
    if carryover_only:
        if not prior_round or prior_round.get("discovery_mode") != batch.get("mode"):
            raise CoverageError("零查询结转批次必须关联同模式的既有轮次")
        if any(entry.get("origin_batch_id") == batch_id for entry in batch.get("discovery_inputs", {}).values()):
            raise CoverageError("零查询结转批次不得登记本批发现结果")
        current_round_kind = round_kind(state, prior_round)
    elif len(query_purposes) != 1:
        raise CoverageError(
            "同一发现轮次不得混合不同query_purpose：" + ", ".join(sorted(query_purposes))
        )
    else:
        current_round_kind = next(iter(query_purposes))
    effective_strategy_id = (str(prior_round.get("strategy_id", "")).strip().upper() if carryover_only
                             else str(strategy_id or batch.get("strategy_id", "")).strip().upper())
    if current_round_kind == "SYSTEM_SEARCH" and effective_strategy_id not in {"", "SYSTEM_SEARCH"}:
        raise CoverageError("SYSTEM_SEARCH查询的strategy_id与查询用途冲突")
    if current_round_kind == "SYSTEM_SEARCH":
        # 查询用途和已验证计划是权威来源；调用方遗漏可确定字段时由代码补齐。
        effective_strategy_id = "SYSTEM_SEARCH"
    if current_round_kind != "SYSTEM_SEARCH" and effective_strategy_id == "SYSTEM_SEARCH":
        raise CoverageError(
            f"{current_round_kind}查询不得登记为SYSTEM_SEARCH轮次"
        )
    if system_queries or (batch.get("strategy_id") == "SYSTEM_SEARCH" and not carryover_only):
        plan = (query_data or {}).get("system_search_plan")
        if not isinstance(plan, dict):
            raise CoverageError("SYSTEM_SEARCH查询日志必须携带已校验system_search_plan")
        plans = state.setdefault("system_search_plans", {})
        try:
            if plan.get("plan_id") not in plans:
                system_search.require_system_stage(state)
                system_search.validate_plan(state, plan)
            elif plans[plan["plan_id"]] != plan:
                raise system_search.PlanError("同一SYSTEM_SEARCH计划内容发生变化")
            planned = {q["query_id"]: q for q in plan["search_tasks"]}
            if (round_id.strip() or batch.get("round_id")) != plan["round_id"]:
                raise system_search.PlanError("汇入轮次与SYSTEM_SEARCH计划不一致")
            if len(system_queries) != len(queries):
                raise system_search.PlanError("SYSTEM_SEARCH不得混入其他用途查询")
            for q in system_queries:
                target = planned.get(q["query_id"], {})
                if any(q.get(k) != target.get(k) for k in system_search.FIELDS + ("query_text", "keyword_id", "keyword_family", "source_type", "district", "role", "industry")) or q["task_id"] != target.get("search_task_id"):
                    raise system_search.PlanError("执行记录与受控SYSTEM_SEARCH计划不一致：" + q["query_id"])
            plans[plan["plan_id"]] = plan
        except (ValueError, KeyError) as exc:
            raise CoverageError(str(exc)) from exc
    invalid_query_purposes = {
        item["query_id"] for item in queries if item.get("query_purpose") not in QUERY_PURPOSES
    }
    if invalid_query_purposes:
        raise CoverageError(
            "查询日志缺少或使用非法 query_purpose：" + ", ".join(sorted(invalid_query_purposes))
        )
    batch_query_ids = [item["query_id"] for item in queries]
    if len(batch_query_ids) != len(set(batch_query_ids)):
        raise CoverageError("本批查询日志存在重复 query_id")
    existing_query_ids = {item["query_id"] for item in state.get("queries", [])}
    duplicate_query_ids = existing_query_ids.intersection(batch_query_ids)
    if duplicate_query_ids:
        raise CoverageError(f"跨批次 query_id 重复：{', '.join(sorted(duplicate_query_ids))}")
    known_keyword_ids = {
        item["keyword_id"] for item in state.get("base_keywords", []) + state.get("keywords", [])
    }
    keyword_by_id = {item["keyword_id"]: item for item in state.get("keywords", [])}
    for item in queries:
        if item.get("query_purpose") == "KEYWORD_TEST":
            keyword = keyword_by_id.get(item.get("keyword_id"), {})
            if len(keyword.get("test_history", [])) >= 2:
                raise CoverageError(f"新词 {item.get('keyword_id')} 已完成最多两次测试，不得继续测试")
    base_search_ready = calculate(state)["base_search_saturated"]
    unknown_keyword_ids = {
        item["keyword_id"] for item in queries
        if item.get("keyword_id") and item.get("keyword_id") not in known_keyword_ids
    }
    if unknown_keyword_ids:
        raise CoverageError(f"查询日志存在无法反查的 keyword_id：{', '.join(sorted(unknown_keyword_ids))}")
    # 扩展批次可在基础遍历期间测试新词；查询仍按用途独立记账。
    base_keyword_by_id = {item["keyword_id"]: item for item in state.get("base_keywords", [])}
    for item in queries:
        if item["query_purpose"] != BASE_TRAVERSAL_PURPOSE:
            continue
        if not item.get("keyword_id"):
            raise CoverageError(f"基础词遍历查询 {item['query_id']} 缺少 keyword_id")
        base_keyword = base_keyword_by_id.get(item["keyword_id"])
        if not base_keyword:
            raise CoverageError(f"基础词遍历查询 {item['query_id']} 未关联基础关键词")
        if not qualifies_for_base_traversal(
            item, base_keyword, allow_source_blocked_record=True
        ):
            raise CoverageError(
                f"基础词遍历查询 {item['query_id']} 未实际命中登记词或不满足组合约束："
                f"keyword_id={item['keyword_id']}"
            )
    # Expansion plans allocate search_task_id before execution, but the plan
    # controller does not write city state. Register those IDs atomically with
    # the closed batch, before candidate provenance is checked below.
    expansion_tasks = {}
    existing_tasks = {item["task_id"]: item for item in state.get("tasks", [])}
    for query in queries:
        if query.get("query_purpose") != "EXPANSION_DISCOVERY":
            continue
        task_id = str(query.get("task_id", "")).strip()
        expansion_id = str(query.get("expansion_task_id", "")).strip()
        if batch.get("mode") != "expansion" or not task_id or not expansion_id or not query.get("seed_id") or not query.get("feature_chain_id"):
            raise CoverageError(f"扩展查询 {query['query_id']} 缺少批次模式或任务追溯字段")
        if task_id in existing_tasks:
            task = existing_tasks[task_id]
            if (task.get("task_class") != "YIELD_EXPANSION" or
                    task.get("expansion_task_id") != expansion_id or
                    task.get("seed_id") != query["seed_id"] or
                    task.get("feature_chain_id") != query["feature_chain_id"]):
                raise CoverageError(f"扩展查询 {query['query_id']} 的 task_id 与既有任务冲突：{task_id}")
            continue
        task = expansion_tasks.get(task_id)
        if task is None:
            task = normalize_task({"task_id": task_id, "task_class": "YIELD_EXPANSION",
                                   "required": False, "minimum_results_examined": 0,
                                   "keyword_family": query.get("keyword_family", ""),
                                   "source_type": query.get("source_type", "")})
            task.update({"expansion_task_id": expansion_id, "seed_id": query["seed_id"],
                         "feature_chain_id": query["feature_chain_id"],
                         "round_id": query.get("round_id", "")})
            expansion_tasks[task_id] = task
        elif (task["expansion_task_id"] != expansion_id or task["seed_id"] != query["seed_id"] or
              task["feature_chain_id"] != query["feature_chain_id"]):
            raise CoverageError(f"同批扩展查询复用了不同特征链的 task_id：{task_id}")
    state.setdefault("tasks", []).extend(expansion_tasks.values())
    state.setdefault("queries", []).extend(queries)
    base_ids = [q["keyword_id"] for q in queries if q.get("query_purpose") == BASE_TRAVERSAL_PURPOSE]
    if base_ids:
        state["active_keyword_ids"] = list(dict.fromkeys(base_ids))
    for query in queries:
        if query.get("query_purpose") == "KEYWORD_TEST" and query.get("keyword_id") in keyword_by_id:
            keyword = keyword_by_id[query["keyword_id"]]
            keyword["test_query_ids"] = list(dict.fromkeys(
                keyword.get("test_query_ids", []) + [query["query_id"]]
            ))
    known_query_ids = {item["query_id"] for item in state.get("queries", [])}
    known_task_ids = {item["task_id"] for item in state.get("tasks", [])}
    effective_round_id = round_id.strip() or str(batch.get("round_id", "")).strip()
    if not effective_round_id:
        raise CoverageError("汇入批次必须提供 round_id；上下文批次不能自动充当搜索轮次")
    existing_final_seeds = {
        item["candidate_id"]: item for item in state.get("candidates", [])
        if seed_eligible(item)
    }
    queries_by_id = {item["query_id"]: item for item in state.get("queries", [])}

    for query in queries:
        task_id = query["task_id"]
        task = find_by_id(state["tasks"], "task_id", task_id) if task_id else None
        if task:
            task["query_ids"] = list(dict.fromkeys(task.get("query_ids", []) + [query["query_id"]]))
            if query_supports_task(query, task):
                task["status"] = "COMPLETED"
                task["completion_reason"] = query["termination_reason"]

    refresh_materialized_gaps(state)

    seen_candidate_ids = {item["candidate_id"] for item in state.get("candidates", [])}
    seen_dedupe_keys = {item.get("dedupe_key") for item in state.get("candidates", []) if item.get("dedupe_key")}
    for item in batch.get("candidates", []):
        candidate_id = str(item.get("candidate_id", "")).strip()
        if not candidate_id:
            continue
        status = str(item.get("status", "")).upper()
        if status not in CANDIDATE_STATUSES:
            raise CoverageError(f"候选 {candidate_id} 状态非法：{status}")
        problem = candidate_source_problem(item, state.get("discovery_records", {}), queries_by_id)
        if problem:
            raise CoverageError(f"候选 {candidate_id} 来源不一致：{problem}")
        business_fit = str(item.get("business_fit", "")).strip()
        scene = item.get("prospect_fit_evidence", {})
        if status in RESOLVED_CANDIDATE_STATUSES:
            try:
                qualify(status, business_fit, scene, item.get("followup_reason", ""), profile_for(state), state["city"])
            except QualificationError as exc:
                raise CoverageError(f"候选 {candidate_id}：{exc}") from exc
        if status == "EXCLUDED" and not str(item.get("result_note", item.get("note", ""))).strip():
            raise CoverageError(f"EXCLUDED候选 {candidate_id} 缺少排除依据")
        if status == "FINAL":
            required_discovery = (
                "matched_keywords", "discovery_queries", "discovered_by_query_ids",
                "discovery_task_ids", "keyword_families",
            )
            missing = [field for field in required_discovery if not unique_strings(item.get(field, []))]
            if missing:
                raise CoverageError(f"FINAL候选 {candidate_id} 缺少检索来源字段：{', '.join(missing)}")
            unknown_queries = set(unique_strings(item.get("discovered_by_query_ids", []))) - known_query_ids
            unknown_tasks = set(unique_strings(item.get("discovery_task_ids", []))) - known_task_ids
            if unknown_queries or unknown_tasks:
                raise CoverageError(
                    f"FINAL候选 {candidate_id} 的检索来源无法反查："
                    f"query_ids={sorted(unknown_queries)}, task_ids={sorted(unknown_tasks)}"
                )
            if str(batch.get("mode", "")).strip() == "expansion":
                required_expansion = (
                    "round_id", "expansion_task_id", "seed_id", "seed_name",
                    "feature_chain_id", "feature_chain", "feature_values",
                    "similarity_dimensions", "similarity_basis", "coverage_gap", "source_route",
                )
                missing_expansion = [field for field in required_expansion if not item.get(field)]
                if item.get("discovery_method") != "YIELD_EXPANSION" or missing_expansion:
                    raise CoverageError(
                        f"扩展FINAL候选 {candidate_id} 缺少受控追溯字段：{', '.join(missing_expansion)}"
                    )
                if item.get("round_id") != effective_round_id:
                    raise CoverageError(f"扩展FINAL候选 {candidate_id} 的 round_id 与汇入轮次不一致")
                seed = existing_final_seeds.get(str(item.get("seed_id", "")))
                if not seed or str(item.get("seed_name", "")).strip() != str(seed.get("normalized_name", "")).strip():
                    raise CoverageError(f"扩展FINAL候选 {candidate_id} 无法反查到既有候选种子企业")
                expansion_queries = [
                    queries_by_id[query_id]
                    for query_id in unique_strings(item.get("discovered_by_query_ids", []))
                    if query_id in queries_by_id
                    and queries_by_id[query_id].get("query_purpose") == "EXPANSION_DISCOVERY"
                ]
                if not expansion_queries or not any(
                    query.get("expansion_task_id") == item.get("expansion_task_id")
                    and query.get("seed_id") == item.get("seed_id")
                    and query.get("feature_chain_id") == item.get("feature_chain_id")
                    for query in expansion_queries
                ):
                    raise CoverageError(f"扩展FINAL候选 {candidate_id} 无法反查对应的扩展查询与特征链")
        elif business_fit and business_fit not in BUSINESS_FITS:
            raise CoverageError(f"候选 {candidate_id} business_fit非法：{business_fit}")
        key = str(item.get("dedupe_key", item.get("company_name", ""))).strip().casefold()
        if not key:
            raise CoverageError(f"候选 {candidate_id} 缺少 dedupe_key")
        existing_candidate = find_by_id(
            state.get("candidates", []), "candidate_id", candidate_id
        )
        if existing_candidate and (
            existing_candidate.get("status") != "CARRYOVER"
            or existing_candidate.get("dedupe_key") != key
        ):
            raise CoverageError(f"跨批次 candidate_id 重复：{candidate_id}")
        if key in seen_dedupe_keys and not existing_candidate:
            raise CoverageError(f"跨批次 dedupe_key 重复：{key}")
        candidate_record = {
            "candidate_id": candidate_id,
            "normalized_name": str(item.get("company_name", "")).strip(),
            **{k: copy.deepcopy(item[k]) for k in ("provenance", "discovery_reason", "lead_summary", "source_url") if k in item},
            "dedupe_key": key,
            "discovered_by_query_ids": unique_strings(item.get("discovered_by_query_ids", [])),
            "discovery_queries": unique_strings(item.get("discovery_queries", [])),
            "discovery_task_ids": unique_strings(item.get("discovery_task_ids", [])),
            "matched_keywords": unique_strings(item.get("matched_keywords", [])),
            "keyword_families": unique_strings(item.get("keyword_families", [])),
            "status": status,
            "evidence_ids": unique_strings(item.get("evidence_ids", [])),
            "business_fit": business_fit,
            "followup_reason": str(item.get("followup_reason", "")),
            "prospect_fit_evidence": scene,
            "official_website": str(item.get("official_website", "")).strip(),
            "website_verification_status": str(item.get("website_verification_status", "")).strip().upper(),
            "website_evidence_ref": str(item.get("website_evidence_ref", "")).strip(),
            "website_entity_match_note": str(item.get("website_entity_match_note", "")).strip(),
            "qualified_in_batch": batch_id if status == "FINAL" else "",
            "discovery_method": str(item.get("discovery_method", "")).strip().upper(),
            "round_id": str(item.get("round_id", "")).strip(),
            "expansion_task_id": str(item.get("expansion_task_id", "")).strip(),
            "seed_id": str(item.get("seed_id", "")).strip(),
            "seed_name": str(item.get("seed_name", "")).strip(),
            "seed_role": str(item.get("seed_role", "")).strip(),
            "feature_chain_id": str(item.get("feature_chain_id", "")).strip(),
            "feature_chain": unique_strings(item.get("feature_chain", [])),
            "feature_values": item.get("feature_values", {}),
            "similarity_dimensions": unique_strings(item.get("similarity_dimensions", [])),
            "similarity_basis": str(item.get("similarity_basis", "")).strip(),
            "coverage_gap": str(item.get("coverage_gap", "")).strip(),
            "source_route": str(item.get("source_route", "")).strip(),
        }
        if existing_candidate:
            existing_candidate.update(candidate_record)
        else:
            state.setdefault("candidates", []).append(candidate_record)
        seen_candidate_ids.add(candidate_id)
        seen_dedupe_keys.add(key)

    for row in batch.get("discovery_records", {}).values():
        if row["disposition"] != "DUPLICATE":
            continue
        target = find_by_id(state.get("candidates", []), "candidate_id", row["candidate_id"])
        if target is None:
            raise CoverageError("重复线索关联的候选不在城市状态中")
        sources = target.setdefault("provenance", [])
        for source in row["record"]["provenance"]:
            grouped = {**source, "result_sha256": row["result_sha256"]}
            if grouped not in sources:
                sources.append(grouped)
        refresh_candidate_sources(target, state["discovery_records"])

    final_added = sum(1 for item in batch.get("candidates", []) if item.get("status") == "FINAL")
    signature = strategy_signature.strip() or stable_signature([
        [item.get("district"), item.get("role"), item.get("industry"), item.get("keyword_family"), item.get("source_type")]
        for item in queries
    ])
    if system_queries:
        signature = stable_signature([[q.get("path_id"), q.get("source_entry"), q["query_text"]]
                                      for q in plan["search_tasks"]])
    round_id = effective_round_id
    round_status = round_status.upper()
    if round_status not in ROUND_STATUSES:
        raise CoverageError(f"轮次状态非法：{round_status}")
    round_item = find_by_id(state.setdefault("rounds", []), "round_id", round_id)
    if not round_item:
        paired_search_round_id = ""
        if str(batch.get("mode", "")).strip() == "expansion":
            paired = {r.get("paired_search_round_id") for r in state.get("rounds", [])
                      if r.get("discovery_mode") == "expansion"}
            preceding = next((r for r in reversed(state.get("rounds", []))
                              if r.get("discovery_mode") == "search"
                              and r.get("round_kind") in {"BASE_KEYWORD_TRAVERSAL", "KEYWORD_DEEPENING", "SYSTEM_SEARCH"}
                              and r.get("round_status") in {"COMPLETED", "EARLY_STOPPED"}), None)
            if preceding and preceding.get("round_id") not in paired:
                paired_search_round_id = preceding["round_id"]
        round_item = {"round_id": round_id, "round_status": "OPEN", "batch_ids": [],
                      "discovery_mode": str(batch.get("mode", "")).strip(),
                      "paired_search_round_id": paired_search_round_id,
                      "round_kind": current_round_kind,
                      "strategy_id": effective_strategy_id, "budget": budget,
                      "strategy_signature": signature, "queries_executed": 0, "results_examined": 0,
                      "unique_candidates_added": 0, "new_pool_candidates_added": 0,
                      "verified_candidates": 0, "final_added": 0,
                      "duplicate_count": 0, "new_types_added": 0, "gaps_closed": 0,
                      "normal_termination_query_count": 0, "source_blocked_query_count": 0,
                      "candidate_statuses": {}, "excluded_candidate_count": 0,
                      "followup_candidate_count": 0, "carryover_candidate_count": 0,
                      "unverified_candidate_count": 0, "unresolved_candidate_count": 0,
                      "new_entity_keys": [], "all_discovery_records_disposed": False}
        state["rounds"].append(round_item)
    elif round_item.get("discovery_mode") != str(batch.get("mode", "")).strip():
        raise CoverageError("同一搜索轮次不得混合 search 与 expansion 批次")
    elif round_kind(state, round_item) != current_round_kind:
        raise CoverageError("同一round_id不得混合不同query_purpose")
    round_item["batch_ids"].append(batch_id)
    round_item["queries_executed"] += len(queries)
    round_item["normal_termination_query_count"] = int(round_item.get("normal_termination_query_count", 0)) + sum(
        1 for query in queries if query.get("termination_reason") != "SOURCE_BLOCKED"
    )
    round_item["source_blocked_query_count"] = int(round_item.get("source_blocked_query_count", 0)) + sum(
        1 for query in queries if query.get("termination_reason") == "SOURCE_BLOCKED"
    )
    round_item["results_examined"] += sum(q["results_examined"] for q in queries)
    candidate_statuses = round_item.setdefault("candidate_statuses", {})
    for candidate in batch.get("candidates", []):
        candidate_id = str(candidate.get("candidate_id", "")).strip()
        if candidate_id:
            candidate_statuses[candidate_id] = str(candidate.get("status", "")).upper()
    new_entity_keys = set(round_item.get("new_entity_keys", []))
    batch_records = [row for row in batch.get("discovery_records", {}).values()
                     if row.get("result_sha256") in batch.get("discovery_inputs", {})
                     and batch["discovery_inputs"][row["result_sha256"]].get("origin_batch_id") == batch_id]
    pending_record_ids = sorted(rid for rid, row in batch.get("discovery_records", {}).items()
                                if row.get("disposition") == "PENDING")
    declared_carryover = set(batch.get("discovery_carryover", {}).get("record_ids", []))
    if set(pending_record_ids) != declared_carryover:
        raise CoverageError("本批未处置发现记录与已登记结转清单不一致")
    for row in batch_records:
        if row.get("counts_as_unique_entity"):
            key = str(row.get("resolved_dedupe_key", "")).strip()
            if not key:
                raise CoverageError("新增主体记录缺少resolved_dedupe_key")
            new_entity_keys.add(key)
    round_item["new_entity_keys"] = sorted(new_entity_keys)
    round_item["unique_candidates_added"] = len(new_entity_keys)
    pool_keys = set(round_item.get("new_pool_entity_keys", []))
    pool_keys.update(str(row.get("resolved_dedupe_key", "")).strip()
                     for row in batch_records
                     if row.get("disposition") == "ADDED" and row.get("counts_as_unique_entity"))
    pool_keys.discard("")
    round_item["new_pool_entity_keys"] = sorted(pool_keys)
    round_item["new_pool_candidates_added"] = len(pool_keys)
    carried_ids = set(round_item.get("pending_discovery_record_ids", [])) | set(pending_record_ids)
    round_item["pending_discovery_record_ids"] = sorted(
        rid for rid in carried_ids
        if state.get("discovery_records", {}).get(rid, {}).get("disposition") == "PENDING"
    )
    round_item["all_discovery_records_disposed"] = not round_item["pending_discovery_record_ids"]
    # A prior round may have remained OPEN solely because this batch carried
    # its pending discovery records. Reconcile it once those records and all
    # candidate work are complete; never infer completion from query count alone.
    for prior_round in state.get("rounds", []):
        if prior_round is round_item:
            continue
        prior_ids = set(prior_round.get("pending_discovery_record_ids", []))
        prior_round["pending_discovery_record_ids"] = sorted(
            rid for rid in prior_ids
            if state.get("discovery_records", {}).get(rid, {}).get("disposition") == "PENDING"
        )
        prior_round["all_discovery_records_disposed"] = not prior_round["pending_discovery_record_ids"]
        prior_statuses = prior_round.get("candidate_statuses", {})
        for candidate_id in prior_statuses:
            latest = find_by_id(state.get("candidates", []), "candidate_id", candidate_id)
            if latest:
                prior_statuses[candidate_id] = latest.get("status", prior_statuses[candidate_id])
        prior_round["unresolved_candidate_count"] = sum(
            status in {"CARRYOVER", "UNVERIFIED", "IN_WORKSET"}
            for status in prior_statuses.values()
        )
        if (prior_round.get("round_status") == "OPEN"
                and prior_round.get("budget_complete")
                and prior_round.get("all_discovery_records_disposed")
                and int(prior_round.get("unresolved_candidate_count", 0)) == 0):
            prior_round["round_status"] = "COMPLETED"
    round_item["duplicate_count"] += sum(q["duplicate_candidates"] for q in queries)
    round_item["verified_candidates"] += sum(1 for i in batch.get("candidates", []) if i.get("status") in {"FINAL", "FOLLOWUP", "EXCLUDED"})
    round_item["final_added"] += final_added
    after_role_types = {
        str(item.get("role_hypothesis", "")).strip()
        for item in state.get("candidates", []) if str(item.get("role_hypothesis", "")).strip()
    }
    after_open_gaps = {
        item["gap_id"] for item in state.get("gaps", [])
        if item.get("status") in {"OPEN", "IN_PROGRESS"}
    }
    # Caller-provided counters are retained only as a backwards-compatible
    # floor.  Normal operation derives both values from state transitions.
    round_item["new_types_added"] += max(len(after_role_types - before_role_types), new_types_added)
    round_item["gaps_closed"] += max(len(before_open_gaps - after_open_gaps), gaps_closed)
    round_item["excluded_candidate_count"] = sum(status == "EXCLUDED" for status in candidate_statuses.values())
    round_item["followup_candidate_count"] = sum(status == "FOLLOWUP" for status in candidate_statuses.values())
    round_item["carryover_candidate_count"] = sum(status == "CARRYOVER" for status in candidate_statuses.values())
    round_item["unverified_candidate_count"] = sum(status in {"UNVERIFIED", "IN_WORKSET"} for status in candidate_statuses.values())
    round_item["unresolved_candidate_count"] = sum(
        status in {"CARRYOVER", "UNVERIFIED", "IN_WORKSET"}
        for status in candidate_statuses.values()
    )
    if batch.get("mode") == "expansion":
        streak = list(round_item.get("expansion_no_new_streak_keys", []))
        for row in batch_records:
            if row.get("disposition") == "ADDED" and row.get("counts_as_unique_entity"):
                streak = []
            elif row.get("disposition") == "DUPLICATE":
                key = str(row.get("resolved_dedupe_key", "")).strip()
                if key and key not in streak:
                    streak.append(key)
        round_item["expansion_no_new_streak_keys"] = streak
        eligible_early_stop = len(streak) >= policy_for(state)["expansion"]["duplicate_streak"] and not round_item.get("source_blocked_query_count")
        if round_status == "EARLY_STOPPED" and not eligible_early_stop:
            raise CoverageError("扩展EARLY_STOPPED未满足绑定策略的无新增阈值，或存在来源受限查询")
        if batch.get("expansion_early_stop") != (round_status == "EARLY_STOPPED"):
            raise CoverageError("扩展批次截停标记与轮次状态不一致")
    if (carryover_only and round_status == "OPEN" and not round_item["pending_discovery_record_ids"]
            and not round_item.get("unresolved_candidate_count") and round_item.get("budget_complete")):
        round_status = "COMPLETED"
    round_item["round_status"] = round_status
    round_item["budget"] = budget or round_item.get("budget", 0)
    round_item["budget_complete"] = round_status == "EARLY_STOPPED" or (round_item["budget"] > 0 and round_item["queries_executed"] >= round_item["budget"])
    round_item["candidate_new_rate"] = round_item["unique_candidates_added"] / max(1, round_item["results_examined"])
    round_item["final_conversion_rate"] = round_item["final_added"] / max(1, round_item["verified_candidates"])
    round_item["duplicate_rate"] = round_item["duplicate_count"] / max(1, round_item["duplicate_count"] + round_item["unique_candidates_added"])
    round_item["final_per_query"] = round_item["final_added"] / max(1, round_item["queries_executed"])
    round_item["final_per_verified_candidate"] = round_item["final_added"] / max(1, round_item["verified_candidates"])
    state.setdefault("batches", []).append({
        "batch_id": batch_id,
        "batch_status": "CLOSED",
        "batch_state_path": canonical_path(batch_state_path),
        "batch_state_sha256": sha256_file(batch_state_path),
        "query_log_path": canonical_path(query_log_path) if query_log_path else "",
        "evidence_path": canonical_path(evidence_path) if evidence_path else "",
    })
    removed_ids = [event.get("removed_candidate_id") for event in batch.get("events", [])
                   if event.get("action") == "RECLASSIFY_DUPLICATE" and event.get("removed_candidate_id")]
    state["candidate_id_reservations"] = list(dict.fromkeys(
        state.get("candidate_id_reservations", []) + batch.get("candidate_id_reservations", []) + removed_ids
    ))
    for path in (batch_state_path, query_log_path, evidence_path):
        if path:
            absolute = canonical_path(path)
            if absolute not in state.setdefault("registered_json_files", []):
                state["registered_json_files"].append(absolute)
    for entry in batch.get("discovery_inputs", {}).values():
        merged_path = Path(entry["path"])
        merged = discovery.load_validated_discovery(merged_path)
        artifacts = [str(merged_path), merged["validation"]["manifest_path"]] + [r["path"] for r in merged["validation"]["results"]]
        for artifact in artifacts:
            absolute = canonical_path(Path(artifact))
            if absolute not in state.setdefault("registered_json_files", []):
                state["registered_json_files"].append(absolute)
    calculation = calculate(state)
    sync_coverage_followups(state, calculation)
    state.setdefault("calculations", []).append(calculation)
    state["latest_calculation"] = calculation
    save_json(city_state_path, state)
    return calculation


def command_init(args: argparse.Namespace) -> None:
    require_token(args)
    state_path = Path(args.state)
    if state_path.exists() and not args.force:
        raise CoverageError(f"城市状态已存在：{state_path}")
    space_path = Path(args.space)
    binding = bind_profile(args.profile)
    bound_state = {"b2b_config": binding}
    profile = profile_for(bound_state)
    authority = load_authoritative_base_keywords(asset_path(bound_state, "keywords"))
    supplied = load_json(space_path)
    for key in ("roles", "industries", "source_types"):
        expected = profile["target"][key]
        if key in supplied and supplied[key] != expected:
            raise CoverageError(f"搜索空间{key}与行业配置不一致")
        supplied[key] = expected
    families = list(dict.fromkeys(k["keyword_family"] for k in authority["base_keywords"]))
    if "keyword_families" in supplied and supplied["keyword_families"] != families:
        raise CoverageError("搜索空间关键词族与行业配置不一致")
    supplied["keyword_families"] = families
    space = validate_space(supplied, authority)
    state = {
        "schema_version": 4,
        "b2b_config": binding,
        "city": space["city"],
        "search_space": {key: value for key, value in space.items() if key not in {"tasks", "base_keywords", "keywords", "gaps"}},
        "tasks": space["tasks"],
        "queries": [],
        "candidates": [],
        "base_keywords": space["base_keywords"],
        "base_keyword_authority": {
            "path": canonical_path(authority["path"]),
            "sha256": authority["sha256"],
            "schema_version": authority["schema_version"],
            "total_keywords": authority["total_keywords"],
        },
        "keywords": space["keywords"],
        "gaps": space["gaps"],
        "rounds": [],
        "batches": [],
        "registered_json_files": [
            canonical_path(space_path),
            *[record["path"] for record in [*binding["files"], *binding["engine_files"]]],
        ],
        "calculations": [],
        "latest_calculation": None,
        "created_at": now(),
    }
    save_json(state_path, state)
    print(f"已初始化城市覆盖状态：{state_path}")


def command_ingest(args: argparse.Namespace) -> None:
    require_token(args)
    result = ingest_closed_batch(
        Path(args.state), Path(args.batch_state),
        Path(args.query_log) if args.query_log else None,
        Path(args.evidence) if args.evidence else None,
        args.strategy_signature, args.new_types_added, args.gaps_closed,
        args.round_id, args.round_status, args.strategy_id, args.budget,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_set_task(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_json(path)
    task = find_by_id(state.get("tasks", []), "task_id", args.task_id)
    if not task:
        raise CoverageError(f"未找到任务：{args.task_id}")
    if args.status not in TASK_STATUSES:
        raise CoverageError(f"非法任务状态：{args.status}")
    if args.status in {"COMPLETED", "BLOCKED_ALLOWED", "BLOCKED_CRITICAL"} and not args.reason.strip():
        raise CoverageError("终态任务必须填写原因")
    if args.status in COMPLETED_TASK_STATUSES and not task_completion_audit(state, task)["qualified"]:
        raise CoverageError("任务不能仅凭完成标签通过；缺有效关联查询或维度依据")
    task["status"] = args.status
    task["completion_reason"] = args.reason.strip()
    state["latest_calculation"] = calculate(state)
    sync_coverage_followups(state, state["latest_calculation"])
    save_json(path, state)
    print(f"任务 {args.task_id} → {args.status}")


def command_set_keyword(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_json(path)
    keyword = find_by_id(state.get("keywords", []), "keyword_id", args.keyword_id)
    if not keyword:
        raise CoverageError(f"未找到扩展词：{args.keyword_id}")
    if keyword.get("keyword_origin") == "DISCOVERED" and args.status != keyword.get("status"):
        raise CoverageError("DISCOVERED词状态只能由evaluate-keywords依据测试结果晋级或停用；set-keyword仅更新回流完成状态")
    calculation = calculate(state)
    if args.status != "PENDING_TEST" and not keyword.get("test_query_ids"):
        raise CoverageError(f"扩展词 {args.keyword_id} 尚无KEYWORD_TEST查询记录，禁止状态晋级")
    if args.replay_completed:
        if args.status != "APPROVED" or not keyword.get("replay_task_ids"):
            raise CoverageError("回流完成仅适用于APPROVED词，且必须存在replay_task_ids")
        task_by_id = {item["task_id"]: item for item in state.get("tasks", [])}
        if any(
            task_id not in task_by_id
            or not task_completion_audit(state, task_by_id[task_id])["qualified"]
            for task_id in keyword["replay_task_ids"]
        ):
            raise CoverageError("扩展词仍有未完成或缺少执行依据的回流任务")
    keyword["status"] = args.status
    keyword["replay_completed"] = args.replay_completed
    state["latest_calculation"] = calculate(state)
    save_json(path, state)
    print(f"扩展词 {args.keyword_id} 已更新")


def command_register_keyword(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_json(path)
    if find_by_id(state.get("keywords", []), "keyword_id", args.keyword_id):
        raise CoverageError(f"扩展词ID已存在：{args.keyword_id}")
    keyword = normalize_keyword({
        "keyword_id": args.keyword_id,
        "keyword": args.keyword,
        "keyword_origin": "DISCOVERED",
        "status": "PENDING_TEST",
    })
    if not keyword["keyword"]:
        raise CoverageError("DISCOVERED新词内容不能为空")
    state.setdefault("keywords", []).append(keyword)
    state["latest_calculation"] = calculate(state)
    save_json(path, state)
    print(f"已登记DISCOVERED新词 {args.keyword_id} → PENDING_TEST")


def command_evaluate_keywords(args: argparse.Namespace) -> None:
    """Advance tested discovered keywords from bottom-level candidate outcomes."""
    require_token(args)
    path = Path(args.state)
    state = load_json(path)
    query_by_id = {q["query_id"]: q for q in state.get("queries", [])}
    changed = []
    for keyword in state.get("keywords", []):
        if keyword.get("keyword_origin") != "DISCOVERED" or keyword.get("status") not in {"PENDING_TEST", "RESTRICTED"}:
            continue
        processed_ids = set(keyword.get("evaluated_test_query_ids", []))
        test_ids = [qid for qid in keyword.get("test_query_ids", [])
                    if query_by_id.get(qid, {}).get("query_purpose") == "KEYWORD_TEST"
                    and qid not in processed_ids
                    and query_by_id[qid].get("termination_reason") in {"BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED"}]
        if not test_ids:
            continue
        history = keyword.setdefault("test_history", [])
        if len(history) >= 2:
            raise CoverageError(f"新词 {keyword['keyword_id']} 已完成最多两次测试，禁止第三次评价")
        new_candidate_ids = {
            row.get("candidate_id") for row in state.get("discovery_records", {}).values()
            if row.get("disposition") == "ADDED" and row.get("counts_as_unique_entity")
            and any(p.get("query_id") in test_ids for p in row.get("record", {}).get("provenance", []))
        }
        new_keys = {
            row.get("resolved_dedupe_key") for row in state.get("discovery_records", {}).values()
            if row.get("disposition") == "ADDED" and row.get("counts_as_unique_entity")
            and any(p.get("query_id") in test_ids for p in row.get("record", {}).get("provenance", []))
        }
        tested = len(new_keys)
        qualified = len({c.get("dedupe_key") or c.get("candidate_id") for c in state.get("candidates", [])
                         if c.get("candidate_id") in new_candidate_ids and c.get("status") == "FINAL"})
        rate = qualified / tested if tested else 0.0
        snapshot = {"test_query_ids": test_ids, "tested_candidates": tested,
                    "qualified_candidates": qualified, "new_candidates": tested,
                    "target_candidate_rate": round(rate, 6)}
        history.append(snapshot)
        keyword["evaluated_test_query_ids"] = list(dict.fromkeys([*processed_ids, *test_ids]))
        zero_streak = 0
        for item in reversed(history):
            if item.get("new_candidates", item.get("tested_candidates", 0)) == 0:
                zero_streak += 1
            else:
                break
        old = keyword.get("status")
        if zero_streak >= 2:
            keyword["status"] = "DISABLED"
        elif rate >= 0.40:
            keyword["status"] = "APPROVED"
        else:
            keyword["status"] = "RESTRICTED"
        if keyword["status"] != old:
            changed.append({"keyword_id": keyword["keyword_id"], "from": old,
                            "to": keyword["status"], **snapshot})
    state["latest_calculation"] = calculate(state)
    save_json(path, state)
    print(json.dumps({"changed": changed}, ensure_ascii=False, indent=2))


def command_set_gap(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_json(path)
    gap = find_by_id(state.get("gaps", []), "gap_id", args.gap_id)
    if not gap:
        raise CoverageError(f"未找到缺口：{args.gap_id}")
    if args.status == "CLOSED":
        task_by_id = {item["task_id"]: item for item in state.get("tasks", [])}
        resolution_ids = gap.get("resolution_task_ids", [])
        if not resolution_ids or any(
            task_id not in task_by_id
            or not task_completion_audit(state, task_by_id[task_id])["qualified"]
            for task_id in resolution_ids
        ):
            raise CoverageError("关闭缺口必须关联已完成且具有执行依据的resolution_task_ids")
    gap["status"] = args.status
    state["latest_calculation"] = calculate(state)
    save_json(path, state)
    print(f"缺口 {args.gap_id} → {args.status}")


def command_evaluate(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_json(path)
    result = calculate(state)
    sync_coverage_followups(state, result)
    state.setdefault("calculations", []).append(result)
    state["latest_calculation"] = result
    save_json(path, state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_materialize_gaps(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_json(path)
    result = materialize_blocking_gap_tasks(state)
    state["latest_calculation"] = calculate(state)
    sync_coverage_followups(state, state["latest_calculation"])
    save_json(path, state)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_freeze(args: argparse.Namespace) -> None:
    require_token(args)
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    latest = calculate(state)
    if not latest["stop_candidate"] and not args.force:
        raise CoverageError("尚未达到STOP_CANDIDATE；禁止冻结。仅诊断时可显式使用 --force")
    files = [state_path]
    for item in state.get("registered_json_files", []):
        path = Path(item).resolve()
        if path not in files:
            files.append(path)
    missing = [str(path) for path in files if not path.exists()]
    if missing:
        raise CoverageError(f"冻结输入缺失：{', '.join(missing)}")
    entries = [{
        "path": canonical_path(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "type": "city_state" if path == state_path else "input_json",
    } for path in sorted(files, key=lambda item: str(item).casefold())]
    manifest_core = {
        "city": state["city"],
        "rules_version": state["search_space"]["rules_version"],
        "batch_range": [item["batch_id"] for item in state.get("batches", [])],
        "files": entries,
        "diagnostic_only": bool(args.force),
    }
    manifest = {
        "schema_version": 1,
        "manifest_id": f"{state['city']}-{stable_signature(manifest_core)}",
        **manifest_core,
        "created_at": now(),
        "diagnostic_only": bool(args.force),
    }
    manifest["manifest_sha256"] = sha256_json(manifest_core)
    save_json(Path(args.manifest), manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def manifest_core(manifest):
    return {key: manifest.get(key) for key in ('city', 'rules_version', 'batch_range', 'files', 'diagnostic_only')}


def read_frozen_inputs(manifest_path, city_state_path=None):
    from b2b_config import binding_for
    manifest_path = Path(manifest_path).resolve()
    manifest = load_json(manifest_path)
    if sha256_json(manifest_core(manifest)) != manifest.get('manifest_sha256'):
        raise CoverageError('冻结清单自身哈希无效')
    entries = manifest.get('files')
    if not isinstance(entries, list) or not entries:
        raise CoverageError('冻结清单不能为空')
    paths = set()
    city_paths = []
    for entry in entries:
        path = Path(entry['path'])
        if not path.is_absolute():
            path = manifest_path.parent / path
        path = path.resolve()
        if path in paths:
            raise CoverageError('冻结清单存在重复文件')
        paths.add(path)
        if not path.is_file() or path.stat().st_size != entry.get('size') or sha256_file(path) != entry.get('sha256'):
            raise CoverageError(f'冻结输入完整性失败：{path}')
        if entry.get('type') == 'city_state':
            city_paths.append(path)
    if len(city_paths) != 1:
        raise CoverageError('冻结清单必须登记唯一城市状态')
    city_path = city_paths[0]
    if city_state_path is not None and Path(city_state_path).resolve() != city_path:
        raise CoverageError('城市状态与冻结清单登记路径不一致')
    state = load_json(city_path)
    binding = binding_for(state)
    required = {city_path, *[Path(p).resolve() for p in state.get('registered_json_files', [])],
                *[Path(f['path']).resolve() for f in binding['files'] + binding['engine_files']]}
    for batch in state.get('batches', []):
        for key in ('state_path', 'batch_state_path', 'query_log_path', 'evidence_path'):
            if batch.get(key):
                p = Path(batch[key]); required.add((p if p.is_absolute() else city_path.parent/p).resolve())
    if not required <= paths:
        raise CoverageError(f'冻结清单遗漏必需输入：{sorted(str(p) for p in required-paths)}')
    if manifest.get('city') != state.get('city') or manifest.get('rules_version') != state['search_space']['rules_version']:
        raise CoverageError('冻结清单城市或规则版本不一致')
    if manifest.get('batch_range') != [b['batch_id'] for b in state.get('batches', [])]:
        raise CoverageError('冻结清单批次范围不一致')
    return manifest, state, city_path


def recompute_frozen_snapshot(manifest, state):
    result = calculate(state)
    result.pop('calculated_at', None)
    comparable = ('metrics', 'base_keyword_traversal', 'base_keyword_traversal_complete',
                  'independent_zero_candidate_system_round_ids', 'expansion_allowed',
                  'coverage_complete', 'convergence_complete', 'stop_candidate')
    differences = [key for key in comparable if (state.get('latest_calculation') or {}).get(key) != result.get(key)]
    return {
        'schema_version': 2,
        'snapshot_id': f"{state['city']}-COV-{stable_signature([manifest['manifest_sha256'], result])}",
        'city': state['city'], 'rules_version': state['search_space']['rules_version'],
        'batch_range': [b['batch_id'] for b in state.get('batches', [])],
        'manifest_sha256': manifest['manifest_sha256'], 'metrics': result['metrics'],
        'coverage_complete': result['coverage_complete'], 'convergence_complete': result['convergence_complete'],
        'search_complete': bool(result['stop_candidate'] and not differences and not manifest.get('diagnostic_only')),
        'diagnostic_only': bool(manifest.get('diagnostic_only')),
        'recalculation_differences': differences,
        'config_fingerprint': binding_for(state)['fingerprint'],
        'execution_version': binding_for(state)['execution_version'],
    }


def verify_formal_snapshot(manifest_path, snapshot_path, city_state_path=None):
    manifest, state, path = read_frozen_inputs(manifest_path, city_state_path)
    expected = recompute_frozen_snapshot(manifest, state)
    snapshot = load_json(Path(snapshot_path))
    if any(snapshot.get(k) != value for k, value in expected.items()):
        raise CoverageError('正式快照与只读全量重算结果不一致')
    if expected['search_complete'] is not True:
        raise CoverageError('检索未完成或为诊断冻结，禁止正式交付')
    return manifest, snapshot, state, path


def command_recalculate(args):
    require_token(args)
    manifest, state, _ = read_frozen_inputs(Path(args.manifest))
    snapshot = recompute_frozen_snapshot(manifest, state)
    snapshot['calculated_at'] = now()
    save_json(Path(args.output), snapshot)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


def command_status(args: argparse.Namespace) -> None:
    state = load_json(Path(args.state))
    # Always calculate from bottom-level records. A cached result may have been
    # produced by an older rule version and must not preserve a false pass.
    print(json.dumps(calculate(state), ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="城市B2B搜索覆盖控制")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="从冻结搜索空间初始化城市状态")
    init.add_argument("--state", required=True)
    init.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    init.add_argument("--space", required=True)
    init.add_argument("--profile", required=True, help="本次任务的产品或服务与客户画像配置")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init)

    ingest = sub.add_parser("ingest-batch", help="汇入一个已关闭批次并累计计算")
    ingest.add_argument("--state", required=True)
    ingest.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    ingest.add_argument("--batch-state", required=True)
    ingest.add_argument("--query-log")
    ingest.add_argument("--evidence")
    ingest.add_argument("--strategy-signature", default="")
    ingest.add_argument("--new-types-added", type=int, default=0)
    ingest.add_argument("--gaps-closed", type=int, default=0)
    ingest.add_argument("--round-id", required=True)
    ingest.add_argument("--round-status", choices=sorted(ROUND_STATUSES), default="OPEN")
    ingest.add_argument("--strategy-id", default="")
    ingest.add_argument("--budget", type=int, default=0)
    ingest.set_defaults(func=command_ingest)

    set_task = sub.add_parser("set-task", help="显式更新覆盖任务状态")
    set_task.add_argument("--state", required=True)
    set_task.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    set_task.add_argument("--task-id", required=True)
    set_task.add_argument("--status", choices=sorted(TASK_STATUSES), required=True)
    set_task.add_argument("--reason", default="")
    set_task.set_defaults(func=command_set_task)

    set_keyword = sub.add_parser("set-keyword", help="更新扩展词状态")
    set_keyword.add_argument("--state", required=True)
    set_keyword.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    set_keyword.add_argument("--keyword-id", required=True)
    set_keyword.add_argument("--status", choices=sorted(KEYWORD_STATUSES), required=True)
    set_keyword.add_argument("--replay-completed", action="store_true")
    set_keyword.set_defaults(func=command_set_keyword)

    register_keyword = sub.add_parser("register-keyword", help="在扩展阶段登记DISCOVERED新词")
    register_keyword.add_argument("--state", required=True)
    register_keyword.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    register_keyword.add_argument("--keyword-id", required=True)
    register_keyword.add_argument("--keyword", required=True)
    register_keyword.set_defaults(func=command_register_keyword)

    evaluate_keywords = sub.add_parser("evaluate-keywords", help="按KEYWORD_TEST真实候选结果自动晋级或停用扩展词")
    evaluate_keywords.add_argument("--state", required=True)
    evaluate_keywords.add_argument("--caller-token", required=True)
    evaluate_keywords.set_defaults(func=command_evaluate_keywords)

    set_gap = sub.add_parser("set-gap", help="更新覆盖缺口状态")
    set_gap.add_argument("--state", required=True)
    set_gap.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    set_gap.add_argument("--gap-id", required=True)
    set_gap.add_argument("--status", choices=sorted(GAP_STATUSES), required=True)
    set_gap.set_defaults(func=command_set_gap)

    evaluate = sub.add_parser("evaluate", help="累计重算并保存当前状态")
    evaluate.add_argument("--state", required=True)
    evaluate.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    evaluate.set_defaults(func=command_evaluate)

    materialize = sub.add_parser("materialize-gaps", help="将覆盖审计阻断缺口转换为可执行MANDATORY_COVERAGE任务")
    materialize.add_argument("--state", required=True)
    materialize.add_argument("--caller-token", required=True, help="主控调用方令牌")
    materialize.set_defaults(func=command_materialize_gaps)

    freeze = sub.add_parser("freeze", help="冻结全部已登记JSON并生成哈希清单")
    freeze.add_argument("--state", required=True)
    freeze.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    freeze.add_argument("--manifest", required=True)
    freeze.add_argument("--force", action="store_true")
    freeze.set_defaults(func=command_freeze)

    recalculate = sub.add_parser("recalculate", help="校验冻结清单并从零全量重算")
    recalculate.add_argument("--manifest", required=True)
    recalculate.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    recalculate.add_argument("--output", required=True)
    recalculate.set_defaults(func=command_recalculate)

    status = sub.add_parser("status", help="输出当前累计计算")
    status.add_argument("--state", required=True)
    status.set_defaults(func=command_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
        return 0
    except (CoverageError, StateGuardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
