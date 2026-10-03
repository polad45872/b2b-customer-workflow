#!/usr/bin/env python3
"""B2B潜客批次与上下文状态机。仅使用 Python 标准库。

写操作（init/add/start-workset/complete/carryover/close）必须提供 --caller-token，
并通过 state_guard 进行令牌校验、flock 排他锁和只读文件保护。
读操作（status）不需要令牌。
"""

from __future__ import annotations

from b2b_qualification import seed_eligible, validate_qualification as qualify, QualificationError

from b2b_config import policy_for, check_if_bound, asset_path, profile_for, binding_for, same_binding, bind_profile, ConfigError

import argparse
import hashlib
import copy
import search_worker_control as discovery
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from state_guard import StateGuardError, protected_write, verify_caller_token


MODES = {"search", "expansion"}
SHORTFALL_REASON_CODES = {"BUDGET_EXHAUSTED", "SOURCE_BLOCKED", "MARKET_SATURATED", "STRATEGY_STOP"}

TERMINAL_STATUSES = {"FINAL", "FOLLOWUP", "EXCLUDED"}
FOLLOWUP_REASONS = {
    "IDENTITY_PENDING", "LOCATION_PENDING", "BUSINESS_FIT_PENDING",
    "QUALIFYING_EVIDENCE_MISSING", "SOURCE_BLOCKED",
}
ALL_STATUSES = {"UNVERIFIED", "IN_WORKSET", "CARRYOVER", *TERMINAL_STATUSES}


