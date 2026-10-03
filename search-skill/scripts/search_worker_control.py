#!/usr/bin/env python3
"""检索阶段无状态研究子任务控制器。

本脚本只创建、导出、校验和合并临时任务；不修改候选池、
城市状态、正式批次或 Excel。
"""

from __future__ import annotations

from b2b_qualification import validate_qualification as qualify, QualificationError

from b2b_config import policy_for, check_if_bound, asset_path, profile_for, binding_for, same_binding, bind_profile, ConfigError

import copy
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import system_search_plan_control as system_search


TERMINAL_STATUSES = {"FINAL", "FOLLOWUP", "EXCLUDED"}
VALID_FALLBACK_REASONS = {
    "SINGLE_QUERY", "SINGLE_CANDIDATE", "SINGLE_EXPANSION_PLAN", "PLATFORM_UNSUPPORTED", "WORKER_UNAVAILABLE",
}
DISCOVERY_QUERY_KEYS = {
    "query_id", "query_text", "results_examined", "unique_candidates",
    "duplicate_candidates", "termination_reason", "visited_source_urls",
}
VALID_TERMINATION_REASONS = {
    "BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED", "SOURCE_BLOCKED", "STRATEGY_STOP",
}
DISCOVERY_CANDIDATE_KEYS = {
    "company_name", "source_url", "matched_keywords", "discovery_query_id", "discovery_query", "preliminary_role",
    "discovery_method", "round_id", "expansion_task_id", "seed_id", "seed_name",
    "seed_role", "feature_chain_id", "feature_chain", "feature_values",
    "similarity_dimensions", "similarity_basis", "coverage_gap", "source_route",
    "discovery_reason", "lead_summary",
}
EXPANSION_PLAN_FIELDS = {
    "round_id", "expansion_task_id", "seed_id", "seed_name", "feature_chain_id",
    "feature_chain", "feature_values", "similarity_dimensions", "coverage_gap", "source_route",
}
VERIFICATION_RESULT_KEYS = {
    "candidate_id", "recommended_status", "business_fit", "prospect_fit_evidence", "note", "followup_reason",
    "official_website", "website_verification_status", "website_evidence_ref", "website_entity_match_note",
}
WEBSITE_STATUSES = {"VERIFIED", "NOT_FOUND", "CONFLICT"}
FOLLOWUP_REASONS = {
    "IDENTITY_PENDING", "LOCATION_PENDING", "BUSINESS_FIT_PENDING",
    "QUALIFYING_EVIDENCE_MISSING", "SOURCE_BLOCKED",
}