class ControlError(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state(path: Path) -> dict:
    if not path.exists():
        raise ControlError(f"状态文件不存在：{path}")
    try:
        return check_if_bound(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"无法读取状态文件：{exc}") from exc


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now()
    with protected_write(path):
        payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
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


def limits_for(state: dict) -> dict:
    return policy_for(state)["limits"][state["mode"]]


def candidates_by_status(state: dict, status: str) -> list[dict]:
    return [item for item in state["candidates"] if item["status"] == status]


def find_candidate(state: dict, candidate_id: str) -> dict:
    for item in state["candidates"]:
        if item["candidate_id"] == candidate_id:
            return item
    raise ControlError(f"未找到候选：{candidate_id}")


def ensure_open(state: dict) -> None:
    binding_for(state)
    if state["batch_status"] != "OPEN":
        raise ControlError(f"批次状态为 {state['batch_status']}，禁止继续修改")


def require_token(args: argparse.Namespace) -> None:
    """校验调用方令牌。所有写命令必须在执行前调用。"""
    verify_caller_token(
        getattr(args, "caller_token", ""),
        state_path=getattr(args, "state", None),
    )


def dedupe_key(value: str) -> str:
    return "".join(value.casefold().split())


def find_existing_name_match(company_name: str, index: dict) -> tuple[str, str]:
    """Match exact names or a complete existing legal name embedded in an alias."""
    key = dedupe_key(company_name)
    exact = next((cid for cid, item in index.items()
                  if dedupe_key(item.get("dedupe_key") or item.get("company_name", "")) == key), "")
    if exact:
        return exact, "规范化企业名称与全局主体索引精确一致"
    legal_suffixes = ("有限责任公司", "股份有限公司", "有限公司", "合伙企业")
    matches = []
    for cid, item in index.items():
        existing = dedupe_key(item.get("company_name") or item.get("normalized_name") or "")
        if len(existing) < 8 or not existing.endswith(legal_suffixes):
            continue
        if existing in key:
            matches.append(cid)
    if len(matches) == 1:
        return matches[0], "线索名称中包含既有主体完整法定名称"
    return "", ""


def next_candidate_id(state: dict) -> str:
    used = {str(item.get("candidate_id", "")) for item in _registry(state).values()}
    used.update(str(x) for x in state.get("candidate_id_reservations", []))
    for event in state.get("events", []):
        if event.get("action") == "RECLASSIFY_DUPLICATE":
            used.add(str(event.get("removed_candidate_id", "")))
    numbers = [int(x[7:]) for x in used if x.startswith("AUTO-C") and x[7:].isdigit()]
    number = max(numbers, default=0) + 1
    return f"AUTO-C{number:06d}"


def unique_strings(values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


VALID_FALLBACK_REASONS = {
    "SINGLE_QUERY", "SINGLE_CANDIDATE", "SINGLE_EXPANSION_PLAN", "PLATFORM_UNSUPPORTED", "WORKER_UNAVAILABLE",
}



def validate_qualification(status, business_fit, scene, followup_reason="", profile=None, city=""):
    try:
        qualify(status, business_fit, scene, followup_reason, profile, city)
    except QualificationError as exc:
        raise ControlError(str(exc)) from exc


def inherited_context(previous: dict) -> dict:
    carryover = []
    for item in previous.get("candidates", []):
        if item.get("status") == "CARRYOVER":
            copied = dict(item)
            copied.update(status="UNVERIFIED", result_note="")
            carryover.append(copied)
    keep_ids = {p["record_id"] for c in carryover for p in c.get("provenance", [])}
    keep_ids.update(rid for rid, row in previous.get("discovery_records", {}).items() if row.get("disposition") == "PENDING")
    inherited_records = {rid: copy.deepcopy(row) for rid, row in previous.get("discovery_records", {}).items() if rid in keep_ids}
    input_hashes = {row["result_sha256"] for row in inherited_records.values()}
    scheduled = previous.get("next_strategy", {})
    scheduled_id = scheduled.get("strategy_id", "") if isinstance(scheduled, dict) else str(scheduled)
    return {
        "b2b_config": copy.deepcopy(binding_for(previous)),
        "city": previous.get("city", ""),
        "candidates": carryover,
        "discovery_records": inherited_records,
        "discovery_inputs": {h: copy.deepcopy(v) for h, v in previous.get("discovery_inputs", {}).items() if h in input_hashes},
        "global_candidate_index": _registry(previous),
        "candidate_id_reservations": list(dict.fromkeys(previous.get("candidate_id_reservations", []) + [
            event.get("removed_candidate_id") for event in previous.get("events", [])
            if event.get("action") == "RECLASSIFY_DUPLICATE" and event.get("removed_candidate_id")
        ])),
        "global_dedupe_keys": list(dict.fromkeys(previous.get("global_dedupe_keys", []))),
        "round_id": previous.get("round_id", ""),
        "strategy_id": scheduled_id or previous.get("strategy_id", ""),
        "query_budget": previous.get("remaining_query_budget", previous.get("query_budget", 0)),
        "queries_executed": 0,
        "coverage_gaps": previous.get("coverage_gaps", []),
        "next_strategy": previous.get("next_strategy", {}),
    }


def generate_next_round_id(rounds: list[dict]) -> str:
    """从已有轮次中找到最大编号，生成下一个 round_id（R001 → R002）。"""
    max_num = 0
    for r in rounds:
        rid = str(r.get("round_id", "")).strip()
        if rid.startswith("R") and rid[1:].isdigit():
            max_num = max(max_num, int(rid[1:]))
    return f"R{max_num + 1:03d}"


def determine_next_round_id(
    city_state_path: Path | None,
    current_round_id: str,
    coverage_result: dict | None,
    round_status_arg: str,
) -> str:
    """
    判断下一批次应继承旧 round_id 还是生成新 round_id。

    规则：
    - 当前轮次预算尚未完成、仍需拆批核验：继承同一个 round_id
    - 当前轮次已经 COMPLETED 或 EARLY_STOPPED，且城市脚本要求开始新的 SYSTEM_SEARCH：生成新的 round_id
    """
    if not city_state_path or not coverage_result:
        return current_round_id
    try:
        city_state = load_state(city_state_path)
    except (OSError, ValueError):
        return current_round_id

    rounds = city_state.get("rounds", [])
    current_round = None
    for r in rounds:
        if str(r.get("round_id", "")).strip() == current_round_id.strip():
            current_round = r
            break

    round_finished = False
    if current_round:
        round_finished = current_round.get("round_status") in {"COMPLETED", "EARLY_STOPPED"}
    elif round_status_arg:
        round_finished = round_status_arg.upper() in {"COMPLETED", "EARLY_STOPPED"}

    if round_finished:
        return generate_next_round_id(rounds)

    return current_round_id


def mode_for_next_strategy(current_mode: str, coverage_result: dict | None) -> str:
    if not coverage_result:
        return current_mode
    strategy = coverage_result.get("next_strategy")
    if strategy == "YIELD_EXPANSION":
        return "expansion"
    if strategy in {"BASE_KEYWORD_TRAVERSAL", "KEYWORD_DEEPENING", "MANDATORY_COVERAGE", "SYSTEM_SEARCH", "REPLAY_APPROVED_KEYWORDS", "TEST_DISCOVERED_KEYWORDS"}:
        return "search"
    return current_mode


def command_init(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    if path.exists() and not args.force:
        raise ControlError(f"状态文件已存在：{path}；如需覆盖请显式使用 --force")
    inherited_final_queue = []
    expansion_gate = None
    if args.mode == "expansion":
        if not args.city_state:
            raise ControlError("初始化 expansion 批次必须提供 --city-state")
        try:
            from city_coverage_control import calculate

            city_state = load_state(Path(args.city_state))
            expansion_gate = calculate(city_state)
        except (ImportError, KeyError, OSError, ValueError) as exc:
            raise ControlError(f"无法校验 expansion 门禁：{exc}") from exc
        if not any(seed_eligible(c) for c in city_state.get("candidates", [])):
            raise ControlError("初始化 expansion 需要城市状态中已有具可追溯线索的种子")
        if expansion_gate.get("next_strategy") != "YIELD_EXPANSION":
            raise ControlError(
                "禁止初始化 expansion：当前批次尚未轮到 YIELD_EXPANSION；"
                f"当前为 {expansion_gate.get('next_strategy')}"
            )
    context = {"candidates": [], "global_dedupe_keys": [], "round_id": args.round_id,
               "strategy_id": args.strategy_id, "query_budget": args.query_budget,
               "queries_executed": 0, "coverage_gaps": [], "next_strategy": {}}
    if args.previous_state:
        previous_path = Path(args.previous_state)
        previous = load_state(previous_path)
        if previous["batch_status"] != "CLOSED":
            raise ControlError(f"上一批次尚未关闭：{previous_path}")
        same_round = not args.round_id or args.round_id == previous.get("round_id", "")
        if same_round and previous.get("mode") != args.mode:
            raise ControlError("同一发现轮次的批次模式必须一致")
        inherited_final_queue = previous.get("pending_final_queue", [])
        inherited = inherited_context(previous)
        if (inherited["candidates"] or any(row.get("disposition") == "PENDING"
                                            for row in inherited["discovery_records"].values())):
            if args.mode != previous.get("mode") or (args.round_id and args.round_id != previous.get("round_id")):
                raise ControlError("尚有待处置结转，下一批必须沿用原模式和轮次")
        if same_round:
            context.update(inherited)
        else:
            context.update({
                "candidates": inherited["candidates"],
                "global_dedupe_keys": inherited["global_dedupe_keys"],
                "coverage_gaps": inherited["coverage_gaps"],
                **{k: inherited[k] for k in ("discovery_records", "discovery_inputs", "global_candidate_index", "candidate_id_reservations")},
            })

    state = {
        "schema_version": 6,
        "batch_id": args.batch_id,
        "mode": args.mode,
        "batch_status": "OPEN",
        "active_workset": [],
        **context,
        "inherited_final_queue": inherited_final_queue,
        "delivery_queue": [],
        "pending_final_queue": [],
        "events": [{
            "time": now(),
            "action": "INIT",
            "mode": args.mode,
            "previous_state": args.previous_state or "",
            "city_state": args.city_state or "",
            "expansion_gate_checked": bool(expansion_gate),
            "inherited_final_count": len(inherited_final_queue),
        }],
        "created_at": now(),
        "updated_at": now(),
    }
    if args.city_state:
        known_city = load_state(Path(args.city_state))
        state["b2b_config"] = copy.deepcopy(binding_for(known_city))
        state["city"] = known_city["city"]
        if args.previous_state:
            same_binding(previous, known_city)
        state["global_candidate_index"] = {**_registry(known_city), **_registry(state)}
        state["candidate_id_reservations"] = list(dict.fromkeys(
            known_city.get("candidate_id_reservations", []) + state.get("candidate_id_reservations", [])
        ))
        if args.mode == "search":
            from base_keyword_traversal_control import traversal_records
            from city_coverage_control import load_authoritative_base_keywords
            missing, _ = traversal_records(known_city, load_authoritative_base_keywords(asset_path(known_city, "keywords")))
            state["assigned_keyword_ids"] = [item["keyword_id"] for item in missing[:policy_for(state)["base_keyword_batch_max"]]]
        else:
            state["assigned_keyword_ids"] = []
    if not args.city_state:
        raise ControlError("B2B批次必须提供--city-state，以继承目标客户配置")
    path.parent.mkdir(parents=True, exist_ok=True)
    save_state(path, state)
    print(f"已初始化 {args.mode} 批次 {args.batch_id}：{path}")


def init_next_state(path: Path, batch_id: str, mode: str, previous_path: Path, round_id: str = "", city_state_path: Path | None = None) -> None:
    if path.exists():
        existing = load_state(path)
        same_binding(existing, load_state(previous_path))
        if existing.get("batch_id") == batch_id and existing.get("batch_status") == "OPEN":
            return
        raise ControlError(f"下一批状态文件已存在且不匹配：{path}")
    previous = load_state(previous_path)
    if previous.get("batch_status") != "CLOSED":
        raise ControlError("创建下一批前，当前批次必须已关闭")
    context = inherited_context(previous)
    # AUTO_INIT_NEXT can follow a discovery batch that created a delivery-only
    # handoff. In that case the previous batch's cached strategy and remaining
    # query budget describe the old batch, not the next action. Re-read the
    # authoritative city calculation whenever it is available.
    if city_state_path and city_state_path.is_file():
        try:
            from city_coverage_control import calculate

            city_state = load_state(city_state_path)
            same_binding(previous, city_state)
            live = calculate(city_state)
            has_carryover = bool(context["candidates"]) or any(
                row.get("disposition") == "PENDING" for row in context["discovery_records"].values()
            )
            live_strategy = str(live.get("next_strategy", "")).strip().upper()
            if live_strategy and not has_carryover:
                context["strategy_id"] = live_strategy
                context["query_budget"] = int(live.get("strategy_budget", 0) or 0)
            if mode == "expansion" and not has_carryover:
                expansion_rounds = [
                    str(item.get("round_id", "")) for item in city_state.get("rounds", [])
                    if item.get("discovery_mode") == "expansion"
                ]
                numbers = [int(value.rsplit("-", 1)[-1]) for value in expansion_rounds
                           if value.startswith("EX-DG-") and value.rsplit("-", 1)[-1].isdigit()]
                context["round_id"] = f"EX-DG-{max(numbers, default=0) + 1:03d}"
        except ConfigError:
            raise
        except (ImportError, KeyError, OSError, ValueError):
            pass
    if round_id and mode != "expansion":
        context["round_id"] = round_id
    state = {
        "schema_version": 6,
        "batch_id": batch_id,
        "mode": mode,
        "batch_status": "OPEN",
        "active_workset": [],
        **context,
        "inherited_final_queue": previous.get("pending_final_queue", []),
        "delivery_queue": [],
        "pending_final_queue": [],
        "events": [{
            "time": now(),
            "action": "AUTO_INIT_NEXT",
            "mode": mode,
            "previous_state": str(previous_path.resolve()),
            "inherited_final_count": len(previous.get("pending_final_queue", [])),
            "inherited_carryover_count": len(context["candidates"]),
        }],
        "created_at": now(),
        "updated_at": now(),
    }
    if city_state_path and mode == "search":
        from base_keyword_traversal_control import traversal_records
        from city_coverage_control import load_authoritative_base_keywords
        city = load_state(city_state_path)
        missing, _ = traversal_records(city, load_authoritative_base_keywords(asset_path(city, "keywords")))
        state["assigned_keyword_ids"] = [item["keyword_id"] for item in missing[:policy_for(state)["base_keyword_batch_max"]]]
    else:
        state["assigned_keyword_ids"] = []
    save_state(path, state)


def _registry(state: dict) -> dict:
    index = dict(state.get("global_candidate_index", {}))
    for candidate in state.get("candidates", []):
        index[candidate["candidate_id"]] = {"candidate_id": candidate["candidate_id"],
            "company_name": candidate.get("company_name", candidate.get("normalized_name", "")),
            "dedupe_key": candidate.get("dedupe_key", "")}
    return index


def register_discovery(state: dict, state_path: Path, result_path: Path) -> dict:
    try:
        payload = discovery.load_validated_discovery(result_path.resolve())
    except (discovery.WorkerControlError, OSError, ValueError) as exc:
        raise ControlError(str(exc)) from exc
    result_hash = discovery.file_hash(result_path)
    inputs = state.setdefault("discovery_inputs", {})
    if result_hash in inputs:
        return payload
    if payload["formal_batch_id"] != state["batch_id"] or payload["input_version"] != discovery.file_hash(state_path):
        raise ControlError("新发现结果与正式批次或输入版本不一致；不得手填来源绕过")
    ledger = state.setdefault("discovery_records", {})
    for record in payload["discovery_records"]:
        rid = record["record_id"]
        if rid in ledger:
            raise ControlError("发现记录ID冲突")
        ledger[rid] = {"record": copy.deepcopy(record), "result_sha256": result_hash,
            "disposition": "PENDING", "disposition_reason": "", "candidate_id": "",
            "entity_resolution_status": "UNRESOLVED", "resolved_dedupe_key": "",
            "existing_candidate_id": "", "counts_as_unique_entity": False}
    inputs[result_hash] = {"path": str(result_path.resolve()), "origin_batch_id": payload["formal_batch_id"]}
    queries = state.setdefault("queries", [])
    known = {q["query_id"] for q in queries}
    if any(q["query_id"] in known for q in payload["queries"]):
        raise ControlError("发现结果查询ID与本批已登记查询重复")
    queries.extend(copy.deepcopy(payload["queries"]))
    if "system_search_plan" in payload:
        if state.get("system_search_plan") and state["system_search_plan"] != payload["system_search_plan"]:
            raise ControlError("同批不能混入不同SYSTEM_SEARCH计划")
        state["system_search_plan"] = payload["system_search_plan"]
    return payload


def refresh_candidate_sources(candidate: dict, ledger: dict) -> None:
    provenance = candidate["provenance"]
    candidate.update(discovery.provenance_fields(provenance))
    candidate["source_url"] = provenance[0]["source_url"]
    candidate["discovery_reason"] = "；".join(unique_strings([
        ledger[p["record_id"]]["record"]["discovery_reason"] for p in provenance]))


def candidate_source_problem(candidate: dict, ledger: dict, queries: dict) -> str:
    provenance = candidate.get("provenance")
    if not isinstance(provenance, list) or not provenance:
        return "LEGACY_SOURCE_UNVERIFIABLE"
    has_added = False
    for source in provenance:
        row = ledger.get(source.get("record_id"), {})
        record = row.get("record", {})
        expected = [{**p, "result_sha256": row.get("result_sha256")} for p in record.get("provenance", [])]
        if source not in expected or row.get("candidate_id") != candidate["candidate_id"] or row.get("disposition") not in {"ADDED", "DUPLICATE"}:
            return "SOURCE_RECORD_MISMATCH"
        if row["disposition"] == "ADDED":
            has_added = True
            canonical_name = row.get("canonical_company_name", record.get("company_name"))
            if canonical_name != candidate.get("company_name", candidate.get("normalized_name")):
                return "COMPANY_RECORD_MISMATCH"
        query = queries.get(source["query_id"], {})
        if any(source.get(k) != query.get(k) for k in ("query_text", "task_id", "keyword_family")):
            return "QUERY_PROVENANCE_MISMATCH"
    if not has_added:
        return "MISSING_ORIGINAL_ADMISSION"
    if any(candidate.get(k) != v for k, v in discovery.provenance_fields(provenance).items()):
        return "LEGACY_PROJECTIONS_MISMATCH"
    expected_reason = "；".join(unique_strings([ledger[p["record_id"]]["record"]["discovery_reason"] for p in provenance]))
    if candidate.get("discovery_reason") != expected_reason or candidate.get("source_url") != provenance[0]["source_url"]:
        return "SOURCE_SUMMARY_MISMATCH"
    return ""


def audit_discovery(state: dict, allow_pending: bool = False) -> tuple[list, list]:
    inputs = state.get("discovery_inputs", {})
    ledger = state.get("discovery_records", {})
    if not inputs:
        # A zero-query, zero-candidate batch can be created solely to hand off
        # inherited FINALs. It has no discovery provenance to audit; keep this
        # exception tightly scoped so ordinary batches still require a ledger.
        if (int(state.get("query_budget", 0) or 0) == 0
                and not state.get("queries")
                and not state.get("candidates")
                and state.get("inherited_final_queue")):
            return [], []
        raise ControlError("缺少已登记发现结果；历史来源无法反查，须补证，不能自行补造")
    expected = {}
    payloads = []
    for result_hash, entry in inputs.items():
        path = Path(entry["path"])
        try:
            if discovery.file_hash(path) != result_hash:
                raise ControlError("已登记发现结果内容变化")
            payload = discovery.load_validated_discovery(path)
        except (OSError, discovery.WorkerControlError) as exc:
            raise ControlError(str(exc)) from exc
        payloads.append(payload)
        for record in payload["discovery_records"]:
            expected[record["record_id"]] = (record, result_hash)
    query_map = {q["query_id"]: q for payload in payloads for q in payload["queries"]}
    current_queries = [q for payload, entry in zip(payloads, inputs.values())
                       if entry["origin_batch_id"] == state["batch_id"] for q in payload["queries"]]
    if state.get("queries", []) != current_queries:
        raise ControlError("本批已校验查询被遗漏或改写")
    for rid, row in ledger.items():
        if rid not in expected or (row.get("record"), row.get("result_sha256")) != expected[rid]:
            raise ControlError(f"处置账原始记录不一致：{rid}")
        disposition = row.get("disposition")
        if disposition not in {"PENDING", "ADDED", "DUPLICATE", "OUT_OF_SCOPE", "INSUFFICIENT_CLUE"}:
            raise ControlError("处置类型非法")
        if disposition != "PENDING" and not str(row.get("disposition_reason", "")).strip():
            raise ControlError("已处置线索缺少具体理由")
        if disposition in {"ADDED", "DUPLICATE"} and row.get("candidate_id") not in _registry(state):
            raise ControlError("新增或重复线索无法反查候选ID")
        if disposition != "PENDING":
            resolution = row.get("entity_resolution_status")
            key = str(row.get("resolved_dedupe_key", "")).strip()
            if resolution not in {"NEW", "EXISTING"} or not key:
                raise ControlError("已处置线索缺少全局主体解析结果")
            if bool(row.get("counts_as_unique_entity")) != (resolution == "NEW"):
                raise ControlError("主体解析状态与新增主体计数标志不一致")
    # All records from a newly registered package must remain represented.
    for result_hash, entry in inputs.items():
        if entry["origin_batch_id"] == state["batch_id"]:
            missing = [rid for rid, (_, h) in expected.items() if h == result_hash and rid not in ledger]
            if missing:
                raise ControlError("已识别线索被删除或未登记：" + ",".join(missing))
    for candidate in state.get("candidates", []):
        problem = candidate_source_problem(candidate, ledger, query_map)
        if problem:
            raise ControlError(f"候选 {candidate['candidate_id']} 来源待补：{problem}")
    pending = [rid for rid, row in ledger.items() if row.get("disposition") == "PENDING"]
    if pending and not allow_pending:
        raise ControlError("存在未处置企业线索：" + ",".join(pending))
    for rid, row in ledger.items():
        if row.get("disposition") == "ADDED" and inputs[row["result_sha256"]]["origin_batch_id"] == state["batch_id"]:
            if not any(c["candidate_id"] == row["candidate_id"] and any(p.get("record_id") == rid for p in c.get("provenance", [])) for c in state.get("candidates", [])):
                raise ControlError("新增处置没有对应正式候选及来源记录")
    return payloads, pending


def command_register_discovery(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state); state = load_state(path); ensure_open(state)
    register_discovery(state, path, Path(args.discovery_result))
    save_state(path, state)
    print("发现结果及全部企业线索已登记；待主控逐条处置")


def command_add(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state); state = load_state(path); ensure_open(state)
    payload = register_discovery(state, path, Path(args.discovery_result))
    row = state["discovery_records"].get(args.record_id)
    if not row or args.record_id not in {r["record_id"] for r in payload["discovery_records"]}:
        raise ControlError("record-id不属于所引用发现结果")
    if row["disposition"] != "PENDING":
        raise ControlError("该线索已经处置，不得重复入池")
    if len(state["candidates"]) >= limits_for(state)["candidate_max"]:
        raise ControlError("候选池已达到本批上限；显式结转待处理线索后续批")
    record = row["record"]
    index = _registry(state)
    if args.candidate_id in index or args.candidate_id in state.get("candidate_id_reservations", []):
        raise ControlError("candidate_id已存在")
    key = dedupe_key(record["company_name"])
    if key in set(state.get("global_dedupe_keys", [])) or any(key == (v.get("dedupe_key") or dedupe_key(v.get("company_name", ""))) for v in index.values()):
        raise ControlError("主体已存在；请登记DUPLICATE并关联已有候选ID")
    candidate = {"candidate_id": args.candidate_id, "company_name": record["company_name"],
        "role_hypothesis": record["preliminary_role"], "dedupe_key": key,
        "provenance": [{**p, "result_sha256": row["result_sha256"]} for p in record["provenance"]],
        "lead_summary": record["lead_summary"], "status": "UNVERIFIED", "result_note": "",
        **{k: copy.deepcopy(record[k]) for k in discovery.EXPANSION_PLAN_FIELDS | {"discovery_method", "seed_role", "similarity_basis"} if k in record}}
    if state.get("mode") == "expansion" and (record.get("discovery_method") != "YIELD_EXPANSION" or any(not record.get(k) for k in discovery.EXPANSION_PLAN_FIELDS | {"similarity_basis"})):
        raise ControlError("扩展候选缺少受控种子特征链")
    row.update(disposition="ADDED", disposition_reason=record["discovery_reason"], candidate_id=args.candidate_id,
               entity_resolution_status="NEW", resolved_dedupe_key=key,
               existing_candidate_id="", counts_as_unique_entity=True)
    refresh_candidate_sources(candidate, state["discovery_records"])
    state["candidates"].append(candidate)
    state.setdefault("global_dedupe_keys", []).append(key)
    state["global_candidate_index"] = _registry(state)
    state["events"].append({"time": now(), "action": "ADD", "candidate_id": args.candidate_id, "record_id": args.record_id})
    save_state(path, state)
    print(f"已从已校验记录加入候选 {args.candidate_id}")


def command_dispose(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state); state = load_state(path); ensure_open(state)
    row = state.get("discovery_records", {}).get(args.record_id)
    if not row or row["disposition"] != "PENDING":
        raise ControlError("线索不存在或已处置")
    if not args.reason.strip():
        raise ControlError("未入池必须写明具体理由；不满足FINAL不能单独作为理由")
    if args.disposition == "DUPLICATE":
        if args.existing_candidate_id not in _registry(state):
            raise ControlError("重复线索必须关联可反查的已有候选ID")
        row["candidate_id"] = args.existing_candidate_id
        existing = _registry(state)[args.existing_candidate_id]
        row.update(entity_resolution_status="EXISTING",
                   resolved_dedupe_key=existing.get("dedupe_key") or dedupe_key(existing.get("company_name", "")),
                   existing_candidate_id=args.existing_candidate_id, counts_as_unique_entity=False)
        local = next((c for c in state["candidates"] if c["candidate_id"] == args.existing_candidate_id), None)
        if local and local.get("provenance"):
            local["provenance"].extend({**p, "result_sha256": row["result_sha256"]} for p in row["record"]["provenance"])
            refresh_candidate_sources(local, state["discovery_records"])
    elif args.existing_candidate_id:
        raise ControlError("仅DUPLICATE可指定已有候选ID")
    else:
        key = dedupe_key(row["record"]["company_name"])
        known = set(state.get("global_dedupe_keys", []))
        is_new = bool(key) and key not in known
        row.update(entity_resolution_status="NEW" if is_new else "EXISTING",
                   resolved_dedupe_key=key, existing_candidate_id="",
                   counts_as_unique_entity=is_new)
        if is_new:
            state.setdefault("global_dedupe_keys", []).append(key)
    row.update(disposition=args.disposition, disposition_reason=args.reason.strip())
    save_state(path, state)
    print(f"线索 {args.record_id} 已处置为 {args.disposition}")


def command_auto_resolve_discovery(args: argparse.Namespace) -> None:
    """Resolve exact duplicates and admit new validated leads atomically."""
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    index = _registry(state)
    added, duplicates = [], []
    for record_id, row in state.get("discovery_records", {}).items():
        if row.get("disposition") != "PENDING":
            continue
        record = row["record"]
        key = dedupe_key(record.get("company_name", ""))
        if not key:
            continue
        existing_id, match_reason = find_existing_name_match(record.get("company_name", ""), index)
        if existing_id:
            row.update(
                disposition="DUPLICATE",
                disposition_reason=match_reason,
                candidate_id=existing_id,
                entity_resolution_status="EXISTING",
                resolved_dedupe_key=index[existing_id].get("dedupe_key") or dedupe_key(index[existing_id].get("company_name", "")),
                existing_candidate_id=existing_id,
                counts_as_unique_entity=False,
            )
            local = next((c for c in state.get("candidates", []) if c["candidate_id"] == existing_id), None)
            if local:
                local.setdefault("provenance", []).extend(
                    {**p, "result_sha256": row["result_sha256"]} for p in record["provenance"]
                )
                refresh_candidate_sources(local, state["discovery_records"])
            duplicates.append(record_id)
            continue
        if len(state.get("candidates", [])) >= limits_for(state)["candidate_max"]:
            break
        candidate_id = next_candidate_id(state)
        candidate = {
            "candidate_id": candidate_id,
            "company_name": record["company_name"],
            "role_hypothesis": record["preliminary_role"],
            "dedupe_key": key,
            "provenance": [{**p, "result_sha256": row["result_sha256"]} for p in record["provenance"]],
            "lead_summary": record["lead_summary"],
            "status": "UNVERIFIED",
            "result_note": "",
            "followup_attempts": 0,
            **{k: copy.deepcopy(record[k]) for k in discovery.EXPANSION_PLAN_FIELDS | {"discovery_method", "seed_role", "similarity_basis"} if k in record},
        }
        row.update(
            disposition="ADDED",
            disposition_reason=record["discovery_reason"],
            candidate_id=candidate_id,
            entity_resolution_status="NEW",
            resolved_dedupe_key=key,
            existing_candidate_id="",
            counts_as_unique_entity=True,
        )
        refresh_candidate_sources(candidate, state["discovery_records"])
        state.setdefault("candidates", []).append(candidate)
        state.setdefault("global_dedupe_keys", []).append(key)
        index[candidate_id] = candidate
        added.append(candidate_id)
    state["global_candidate_index"] = {**_registry(state), **index}
    state.setdefault("events", []).append({
        "time": now(), "action": "AUTO_RESOLVE_DISCOVERY",
        "added_candidate_ids": added, "duplicate_record_ids": duplicates,
    })
    save_state(path, state)
    pending = [rid for rid, row in state.get("discovery_records", {}).items() if row.get("disposition") == "PENDING"]
    print(json.dumps({"added_candidate_ids": added, "duplicate_record_ids": duplicates,
                      "review_required_record_ids": pending}, ensure_ascii=False, indent=2))


def command_reclassify_duplicate(args: argparse.Namespace) -> None:
    """Correct an admitted alias after independent verification confirms identity."""
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    if not args.reason.strip():
        raise ControlError("重复更正必须写明独立核验依据")
    row = state.get("discovery_records", {}).get(args.record_id)
    if not row or row.get("disposition") != "ADDED" or row.get("candidate_id") != args.candidate_id:
        raise ControlError("发现记录不是该候选当前的ADDED线索")
    entry = state.get("discovery_inputs", {}).get(row.get("result_sha256"), {})
    if entry.get("origin_batch_id") != state.get("batch_id"):
        raise ControlError("只能更正本批新增的候选")
    candidate = find_candidate(state, args.candidate_id)
    if candidate.get("status") != "EXCLUDED" or args.candidate_id in state.get("active_workset", []):
        raise ControlError("只有已独立核验为EXCLUDED且退出工作集的候选可以更正")
    index = _registry(state)
    index.pop(args.candidate_id, None)
    existing = index.get(args.existing_candidate_id)
    if not existing or args.existing_candidate_id == args.candidate_id:
        raise ControlError("目标既有候选不存在或与当前候选相同")
    matched_id, _ = find_existing_name_match(row["record"].get("company_name", ""), index)
    if matched_id != args.existing_candidate_id:
        raise ControlError("记录名称未包含目标主体完整法定名称；禁止仅凭手工指定改记重复")
    target_key = existing.get("dedupe_key") or dedupe_key(existing.get("company_name", ""))
    removed_key = candidate.get("dedupe_key") or dedupe_key(candidate.get("company_name", ""))
    row.update(
        disposition="DUPLICATE",
        disposition_reason=args.reason.strip(),
        candidate_id=args.existing_candidate_id,
        entity_resolution_status="EXISTING",
        resolved_dedupe_key=target_key,
        existing_candidate_id=args.existing_candidate_id,
        counts_as_unique_entity=False,
    )
    state["candidates"] = [item for item in state.get("candidates", []) if item.get("candidate_id") != args.candidate_id]
    state["global_dedupe_keys"] = [key for key in state.get("global_dedupe_keys", []) if key != removed_key]
    state.setdefault("global_candidate_index", {}).pop(args.candidate_id, None)
    state["global_candidate_index"] = _registry(state)
    state.setdefault("events", []).append({
        "time": now(), "action": "RECLASSIFY_DUPLICATE", "record_id": args.record_id,
        "removed_candidate_id": args.candidate_id, "existing_candidate_id": args.existing_candidate_id,
        "reason": args.reason.strip(),
    })
    state["candidate_id_reservations"] = list(dict.fromkeys(
        state.get("candidate_id_reservations", []) + [args.candidate_id]
    ))
    save_state(path, state)
    print(f"已将 {args.candidate_id} 更正为既有主体 {args.existing_candidate_id} 的重复线索")


def command_reassign_candidate_id(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    old_id, new_id = args.candidate_id, args.new_candidate_id
    candidate = find_candidate(state, old_id)
    if new_id != old_id and candidate.get("status") not in {"UNVERIFIED", "IN_WORKSET"}:
        raise ControlError("只能重编号尚未完成核验的候选")
    if new_id != old_id and (new_id in _registry(state) or new_id in state.get("candidate_id_reservations", [])):
        raise ControlError("新编号已使用或已保留")
    linked = [row for row in state.get("discovery_records", {}).values()
              if row.get("candidate_id") == old_id and row.get("disposition") == "ADDED"]
    if not linked:
        raise ControlError("候选没有关联的本批新增发现记录")
    candidate["candidate_id"] = new_id
    for provenance in candidate.get("provenance", []):
        provenance.pop("candidate_id", None)
    reserved_ids = set(state.get("candidate_id_reservations", []))
    entity_key = dedupe_key(candidate.get("company_name", ""))
    for row in state.get("discovery_records", {}).values():
        same_entity = dedupe_key(row.get("record", {}).get("company_name", "")) == entity_key
        if (row.get("candidate_id") == old_id or
                (row.get("candidate_id") in reserved_ids and same_entity)) and row.get("disposition") in {"ADDED", "DUPLICATE"}:
            row["candidate_id"] = new_id
    state["active_workset"] = [new_id if x == old_id else x for x in state.get("active_workset", [])]
    state["candidate_id_reservations"] = list(dict.fromkeys(
        state.get("candidate_id_reservations", []) + [old_id]
    ))
    state.setdefault("global_candidate_index", {}).pop(old_id, None)
    state["global_candidate_index"][new_id] = {"candidate_id": new_id,
        "company_name": candidate.get("company_name", ""),
        "dedupe_key": candidate.get("dedupe_key") or dedupe_key(candidate.get("company_name", ""))}
    state["global_candidate_index"] = _registry(state)
    state.setdefault("events", []).append({"time": now(), "action": "REASSIGN_CANDIDATE_ID",
        "old_candidate_id": old_id, "new_candidate_id": new_id, "reason": args.reason.strip()})
    save_state(path, state)
    print(f"已将候选编号 {old_id} 调整为 {new_id}")


def command_correct_candidate_name(args: argparse.Namespace) -> None:
    """Correct a candidate's legal name after independent identity verification."""
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    candidate = find_candidate(state, args.candidate_id)
    new_name = args.new_name.strip()
    if not new_name:
        raise ControlError("更正后的主体名称不能为空")
    old_name = candidate.get("company_name", "")
    old_key = candidate.get("dedupe_key") or dedupe_key(old_name)
    new_key = dedupe_key(new_name)
    if not new_key:
        raise ControlError("更正后的主体名称无法生成去重键")
    for cid, item in _registry(state).items():
        if cid != args.candidate_id and dedupe_key(item.get("dedupe_key") or item.get("company_name", "")) == new_key:
            raise ControlError(f"更正后的法定名称与既有候选 {cid} 重复")
    candidate["company_name"] = new_name
    candidate["dedupe_key"] = new_key
    for row in state.get("discovery_records", {}).values():
        if row.get("candidate_id") == args.candidate_id and row.get("disposition") == "ADDED":
            row["canonical_company_name"] = new_name
            row["canonical_name_reason"] = args.reason.strip()
    keys = state.setdefault("global_dedupe_keys", [])
    if old_key and old_key not in keys:
        keys.append(old_key)
    if new_key not in keys:
        keys.append(new_key)
    state["global_candidate_index"] = _registry(state)
    state.setdefault("events", []).append({
        "time": now(), "action": "CORRECT_CANDIDATE_NAME",
        "candidate_id": args.candidate_id, "old_name": old_name,
        "new_name": new_name, "reason": args.reason.strip(),
    })
    save_state(path, state)
    print(f"已更正候选 {args.candidate_id} 的主体名称")


def command_retry_followup(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    requested = set(args.candidate_ids or [])
    selected = []
    for item in state.get("candidates", []):
        if (item.get("status") != "FOLLOWUP"
                or item.get("followup_reason") not in {"IDENTITY_PENDING", "LOCATION_PENDING"}
                or (requested and item["candidate_id"] not in requested)):
            continue
        attempts = int(item.get("followup_attempts", 0))
        if attempts >= args.max_attempts:
            continue
        item["followup_attempts"] = attempts + 1
        item["status"] = "UNVERIFIED"
        item["result_note"] = ""
        selected.append(item["candidate_id"])
    state.setdefault("events", []).append({
        "time": now(), "action": "RETRY_FOLLOWUP", "candidate_ids": selected,
    })
    save_state(path, state)
    print(json.dumps({"requeued_candidate_ids": selected,
                      "max_attempts": args.max_attempts}, ensure_ascii=False, indent=2))


def command_start_workset(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    if state["active_workset"]:
        raise ControlError("已有未完成工作集；必须先完成全部分流")
    ids = list(dict.fromkeys(args.candidate_ids))
    if not ids:
        raise ControlError("工作集不能为空")
    if len(ids) > limits_for(state)["workset_max"]:
        raise ControlError(f"单次工作集最多 {limits_for(state)['workset_max']} 家")
    for candidate_id in ids:
        item = find_candidate(state, candidate_id)
        if item["status"] != "UNVERIFIED":
            raise ControlError(f"候选 {candidate_id} 当前状态为 {item['status']}，不能进入新工作集")
    for candidate_id in ids:
        find_candidate(state, candidate_id)["status"] = "IN_WORKSET"
    state["active_workset"] = ids
    state["events"].append({"time": now(), "action": "START_WORKSET", "candidate_ids": ids})
    save_state(path, state)
    print(f"已启动工作集：{', '.join(ids)}")


def command_complete(args):
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    item = find_candidate(state, args.candidate_id)
    if args.candidate_id not in state["active_workset"] or item["status"] != "IN_WORKSET":
        raise ControlError("候选不在活动工作集中")
    payload = json.loads(Path(args.verification_result).read_text(encoding="utf-8"))
    if (payload.get("batch_id") != state["batch_id"]
            or payload.get("input_version") != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ControlError("单项核验结果的批次或版本已变化")
    verification = next((r for r in payload.get("results", []) if r.get("candidate_id") == args.candidate_id), None)
    if verification is None or verification.get("status") != args.status or verification.get("note") != args.note:
        raise ControlError("核验文件缺少一致的候选、状态及说明")
    verification = discovery.normalize_verification_record(verification)
    discovery.validate_verification_record(verification, status_key="status", profile=profile_for(state), city=state.get("city", ""))
    if args.status == "FINAL":
        required = ("matched_keywords", "discovery_queries", "discovered_by_query_ids", "discovery_task_ids", "keyword_families")
        if any(not unique_strings(item.get(k)) for k in required):
            raise ControlError("FINAL缺少完整检索来源")
    item.update(status=args.status, result_note=args.note)
    for key in ("business_fit", "prospect_fit_evidence", "official_website", "website_verification_status", "website_evidence_ref", "website_entity_match_note"):
        item[key] = copy.deepcopy(verification.get(key, ""))
    item["followup_reason"] = verification.get("followup_reason", "") if args.status == "FOLLOWUP" else ""
    scene = item["prospect_fit_evidence"]
    item["evidence_ids"] = list(dict.fromkeys([*item.get("evidence_ids", []),
        *[str(r.get("ref", "")) for r in scene["evidence_refs"] if isinstance(r, dict) and r.get("ref")]]))
    state["active_workset"].remove(args.candidate_id)
    state["events"].append({"time": now(), "action": "COMPLETE", "candidate_id": args.candidate_id, "status": args.status})
    save_state(path, state)
    print(f"已分流 {args.candidate_id} → {args.status}")


def command_carryover(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    item = find_candidate(state, args.candidate_id)
    if item["status"] != "UNVERIFIED":
        raise ControlError(f"只有 UNVERIFIED 候选可以结转；当前状态为 {item['status']}")
    if not args.reason.strip():
        raise ControlError("结转必须填写原因")
    item["status"] = "CARRYOVER"
    item["result_note"] = args.reason.strip()
    state["events"].append({"time": now(), "action": "CARRYOVER", "candidate_id": args.candidate_id})
    save_state(path, state)
    print(f"已结转 {args.candidate_id} → CARRYOVER")


def summary(state: dict) -> dict:
    counts = {status: len(candidates_by_status(state, status)) for status in ALL_STATUSES}
    limits = limits_for(state)
    inherited_count = len(state.get("inherited_final_queue", []))
    delivery_queue = state.get("delivery_queue", [])
    pending_final_queue = state.get("pending_final_queue", [])
    return {
        "batch_id": state["batch_id"],
        "mode": state["mode"],
        "batch_status": state["batch_status"],
        "candidate_count": len(state["candidates"]),
        "candidate_max": limits["candidate_max"],
        "active_workset": state["active_workset"],
        "counts": counts,
        "delivery_size": limits["delivery_size"],
        "inherited_final_count": inherited_count,
        "available_final_count": inherited_count + counts["FINAL"],
        "delivery_queue_count": len(delivery_queue),
        "pending_final_count": len(pending_final_queue),
        "ready_for_delivery": len(delivery_queue) == limits["delivery_size"],
    }


def command_status(args: argparse.Namespace) -> None:
    state = load_state(Path(args.state))
    result = summary(state)
    result["pending_discovery_ids"] = [rid for rid, row in state.get("discovery_records", {}).items() if row.get("disposition") == "PENDING"]
    print(json.dumps(result, ensure_ascii=False, indent=2))


def expansion_no_new_streak(batch_state: dict, city_state: dict | None = None) -> list[str]:
    """Distinct, disposed duplicate companies since the last new pool company."""
    previous = next((r for r in reversed((city_state or {}).get("rounds", []))
                     if r.get("round_id") == batch_state.get("round_id")
                     and r.get("discovery_mode") == "expansion"), {})
    streak = list(previous.get("expansion_no_new_streak_keys", []))
    for row in batch_state.get("discovery_records", {}).values():
        if row.get("disposition") == "ADDED" and row.get("counts_as_unique_entity"):
            streak = []
        elif row.get("disposition") == "DUPLICATE":
            key = str(row.get("resolved_dedupe_key", "")).strip()
            if key and key not in streak:
                streak.append(key)
    return streak


def command_close(args: argparse.Namespace) -> None:
    require_token(args)
    path = Path(args.state)
    state = load_state(path)
    ensure_open(state)
    if bool(args.next_state) != bool(args.next_batch_id):
        raise ControlError("自动续批必须同时提供 --next-state 和 --next-batch-id")
    payloads, pending_leads = audit_discovery(state, allow_pending=bool(args.next_state and args.next_batch_id))
    if pending_leads:
        state["discovery_carryover"] = {"record_ids": pending_leads, "next_state": str(Path(args.next_state).resolve()), "next_batch_id": args.next_batch_id}
    expected_queries = {q["query_id"]: q for payload in payloads for q in payload["queries"]}
    if any(q != expected_queries.get(q["query_id"]) for q in state.get("queries", [])):
        raise ControlError("本批查询与已校验发现结果不一致")
    if args.query_log:
        supplied = load_state(Path(args.query_log))
        if (supplied.get("queries") or supplied.get("tasks") or []) != state.get("queries", []):
            raise ControlError("关闭时提供的查询日志与本批已登记查询不一致")
    info = summary(state)
    if state["active_workset"]:
        raise ControlError("仍有活动工作集，不能关闭批次")
    if info["counts"]["IN_WORKSET"]:
        raise ControlError("仍有未分流的 IN_WORKSET 候选")
    if info["counts"]["UNVERIFIED"]:
        raise ControlError("仍有 UNVERIFIED 候选；必须核验分流或使用 carryover 显式结转")
    candidate_min = limits_for(state)["candidate_min"]
    budget = int(state.get("query_budget", 0) or 0)
    if state.get("mode") == "search" and state.get("system_search_plan"):
        plan_budget = int(state["system_search_plan"].get("budget", 0) or 0)
        if plan_budget and plan_budget != len(state.get("queries", [])):
            raise ControlError("SYSTEM_SEARCH执行数与受控计划预算不一致")
        if plan_budget:
            budget = plan_budget
            state["query_budget"] = plan_budget
    executed = len(state.get("queries", []))
    if args.queries_executed is not None and args.queries_executed != executed:
        raise ControlError("自报查询数与已登记执行记录不一致")
    state["queries_executed"] = executed
    state["remaining_query_budget"] = max(0, budget - executed)
    city_for_stop = load_state(Path(args.city_state)) if args.city_state and Path(args.city_state).is_file() else {}
    no_new_keys = expansion_no_new_streak(state, city_for_stop) if state.get("mode") == "expansion" else []
    expansion_early_stop = (state.get("mode") == "expansion" and len(no_new_keys) >= policy_for(state)["expansion"]["duplicate_streak"]
                            and not pending_leads and not any(q.get("termination_reason") == "SOURCE_BLOCKED" for q in state.get("queries", []))
                            and not next((r.get("source_blocked_query_count", 0) for r in reversed(city_for_stop.get("rounds", []))
                                          if r.get("round_id") == state.get("round_id")), 0))
    state["expansion_no_new_streak_keys"] = no_new_keys
    state["expansion_early_stop"] = expansion_early_stop
    if expansion_early_stop and args.round_status not in {"AUTO", "EARLY_STOPPED"}:
        raise ControlError("扩展达到绑定策略的无新增阈值时须EARLY_STOPPED，不得继续同一轮次")
    if args.round_status == "EARLY_STOPPED" and not expansion_early_stop and state.get("mode") == "expansion":
        raise ControlError("扩展未满足绑定策略的无新增阈值，不能手填EARLY_STOPPED")
    carryover_only = not state.get("queries") and bool(state.get("discovery_records") or state.get("candidates") or state.get("inherited_final_queue"))
    shortfall = (len(state["candidates"]) < candidate_min
                 and not (budget > 0 and executed >= budget) and not carryover_only)
    if shortfall and not expansion_early_stop and args.shortfall_reason_code not in SHORTFALL_REASON_CODES:
        raise ControlError(
            f"候选数低于最低关闭门槛 {candidate_min}，且查询预算未完成；"
            f"必须提供 --shortfall-reason-code（{', '.join(sorted(SHORTFALL_REASON_CODES))}）"
        )
    limits = limits_for(state)
    final_count = info["counts"]["FINAL"]
    inherited = list(state.get("inherited_final_queue", []))
    current_finals = [
        {
            "candidate_id": item["candidate_id"],
            "company_name": item["company_name"],
            "source_url": item["source_url"],
            **{k: copy.deepcopy(item[k]) for k in ("provenance", "discovery_reason", "lead_summary") if k in item},
            "role_hypothesis": item["role_hypothesis"],
            "matched_keywords": unique_strings(item.get("matched_keywords")),
            "discovery_queries": unique_strings(item.get("discovery_queries")),
            "discovered_by_query_ids": unique_strings(item.get("discovered_by_query_ids")),
            "discovery_task_ids": unique_strings(item.get("discovery_task_ids")),
            "keyword_families": unique_strings(item.get("keyword_families")),
            **{key: item.get(key) for key in (
                "discovery_method", "round_id", "expansion_task_id", "seed_id", "seed_name",
                "seed_role", "feature_chain_id", "feature_chain", "feature_values",
                "similarity_dimensions", "similarity_basis", "coverage_gap", "source_route",
            ) if item.get(key)},
            "qualification_note": item["result_note"],
            "business_fit": item.get("business_fit", ""),
            "prospect_fit_evidence": copy.deepcopy(item.get("prospect_fit_evidence", {})),
            "evidence_ids": unique_strings(item.get("evidence_ids")),
            "official_website": item.get("official_website", ""),
            "website_verification_status": item.get("website_verification_status", ""),
            "website_evidence_ref": item.get("website_evidence_ref", ""),
            "website_entity_match_note": item.get("website_entity_match_note", ""),
            "qualified_in_batch": state["batch_id"],
        }
        for item in state["candidates"]
        if item["status"] == "FINAL"
    ]
    combined = inherited + current_finals
    seen_ids = set()
    duplicate_ids = []
    for item in combined:
        candidate_id = item["candidate_id"]
        if candidate_id in seen_ids:
            duplicate_ids.append(candidate_id)
        seen_ids.add(candidate_id)
    if duplicate_ids:
        raise ControlError(f"FINAL 导出队列存在重复 candidate_id：{', '.join(sorted(set(duplicate_ids)))}")

    delivery_size = limits["delivery_size"]
    if len(combined) >= delivery_size:
        state["delivery_queue"] = combined[:delivery_size]
        state["pending_final_queue"] = combined[delivery_size:]
    else:
        state["delivery_queue"] = []
        state["pending_final_queue"] = combined
    effective_round_status = args.round_status
    if effective_round_status == "AUTO" and expansion_early_stop:
        effective_round_status = "EARLY_STOPPED"
    if effective_round_status == "AUTO":
        unresolved = sum(
            item.get("status") in {"CARRYOVER", "UNVERIFIED", "IN_WORKSET"}
            for item in state.get("candidates", [])
        )
        if budget > 0 and executed >= budget and not pending_leads and not unresolved:
            effective_round_status = "COMPLETED"
        else:
            effective_round_status = "OPEN"
    state["batch_status"] = "CLOSED"
    state["close_reason"] = args.reason.strip()
    state["events"].append({
        "time": now(),
        "action": "CLOSE",
        "final_count": final_count,
        "delivery_queue_count": len(state["delivery_queue"]),
        "pending_final_count": len(state["pending_final_queue"]),
        "city_state": args.city_state or "",
        "query_log": args.query_log or "",
        "evidence": args.evidence or "",
        "shortfall_reason_code": args.shortfall_reason_code or "",
        "queries_executed": executed,
    })
    save_state(path, state)
    coverage_result = None
    if args.city_state:
        try:
            from city_coverage_control import CoverageError, ingest_closed_batch

            coverage_result = ingest_closed_batch(
                Path(args.city_state),
                path,
                Path(args.query_log) if args.query_log else None,
                Path(args.evidence) if args.evidence else None,
                args.strategy_signature,
                args.new_types_added,
                args.gaps_closed,
                state.get("round_id", ""),
                effective_round_status,
                state.get("strategy_id", ""),
                int(state.get("query_budget", 0) or 0),
            )
        except (CoverageError, OSError, ValueError) as exc:
            raise ControlError(
                f"批次已关闭，但城市覆盖状态汇入失败：{exc}；"
                "修复输入后使用 city_coverage_control.py ingest-batch 重试"
            ) from exc
    print(
        f"批次已关闭；本批FINAL={final_count}；"
        f"导出队列={len(state['delivery_queue'])}/{delivery_size}；"
        f"下一批继承FINAL={len(state['pending_final_queue'])}"
    )
    if coverage_result is not None:
        print(
            "城市覆盖已累计计算；"
            f"coverage_complete={coverage_result['coverage_complete']}；"
            f"convergence_complete={coverage_result['convergence_complete']}；"
            f"stop_candidate={coverage_result['stop_candidate']}"
        )
    should_continue = coverage_result is None or not coverage_result["stop_candidate"]
    if should_continue and (args.next_state or args.next_batch_id):
        next_path = Path(args.next_state)
        next_round_id = determine_next_round_id(
            Path(args.city_state) if args.city_state else None,
            state.get("round_id", ""),
            coverage_result,
            effective_round_status,
        )
        next_mode = mode_for_next_strategy(state["mode"], coverage_result)
        if pending_leads or any(item.get("status") == "CARRYOVER" for item in state.get("candidates", [])):
            next_round_id = state.get("round_id", "")
            next_mode = state["mode"]
        init_next_state(next_path, args.next_batch_id, next_mode, path, round_id=next_round_id,
                        city_state_path=Path(args.city_state) if args.city_state else None)
        print(f"已自动创建下一批 {args.next_batch_id}（round_id={next_round_id}）：{next_path}")
    elif should_continue:
        print("CONTINUE_REQUIRED：尚未满足停止条件；建议关闭时提供 --next-state 和 --next-batch-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B2B潜客批次与上下文控制")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化批次")
    init.add_argument("--state", required=True)
    init.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    init.add_argument("--batch-id", required=True)
    init.add_argument("--mode", choices=sorted(MODES), required=True)
    init.add_argument("--previous-state", help="上一已关闭批次状态；继承其待导出FINAL队列")
    init.add_argument("--round-id", default="")
    init.add_argument("--strategy-id", default="")
    init.add_argument("--query-budget", type=int, default=0)
    init.add_argument("--city-state", help="城市覆盖状态；mode=expansion 时必填并校验扩展门禁")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=command_init)

    add = sub.add_parser("add", help="加入一个候选")
    add.add_argument("--state", required=True)
    add.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    add.add_argument("--candidate-id", required=True)
    add.add_argument("--discovery-result", required=True, help="已校验的发现合并结果")
    add.add_argument("--record-id", required=True, help="合并结果中的稳定发现记录ID")
    add.set_defaults(func=command_add)

    register = sub.add_parser("register-discovery", help="登记已校验查询及全部企业线索")
    register.add_argument("--state", required=True)
    register.add_argument("--caller-token", required=True)
    register.add_argument("--discovery-result", required=True)
    register.set_defaults(func=command_register_discovery)
    dispose = sub.add_parser("dispose-discovery", help="登记重复、明确范围外或线索不足；新增使用add")
    dispose.add_argument("--state", required=True)
    dispose.add_argument("--caller-token", required=True)
    dispose.add_argument("--record-id", required=True)
    dispose.add_argument("--disposition", choices=["DUPLICATE", "OUT_OF_SCOPE", "INSUFFICIENT_CLUE"], required=True)
    dispose.add_argument("--existing-candidate-id", default="")
    dispose.add_argument("--reason", required=True)
    dispose.set_defaults(func=command_dispose)

    auto_resolve = sub.add_parser("auto-resolve-discovery", help="自动处理精确重复、完整法定名称别名并为新主体分配ID")
    auto_resolve.add_argument("--state", required=True)
    auto_resolve.add_argument("--caller-token", required=True)
    auto_resolve.set_defaults(func=command_auto_resolve_discovery)

    reclassify = sub.add_parser("reclassify-duplicate", help="独立核验后将误入池的法定名称别名改记为重复")
    reclassify.add_argument("--state", required=True)
    reclassify.add_argument("--caller-token", required=True)
    reclassify.add_argument("--record-id", required=True)
    reclassify.add_argument("--candidate-id", required=True)
    reclassify.add_argument("--existing-candidate-id", required=True)
    reclassify.add_argument("--reason", required=True)
    reclassify.set_defaults(func=command_reclassify_duplicate)

    reassign = sub.add_parser("reassign-candidate-id", help="更正尚未完成核验的候选编号")
    reassign.add_argument("--state", required=True)
    reassign.add_argument("--caller-token", required=True)
    reassign.add_argument("--candidate-id", required=True)
    reassign.add_argument("--new-candidate-id", required=True)
    reassign.add_argument("--reason", required=True)
    reassign.set_defaults(func=command_reassign_candidate_id)

    correct_name = sub.add_parser("correct-candidate-name", help="主体核验后更正候选法定名称")
    correct_name.add_argument("--state", required=True)
    correct_name.add_argument("--caller-token", required=True)
    correct_name.add_argument("--candidate-id", required=True)
    correct_name.add_argument("--new-name", required=True)
    correct_name.add_argument("--reason", required=True)
    correct_name.set_defaults(func=command_correct_candidate_name)

    retry = sub.add_parser("retry-followup", help="将可重试FOLLOWUP重新放回核验队列")
    retry.add_argument("--state", required=True)
    retry.add_argument("--caller-token", required=True)
    retry.add_argument("--candidate-id", dest="candidate_ids", action="append", default=[])
    retry.add_argument("--max-attempts", type=int, default=2)
    retry.set_defaults(func=command_retry_followup)

    start = sub.add_parser("start-workset", help="按绑定策略启动核验工作集")
    start.add_argument("--state", required=True)
    start.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    start.add_argument("candidate_ids", nargs="+")
    start.set_defaults(func=command_start_workset)

    complete = sub.add_parser("complete", help="完成当前工作集中的一个候选")
    complete.add_argument("--state", required=True)
    complete.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    complete.add_argument("--candidate-id", required=True)
    complete.add_argument("--status", choices=sorted(TERMINAL_STATUSES), required=True)
    complete.add_argument("--note", required=True)
    complete.add_argument("--verification-result", required=True, help="含batch_id、input_version与results的主控核验文件")
    complete.set_defaults(func=command_complete)

    carryover = sub.add_parser("carryover", help="将尚未开始核验的候选显式结转到下一批")
    carryover.add_argument("--state", required=True)
    carryover.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    carryover.add_argument("--candidate-id", required=True)
    carryover.add_argument("--reason", required=True)
    carryover.set_defaults(func=command_carryover)

    status = sub.add_parser("status", help="输出批次摘要")
    status.add_argument("--state", required=True)
    status.set_defaults(func=command_status)

    close = sub.add_parser("close", help="校验并关闭批次")
    close.add_argument("--state", required=True)
    close.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    close.add_argument("--shortfall-reason-code", choices=sorted(SHORTFALL_REASON_CODES))
    close.add_argument("--queries-executed", type=int)
    close.add_argument("--reason", default="")
    close.add_argument("--city-state", help="城市覆盖状态；提供后在关闭批次后自动汇入并累计计算")
    close.add_argument("--query-log", help="本批查询日志JSON；与 --city-state 配合使用")
    close.add_argument("--evidence", help="本批证据JSON；与 --city-state 配合使用")
    close.add_argument("--strategy-signature", default="", help="本轮独立搜索策略签名；留空时由查询维度生成")
    close.add_argument("--new-types-added", type=int, default=0)
    close.add_argument("--gaps-closed", type=int, default=0)
    close.add_argument("--round-status", choices=["AUTO", "OPEN", "COMPLETED", "EARLY_STOPPED"], default="AUTO")
    close.add_argument("--next-state", help="停止条件未满足时自动创建的下一批状态文件")
    close.add_argument("--next-batch-id", help="停止条件未满足时自动创建的下一批编号")
    close.set_defaults(func=command_close)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except (ControlError, StateGuardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