class WorkerControlError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    try:
        return check_if_bound(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerControlError(f"无法读取JSON：{path}：{exc}") from exc


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_task_package(path: Path, value: Any) -> None:
    """Create an immutable task package; never overwrite an earlier workset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError as exc:
        raise WorkerControlError(f"任务包路径已存在，禁止覆盖旧任务包：{path}") from exc


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def text(value: Any) -> str:
    return str(value or "").strip()


def unique(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(text(value) for value in values if text(value)))


def normalize_company(value: Any) -> str:
    return "".join(text(value).casefold().split())


def task_package_id(manifest: dict[str, Any], worker: dict[str, Any]) -> str:
    identity = {
        "task_type": manifest.get("task_type"),
        "formal_batch_id": manifest.get("formal_batch_id"),
        "input_version": manifest.get("input_sha256"),
        "research_task_id": worker.get("research_task_id"),
        "assigned_query_ids": worker.get("assigned_query_ids", []),
        "candidate_ids": worker.get("candidate_ids", []),
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def current_manifest(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    manifest_path = Path(args.manifest).resolve()
    manifest = load_json(manifest_path)
    if manifest.get("status") == "INVALIDATED":
        raise WorkerControlError("临时任务清单已作废")
    state_path = Path(manifest.get("formal_state", "")).resolve()
    if not state_path.exists() or file_hash(state_path) != manifest.get("input_sha256"):
        raise WorkerControlError("正式状态已变化，子任务结果已过期")
    state = load_json(state_path)
    if state.get("batch_id") != manifest.get("formal_batch_id") or state.get("batch_status") != "OPEN":
        raise WorkerControlError("正式批次不再匹配或已关闭")
    return manifest, manifest_path


def split_evenly(values: list[Any], count: int) -> list[list[Any]]:
    if count < 1:
        raise WorkerControlError("workers必须大于0")
    groups = [[] for _ in range(min(count, max(1, len(values))))]
    for index, value in enumerate(values):
        groups[index % len(groups)].append(value)
    return [group for group in groups if group]


def command_create_discovery(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    if state.get("batch_status") != "OPEN":
        raise WorkerControlError("只能为OPEN正式批次创建发现任务")
    plan = load_json(Path(args.plan).resolve())
    tasks = plan.get("search_tasks") if isinstance(plan, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise WorkerControlError("发现计划必须包含非空search_tasks数组")
    traversal_ids = {text(t.get("keyword_id")) for t in tasks
                     if t.get("query_purpose") == "BASE_KEYWORD_TRAVERSAL"}
    if len(traversal_ids) > policy_for(state)["base_keyword_batch_max"]:
        raise WorkerControlError("基础批次关键词数量超出绑定策略上限")
    assigned = set(state.get("assigned_keyword_ids", []))
    if traversal_ids and assigned and not traversal_ids <= assigned:
        raise WorkerControlError("基础遍历任务的keyword_id不属于当前批次领取的BK")
    is_system = plan.get("plan_type") == "SYSTEM_SEARCH" or any(t.get("query_purpose") == "SYSTEM_SEARCH" for t in tasks) or state.get("strategy_id") == "SYSTEM_SEARCH"
    if is_system:
        if not getattr(args, "city_state", ""):
            raise WorkerControlError("SYSTEM_SEARCH必须提供--city-state并使用受控计划")
        try:
            city = load_json(Path(args.city_state).resolve())
            same_binding(state, city)
            system_search.require_system_stage(city)
            system_search.validate_plan(city, plan)
            if state.get("round_id") != plan["round_id"]:
                raise system_search.PlanError("正式批次round_id与计划不符")
        except (ValueError, KeyError) as exc:
            raise WorkerControlError(str(exc)) from exc
    fallback_reason = text(getattr(args, "fallback_reason", "")).upper()
    if min(args.workers, len(tasks)) < 2 and not fallback_reason:
        raise WorkerControlError(
            "查询数 >= 2 时必须使用多 Agent；仅 1 个 Worker 时必须提供 --fallback-reason"
        )
    if fallback_reason and fallback_reason not in VALID_FALLBACK_REASONS:
        raise WorkerControlError(
            f"非法 fallback_reason：{fallback_reason}；合法值：{sorted(VALID_FALLBACK_REASONS)}"
        )
    required = {
        "search_task_id", "query_id", "query_text", "query_purpose",
        "keyword_family", "source_type",
    }
    task_ids: set[str] = set()
    query_ids: set[str] = set()
    normalized = []
    for item in tasks:
        if not isinstance(item, dict) or any(not text(item.get(key)) for key in required):
            raise WorkerControlError(f"发现任务缺少必填字段：{required}")
        task_id = text(item["search_task_id"])
        query_id = text(item["query_id"])
        if (task_id in task_ids and not is_system) or query_id in query_ids:
            raise WorkerControlError("搜索任务ID或查询ID重复")
        task_ids.add(task_id)
        query_ids.add(query_id)
        purpose = text(item["query_purpose"]).upper()
        if purpose == "BASE_KEYWORD_TRAVERSAL" and not text(item.get("keyword_id")):
            raise WorkerControlError(f"基础词遍历任务 {query_id} 缺少 keyword_id")
        if purpose == "EXPANSION_DISCOVERY":
            missing = [key for key in EXPANSION_PLAN_FIELDS if not item.get(key)]
            if missing or item.get("discovery_method") != "YIELD_EXPANSION":
                raise WorkerControlError(f"扩展发现任务 {query_id} 缺少溯源字段：{sorted(missing)}")
        item = dict(item)
        item["query_purpose"] = purpose
        normalized.append(dict(item))
    groups = split_evenly(normalized, args.workers)
    batch_id = text(state.get("batch_id"))
    workers = []
    for index, group in enumerate(groups, 1):
        workers.append({
            "research_task_id": f"SEARCH-{batch_id}-D{index:02d}",
            "assigned_search_task_ids": [text(item["search_task_id"]) for item in group],
            "assigned_query_ids": [text(item["query_id"]) for item in group],
            "search_tasks": group,
        })
    manifest = {
        "schema_version": 1,
        "task_type": "discovery",
        "b2b_brief": {"offering": profile_for(state)["offering"], "target": profile_for(state)["target"],
                      "fit_rules": profile_for(state)["fit_rules"], "exclude_rules": profile_for(state).get("exclude_rules", []),
                      "config_fingerprint": binding_for(state)["fingerprint"]},
        "status": "OPEN",
        "formal_state": str(state_path),
        "formal_batch_id": batch_id,
        "input_sha256": file_hash(state_path),
        "workers": workers,
        "fallback_reason": fallback_reason,
        "created_at": now(),
    }
    if is_system:
        manifest["system_search_plan"] = plan
    save_json(Path(args.manifest).resolve(), manifest)
    print(json.dumps({"task_type": "discovery", "worker_count": len(workers), "formal_batch_id": batch_id, "fallback_reason": fallback_reason}, ensure_ascii=False))


def command_create_verification(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state = load_json(state_path)
    active = state.get("active_workset")
    if state.get("batch_status") != "OPEN" or not isinstance(active, list) or not active:
        raise WorkerControlError("必须先领取非空活动工作集")
    item_map = {text(item.get("candidate_id")): item for item in state.get("candidates", [])}
    if any(cid not in item_map or item_map[cid].get("status") != "IN_WORKSET" for cid in active):
        raise WorkerControlError("活动工作集与候选状态不一致")
    fallback_reason = text(getattr(args, "fallback_reason", "")).upper()
    if min(args.workers, len(active)) < 2 and not fallback_reason:
        raise WorkerControlError(
            "工作集候选数 >= 2 时必须使用多 Agent；仅 1 个 Worker 时必须提供 --fallback-reason"
        )
    if fallback_reason and fallback_reason not in VALID_FALLBACK_REASONS:
        raise WorkerControlError(
            f"非法 fallback_reason：{fallback_reason}；合法值：{sorted(VALID_FALLBACK_REASONS)}"
        )
    groups = split_evenly(active, args.workers)
    batch_id = text(state.get("batch_id"))
    workers = []
    for index, group in enumerate(groups, 1):
        workers.append({
            "research_task_id": f"VERIFY-{batch_id}-W{index:02d}",
            "candidate_ids": group,
            "candidates": [item_map[cid] for cid in group],
        })
    manifest = {
        "schema_version": 1,
        "task_type": "verification",
        "b2b_brief": {"offering": profile_for(state)["offering"], "target": profile_for(state)["target"],
                      "fit_rules": profile_for(state)["fit_rules"], "config_fingerprint": binding_for(state)["fingerprint"]},
        "status": "OPEN",
        "formal_state": str(state_path),
        "formal_batch_id": batch_id,
        "input_sha256": file_hash(state_path),
        "active_workset": active,
        "workers": workers,
        "fallback_reason": fallback_reason,
        "created_at": now(),
    }
    save_json(Path(args.manifest).resolve(), manifest)
    print(json.dumps({"task_type": "verification", "worker_count": len(workers), "candidate_count": len(active), "fallback_reason": fallback_reason}, ensure_ascii=False))


def command_export(args: argparse.Namespace) -> None:
    manifest, _ = current_manifest(args)
    worker = next((item for item in manifest["workers"] if item["research_task_id"] == args.task_id), None)
    if not worker:
        raise WorkerControlError(f"未找到子任务：{args.task_id}")
    package_id = task_package_id(manifest, worker)
    payload = {
        "task_type": manifest["task_type"],
        "b2b_brief": manifest.get("b2b_brief", {}),
        "task_package_id": package_id,
        "research_task_id": worker["research_task_id"],
        "formal_batch_id": manifest["formal_batch_id"],
        "input_version": manifest["input_sha256"],
        **{key: value for key, value in worker.items() if key != "research_task_id"},
    }
    requested = Path(args.output).resolve()
    output_dir = requested if requested.exists() and requested.is_dir() else requested.parent
    safe_task_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-"
                           for ch in worker["research_task_id"])
    output_path = output_dir / f"{safe_task_id}-{package_id[:12]}.task.json"
    save_task_package(output_path, payload)
    print(json.dumps({
        "exported": True,
        "task_type": manifest["task_type"],
        "research_task_id": worker["research_task_id"],
        "task_package_id": package_id,
        "output": str(output_path),
    }, ensure_ascii=False))


def validate_envelope(manifest: dict[str, Any], worker: dict[str, Any], result: dict[str, Any]) -> None:
    if text(result.get("task_type")) != manifest["task_type"]:
        raise WorkerControlError("结果task_type与任务类型不一致")
    if text(result.get("task_package_id")) != task_package_id(manifest, worker):
        raise WorkerControlError("结果task_package_id与当前任务包不一致")
    if text(result.get("research_task_id")) != worker["research_task_id"]:
        raise WorkerControlError("结果research_task_id与分配任务不一致")
    if text(result.get("formal_batch_id")) != manifest["formal_batch_id"]:
        raise WorkerControlError("结果formal_batch_id与正式批次不一致")
    if text(result.get("input_version")) != manifest["input_sha256"]:
        raise WorkerControlError("结果input_version已过期")


def validate_discovery(worker: dict[str, Any], result: dict[str, Any]) -> None:
    allowed_queries = set(worker["assigned_query_ids"])
    queries = result.get("queries")
    candidates = result.get("candidates")
    if not isinstance(queries, list) or not isinstance(candidates, list):
        raise WorkerControlError("发现结果必须包含queries和candidates数组")
    returned_queries = []
    planned_by_query = {text(item["query_id"]): item for item in worker.get("search_tasks", [])}
    for item in queries:
        if not isinstance(item, dict):
            raise WorkerControlError("查询记录必须为对象")
        query_id = text(item.get("query_id"))
        if set(item) - DISCOVERY_QUERY_KEYS:
            raise WorkerControlError(f"查询记录返回越权字段：{sorted(set(item) - DISCOVERY_QUERY_KEYS)}")
        if query_id not in allowed_queries or query_id in returned_queries:
            raise WorkerControlError("查询ID越界或重复")
        termination_reason = text(item.get("termination_reason")).upper()
        count_fields = ("results_examined", "unique_candidates", "duplicate_candidates")
        if not text(item.get("query_text")) or any(
            not isinstance(item.get(field), int)
            or isinstance(item.get(field), bool)
            or item[field] < 0
            for field in count_fields
        ):
            raise WorkerControlError("查询记录缺少query_text或合法的非负整数统计字段")
        if item.get("query_text") != planned_by_query[query_id].get("query_text"):
            raise WorkerControlError(f"查询 {query_id} 的query_text与分配任务不一致")
        urls = item.get("visited_source_urls", [])
        if not isinstance(urls, list) or any(
            not isinstance(url, str) or not url.startswith(("http://", "https://"))
            for url in urls
        ):
            raise WorkerControlError("visited_source_urls必须是HTTP(S) URL数组")
        item["visited_source_urls"] = urls
        if termination_reason not in VALID_TERMINATION_REASONS:
            raise WorkerControlError(
                f"查询 {query_id} 缺少合法termination_reason："
                f"{sorted(VALID_TERMINATION_REASONS)}"
            )
        item["termination_reason"] = termination_reason
        problem = execution_problem(item)
        if problem and termination_reason in {"BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED"}:
            raise WorkerControlError(f"查询 {query_id} 统计或终止状态不合规：{problem}")
        returned_queries.append(query_id)
    if set(returned_queries) != allowed_queries:
        raise WorkerControlError("发现结果未覆盖子任务的全部查询")
    for item in candidates:
        if not isinstance(item, dict):
            raise WorkerControlError("候选记录必须为对象")
        if set(item) - DISCOVERY_CANDIDATE_KEYS:
            raise WorkerControlError(f"候选记录返回越权字段：{sorted(set(item) - DISCOVERY_CANDIDATE_KEYS)}")
        required = ("company_name", "source_url", "discovery_query_id", "discovery_query", "preliminary_role", "discovery_reason", "lead_summary")
        if any(not text(item.get(key)) for key in required) or not unique(item.get("matched_keywords")):
            raise WorkerControlError("候选记录缺少必填来源字段")
        if text(item["discovery_query_id"]) not in allowed_queries:
            raise WorkerControlError("候选引用了未分配查询")
        planned = planned_by_query[text(item["discovery_query_id"])]
        if item.get("discovery_query") != planned.get("query_text"):
            raise WorkerControlError(
                f"候选 {text(item.get('company_name'))} 的discovery_query与分配任务不一致"
            )
        if text(planned.get("query_purpose")).upper() == "EXPANSION_DISCOVERY":
            for key in EXPANSION_PLAN_FIELDS | {"discovery_method"}:
                if item.get(key) != planned.get(key):
                    raise WorkerControlError(f"扩展候选不得修改或遗漏计划字段：{key}")
            if not text(item.get("similarity_basis")):
                raise WorkerControlError("扩展候选缺少自身similarity_basis")
    for query in queries:
        count = sum(item.get("discovery_query_id") == query["query_id"] for item in candidates)
        if query["unique_candidates"] + query["duplicate_candidates"] != count:
            raise WorkerControlError("查询企业线索统计与逐条发现记录不一致；重复企业也须保留来源记录")


def validate_verification(worker: dict[str, Any], result: dict[str, Any]) -> None:
    records = result.get("results")
    if not isinstance(records, list):
        raise WorkerControlError("核验结果缺少results数组")
    expected = set(worker["candidate_ids"])
    returned = [text(item.get("candidate_id")) for item in records if isinstance(item, dict)]
    if len(returned) != len(set(returned)) or set(returned) != expected:
        raise WorkerControlError("核验结果与分配candidate_id不完全一致")
    for item in records:
        validate_verification_record(item, status_key="recommended_status")


def normalize_verification_record(item):
    item = copy.deepcopy(item)
    if 'candidate_id' in item:
        item['candidate_id'] = text(item['candidate_id'])
    for key in ('status', 'recommended_status', 'business_fit', 'followup_reason', 'website_verification_status'):
        if key in item:
            item[key] = text(item[key]).upper()
    for key in ('official_website', 'website_evidence_ref', 'website_entity_match_note', 'followup_reason'):
        item.setdefault(key, '')
    return item


def validate_verification_record(item, *, status_key, profile=None, city=""):
    item = normalize_verification_record(item)
    allowed = (VERIFICATION_RESULT_KEYS - {"recommended_status"}) | {status_key}
    if set(item) - allowed:
        raise WorkerControlError(f"{item.get('candidate_id')}返回越权字段：{sorted(set(item) - allowed)}")
    status = text(item.get(status_key)).upper()
    if not text(item.get("note")):
        raise WorkerControlError("核验结果缺少分流说明")
    try:
        qualify(status, text(item.get("business_fit")), item.get("prospect_fit_evidence"),
                text(item.get("followup_reason")).upper(), profile, city)
    except QualificationError as exc:
        raise WorkerControlError(str(exc)) from exc
    website_status = text(item.get("website_verification_status")).upper()
    website = text(item.get("official_website"))
    website_ref = text(item.get("website_evidence_ref"))
    if website_status not in WEBSITE_STATUSES:
        raise WorkerControlError("website_verification_status必须为VERIFIED、NOT_FOUND或CONFLICT")
    if website_status == "VERIFIED" and (not website.startswith(("http://", "https://"))
                                         or not website_ref.startswith(("http://", "https://"))
                                         or not text(item.get("website_entity_match_note"))):
        raise WorkerControlError("官网VERIFIED必须提供官网URL、证据URL和主体匹配说明")
    if website_status != "VERIFIED" and website:
        raise WorkerControlError("官网未核验通过时official_website必须为空")


def validate_multi_agent_execution(
    manifest: dict[str, Any],
    result_paths: list[str],
    fallback_reason: str = "",
) -> None:
    """校验多 Agent 执行记录完整性。

    规则：
    - 至少两个不同的 research_task_id（即 workers >= 2），或存在合法 fallback_reason；
    - 每个任务对应独立 Worker 结果文件；
    - 所有分配查询均已返回（由 load_and_validate_results 保证）；
    - 主 Agent 没有伪造单个结果覆盖多个 Worker（结果文件数必须等于 workers 数）。
    """
    workers = manifest.get("workers", [])
    worker_count = len(workers)
    task_ids = [text(w.get("research_task_id")) for w in workers]
    if len(set(task_ids)) != worker_count:
        raise WorkerControlError("Worker manifest 中 research_task_id 存在重复")
    if worker_count < 2:
        reason = text(fallback_reason).upper()
        if not reason:
            raise WorkerControlError(
                "仅 1 个 Worker 时必须提供合法 fallback_reason；禁止主 Agent 无降级原因单 Agent 执行"
            )
        if reason not in VALID_FALLBACK_REASONS:
            raise WorkerControlError(
                f"非法 fallback_reason：{reason}；合法值：{sorted(VALID_FALLBACK_REASONS)}"
            )
    if len(result_paths) != worker_count:
        raise WorkerControlError(
            f"结果文件数量({len(result_paths)})与 Worker 数量({worker_count})不一致；"
            "禁止主 Agent 伪造单个结果覆盖多个 Worker"
        )


def load_and_validate_results(manifest: dict[str, Any], result_paths: list[str]) -> list[dict[str, Any]]:
    if len(result_paths) != len(manifest["workers"]):
        raise WorkerControlError("结果文件数量必须与子任务数量一致")
    worker_map = {item["research_task_id"]: item for item in manifest["workers"]}
    seen: set[str] = set()
    results = []
    for raw_path in result_paths:
        result = load_json(Path(raw_path).resolve())
        task_id = text(result.get("research_task_id")) if isinstance(result, dict) else ""
        if task_id not in worker_map or task_id in seen:
            raise WorkerControlError("结果子任务ID未分配或重复")
        validate_envelope(manifest, worker_map[task_id], result)
        (validate_discovery if manifest["task_type"] == "discovery" else validate_verification)(worker_map[task_id], result)
        seen.add(task_id)
        results.append(result)
    if seen != set(worker_map):
        raise WorkerControlError("子任务结果存在遗漏")
    return results


def json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def execution_problem(query: dict) -> str:
    """Lightweight execution sanity checks without per-result proof records."""
    if query.get("termination_reason") not in {"BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED"}:
        return "STOP_NOT_QUALIFYING"
    fields = ("results_examined", "unique_candidates", "duplicate_candidates")
    if any(not isinstance(query.get(field), int) or isinstance(query.get(field), bool)
           or query[field] < 0 for field in fields):
        return "INVALID_QUERY_COUNTS"
    if query["unique_candidates"] + query["duplicate_candidates"] > query["results_examined"]:
        return "CANDIDATE_COUNT_EXCEEDS_RESULTS"
    return ""


def provenance_fields(provenance: list) -> dict:
    """Legacy arrays are projections only, never independently editable sources."""
    return {
        "matched_keywords": unique([v for p in provenance for v in p.get("matched_keywords", [])]),
        "discovery_queries": unique([p["query_text"] for p in provenance]),
        "discovered_by_query_ids": unique([p["query_id"] for p in provenance]),
        "discovery_task_ids": unique([p["task_id"] for p in provenance]),
        "keyword_families": unique([p["keyword_family"] for p in provenance]),
    }


def load_validated_discovery(path: Path) -> dict:
    payload = load_json(path)
    receipt = payload.get("validation", {})
    core = {k: v for k, v in payload.items() if k != "validation"}
    if receipt.get("payload_sha256") != json_hash(core):
        raise WorkerControlError("发现合并结果缺少校验凭据或内容已变化")
    manifest_path = Path(receipt.get("manifest_path", ""))
    if not manifest_path.is_file() or file_hash(manifest_path) != receipt.get("manifest_sha256"):
        raise WorkerControlError("发现任务清单无法反查或已变化")
    manifest = load_json(manifest_path)
    paths = []
    for entry in receipt.get("results", []):
        result_path = Path(entry["path"])
        if not result_path.is_file() or file_hash(result_path) != entry.get("sha256"):
            raise WorkerControlError("原始Worker结果无法反查或已变化")
        paths.append(str(result_path))
    if manifest.get("task_type") != "discovery" or manifest.get("status") == "INVALIDATED":
        raise WorkerControlError("不是有效发现任务")
    validate_multi_agent_execution(manifest, paths, receipt.get("fallback_reason", ""))
    results = load_and_validate_results(manifest, paths)
    if build_discovery_payload(manifest, results) != core:
        raise WorkerControlError("发现合并结果与已校验原始记录不一致")
    return payload


def load_validated_verification(path: Path, state_path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    receipt = payload.get("validation", {}) if isinstance(payload, dict) else {}
    core = {k: v for k, v in payload.items() if k != "validation"} if isinstance(payload, dict) else {}
    if receipt.get("payload_sha256") != json_hash(core):
        raise WorkerControlError("核验合并结果缺少校验凭据或内容已变化")
    manifest_path = Path(receipt.get("manifest_path", ""))
    if not manifest_path.is_file() or file_hash(manifest_path) != receipt.get("manifest_sha256"):
        raise WorkerControlError("核验任务清单无法反查或已变化")
    manifest = load_json(manifest_path)
    if manifest.get("task_type") != "verification" or manifest.get("status") == "INVALIDATED":
        raise WorkerControlError("不是有效核验任务")
    if Path(manifest.get("formal_state", "")).resolve() != state_path.resolve():
        raise WorkerControlError("核验任务清单不属于当前正式批次")
    if file_hash(state_path) != manifest.get("input_sha256"):
        raise WorkerControlError("正式批次已变化，核验合并结果已过期")
    paths = []
    for entry in receipt.get("results", []):
        result_path = Path(entry.get("path", ""))
        if not result_path.is_file() or file_hash(result_path) != entry.get("sha256"):
            raise WorkerControlError("核验Worker原始结果无法反查或已变化")
        paths.append(str(result_path))
    validate_multi_agent_execution(manifest, paths, receipt.get("fallback_reason", ""))
    results = load_and_validate_results(manifest, paths)
    records = []
    for result in results:
        for item in result["results"]:
            converted = dict(item)
            converted["status"] = text(converted.pop("recommended_status")).upper()
            records.append(converted)
    record_map = {text(item["candidate_id"]): item for item in records}
    rebuilt = {"batch_id": manifest["formal_batch_id"],
               "results": [record_map[cid] for cid in manifest["active_workset"]]}
    if rebuilt != core:
        raise WorkerControlError("核验合并结果与已校验原始记录不一致")
    state = load_json(state_path)
    for item in rebuilt["results"]:
        validate_verification_record(item, status_key="status", profile=profile_for(state), city=state.get("city", ""))
    return rebuilt["results"]


def command_validate_result(args: argparse.Namespace) -> None:
    manifest, _ = current_manifest(args)
    result = load_json(Path(args.result).resolve())
    task_id = text(result.get("research_task_id")) if isinstance(result, dict) else ""
    worker = next((item for item in manifest["workers"] if item["research_task_id"] == task_id), None)
    if not worker:
        raise WorkerControlError("结果不属于任务清单")
    validate_envelope(manifest, worker, result)
    (validate_discovery if manifest["task_type"] == "discovery" else validate_verification)(worker, result)
    print(json.dumps({"valid": True, "research_task_id": task_id}, ensure_ascii=False))


def build_discovery_payload(manifest: dict, results: list) -> dict:
    planned_queries = {
        text(task["query_id"]): task
        for worker in manifest["workers"]
        for task in worker.get("search_tasks", [])
    }
    queries = []
    for result in results:
        for returned in result["queries"]:
            query_id = text(returned["query_id"])
            planned = planned_queries[query_id]
            enriched = dict(returned)
            enriched.update({
                "query_purpose": text(planned["query_purpose"]).upper(),
                "keyword_id": text(planned.get("keyword_id")),
                "task_id": text(planned["search_task_id"]),
                "source_type": text(planned["source_type"]),
                "keyword_family": text(planned["keyword_family"]),
                **{key: planned.get(key) for key in EXPANSION_PLAN_FIELDS | {"discovery_method"} if key in planned},
                **{key: planned[key] for key in system_search.FIELDS if key in planned},
                **{key: planned[key] for key in ("variant_key", "deepening_reason", "search_intent", "variant_mode", "variant_id", "variant_template_version", "combination_term", "variant_context") if key in planned},
                **{key: planned.get(key, "") for key in ("district", "role", "industry")},
            })
            queries.append(enriched)
    merged: dict[str, dict[str, Any]] = {}
    discovery_records = []
    for result in results:
        for position, item in enumerate(result["candidates"]):
            planned = planned_queries[item["discovery_query_id"]]
            record_id = "DR-" + json_hash([manifest["formal_batch_id"], manifest["input_sha256"], result["research_task_id"], position, item])[:24]
            provenance = {"record_id": record_id, "company_name": item["company_name"],
                "query_id": item["discovery_query_id"], "query_text": item["discovery_query"],
                "source_url": item["source_url"], "task_id": planned["search_task_id"],
                "keyword_family": planned["keyword_family"], "matched_keywords": unique(item["matched_keywords"])}
            discovery_records.append({**item, "record_id": record_id, "provenance": [provenance]})
            key = normalize_company(item["company_name"])
            if not key:
                raise WorkerControlError("候选企业名规范化后为空")
            if key not in merged:
                merged[key] = dict(item)
                merged[key]["provenance"] = [provenance]
                merged[key]["discovered_by_query_ids"] = [text(item["discovery_query_id"])]
                merged[key]["discovery_queries"] = [text(item["discovery_query"])]
            else:
                current = merged[key]
                current["provenance"].append(provenance)
                current["matched_keywords"] = list(dict.fromkeys(unique(current.get("matched_keywords")) + unique(item.get("matched_keywords"))))
                current["discovered_by_query_ids"] = list(dict.fromkeys(current["discovered_by_query_ids"] + [text(item["discovery_query_id"])]))
                current["discovery_queries"] = list(dict.fromkeys(current["discovery_queries"] + [text(item["discovery_query"])]))
                trace_fields = EXPANSION_PLAN_FIELDS | {"discovery_method", "seed_role", "similarity_basis"}
                for field in trace_fields:
                    if field in item and field not in current:
                        current[field] = item[field]
    payload = {
        "formal_batch_id": manifest["formal_batch_id"],
        "input_version": manifest["input_sha256"],
        "queries": queries,
        "discovery_records": discovery_records,
        "candidates": list(merged.values()),
        "note": "仅完成跨子任务规范化去重；主控仍须执行全局去重、主体核验和ID分配。",
    }
    if "system_search_plan" in manifest:
        payload["system_search_plan"] = manifest["system_search_plan"]
    return payload


def command_merge(args: argparse.Namespace) -> None:
    manifest, _ = current_manifest(args)
    fallback_reason = text(manifest.get("fallback_reason", "")).upper() or text(
        getattr(args, "fallback_reason", "")
    ).upper()
    validate_multi_agent_execution(manifest, args.result, fallback_reason)
    results = load_and_validate_results(manifest, args.result)
    if manifest["task_type"] == "verification":
        records = []
        for result in results:
            for item in result["results"]:
                converted = dict(item)
                converted["status"] = text(converted.pop("recommended_status")).upper()
                records.append(converted)
        expected = manifest["active_workset"]
        record_map = {text(item["candidate_id"]): item for item in records}
        payload = {"batch_id": manifest["formal_batch_id"], "results": [record_map[cid] for cid in expected]}
    else:
        payload = build_discovery_payload(manifest, results)
    payload["validation"] = {
        "manifest_path": str(Path(args.manifest).resolve()),
        "manifest_sha256": file_hash(Path(args.manifest).resolve()),
        "results": [{"path": str(Path(p).resolve()), "sha256": file_hash(Path(p).resolve())} for p in args.result],
        "fallback_reason": fallback_reason,
        "payload_sha256": json_hash(payload),
    }
    save_json(Path(args.output).resolve(), payload)
    print(json.dumps({"merged": True, "task_type": manifest["task_type"], "output": str(Path(args.output).resolve())}, ensure_ascii=False))


def command_status(args: argparse.Namespace) -> None:
    manifest = load_json(Path(args.manifest).resolve())
    print(json.dumps({
        "status": manifest.get("status"),
        "task_type": manifest.get("task_type"),
        "formal_batch_id": manifest.get("formal_batch_id"),
        "research_task_ids": [item.get("research_task_id") for item in manifest.get("workers", [])],
    }, ensure_ascii=False, indent=2))


def command_invalidate(args: argparse.Namespace) -> None:
    path = Path(args.manifest).resolve()
    manifest = load_json(path)
    manifest["status"] = "INVALIDATED"
    manifest["invalidated_at"] = now()
    manifest["invalidation_reason"] = text(args.reason)
    save_json(path, manifest)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检索阶段研究子任务切分、校验和合并")
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("create-discovery")
    discovery.add_argument("--state", required=True); discovery.add_argument("--plan", required=True)
    discovery.add_argument("--city-state", default="", help="SYSTEM_SEARCH受控计划的城市状态")
    discovery.add_argument("--workers", type=int, required=True); discovery.add_argument("--manifest", required=True)
    discovery.add_argument("--fallback-reason", choices=sorted(VALID_FALLBACK_REASONS), default="",
                           help="单 Agent 降级原因；workers<2 时必填")
    discovery.set_defaults(func=command_create_discovery)
    verification = sub.add_parser("create-verification")
    verification.add_argument("--state", required=True); verification.add_argument("--workers", type=int, required=True)
    verification.add_argument("--manifest", required=True)
    verification.add_argument("--fallback-reason", choices=sorted(VALID_FALLBACK_REASONS), default="",
                              help="单 Agent 降级原因；workers<2 时必填")
    verification.set_defaults(func=command_create_verification)
    export = sub.add_parser("export")
    export.add_argument("--manifest", required=True); export.add_argument("--task-id", required=True); export.add_argument("--output", required=True)
    export.set_defaults(func=command_export)
    validate = sub.add_parser("validate-result")
    validate.add_argument("--manifest", required=True); validate.add_argument("--result", required=True); validate.set_defaults(func=command_validate_result)
    merge = sub.add_parser("merge")
    merge.add_argument("--manifest", required=True); merge.add_argument("--result", action="append", required=True); merge.add_argument("--output", required=True)
    merge.add_argument("--fallback-reason", choices=sorted(VALID_FALLBACK_REASONS), default="",
                       help="单 Agent 降级原因；manifest 未记录时可在此补登")
    merge.set_defaults(func=command_merge)
    status = sub.add_parser("status"); status.add_argument("--manifest", required=True); status.set_defaults(func=command_status)
    invalidate = sub.add_parser("invalidate")
    invalidate.add_argument("--manifest", required=True); invalidate.add_argument("--reason", required=True); invalidate.set_defaults(func=command_invalidate)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except (WorkerControlError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"检索子任务控制失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
