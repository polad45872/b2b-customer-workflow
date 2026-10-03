#!/usr/bin/env python3
"""搜索阶段轻量运行入口：按绑定策略组织核验工作集、交付分组和续批。"""

from __future__ import annotations

from b2b_config import policy_for, asset_path, profile_for

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import batch_context_control as batch
import base_keyword_traversal_control as traversal
import city_coverage_control as city_coverage
import system_search_plan_control as system_search
from state_guard import StateGuardError, verify_caller_token


class RunnerError(RuntimeError):
    pass


def load_result(path: Path) -> list[dict[str, Any]]:
    payload = batch.load_state(path)
    records = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise RunnerError("分流结果必须是JSON数组，或包含results数组的对象")
    return records


def workset_payload(state: dict[str, Any]) -> dict[str, Any]:
    item_map = {item["candidate_id"]: item for item in state.get("candidates", [])}
    ids = state.get("active_workset", [])
    return {
        "action": "VERIFY_WORKSET",
        "batch_id": state["batch_id"],
        "candidate_ids": ids,
        "candidates": [item_map[cid] for cid in ids],
        "workset_size": len(ids),
        "final_progress": len(state.get("inherited_final_queue", []))
        + sum(item.get("status") == "FINAL" for item in state.get("candidates", [])),
        "loop_target": batch.limits_for(state)["delivery_size"],
    }


def state_version(path: Path) -> str:
    """Version shown by `next` and required for a controller-authored result."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def next_batch_values(state_path: Path, state: dict[str, Any]) -> tuple[Path, str]:
    current_id = str(state["batch_id"])
    match = re.search(r"(\d+)$", current_id)
    if match:
        number = int(match.group(1)) + 1
        next_id = current_id[:match.start(1)] + str(number).zfill(len(match.group(1)))
    else:
        next_id = current_id + "-NEXT"
    next_path = state_path.with_name(f"{next_id}.json")
    return next_path, next_id


def command_next(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state = batch.load_state(state_path)
    batch.ensure_open(state)
    if state.get("active_workset"):
        print(json.dumps({**workset_payload(state), "input_version": state_version(state_path)}, ensure_ascii=False, indent=2))
        return
    pending = [item["candidate_id"] for item in state.get("candidates", []) if item.get("status") == "UNVERIFIED"]
    if not pending:
        print(json.dumps({
            "action": "CLOSE_BATCH",
            "batch_id": state["batch_id"],
            "reason": "当前批次候选已全部分流或结转",
        }, ensure_ascii=False, indent=2))
        return
    selected = pending[: batch.limits_for(state)["workset_max"]]
    batch.command_start_workset(SimpleNamespace(
        state=str(state_path), candidate_ids=selected,
        caller_token=getattr(args, "caller_token", ""),
    ))
    print(json.dumps({**workset_payload(batch.load_state(state_path)), "input_version": state_version(state_path)}, ensure_ascii=False, indent=2))


def command_commit_workset(args: argparse.Namespace) -> None:
    verify_caller_token(getattr(args, "caller_token", ""), state_path=args.state)
    state_path = Path(args.state).resolve()
    state = batch.load_state(state_path)
    batch.ensure_open(state)
    active = list(state.get("active_workset", []))
    if not active:
        raise RunnerError("当前没有活动核验工作集")
    result_path = Path(args.result).resolve()
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerError(f"无法读取主控核验结果：{exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"batch_id", "input_version", "candidate_ids", "results"}:
        raise RunnerError("主控核验结果只允许batch_id、input_version、candidate_ids、results字段")
    if payload["batch_id"] != state["batch_id"] or payload["input_version"] != state_version(state_path):
        raise RunnerError("批次或工作集版本已变化，请重新运行next并核验")
    if payload["candidate_ids"] != active:
        raise RunnerError("candidate_ids必须与当前工作集顺序及内容一致")
    records = payload["results"]
    if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
        raise RunnerError("results必须为核验结果对象数组")
    result_map = {str(item.get("candidate_id", "")).strip(): item for item in records}
    if len(result_map) != len(records) or set(result_map) != set(active):
        raise RunnerError("分流结果必须与当前工作集完整一致且ID不得重复")
    try:
        for row in records:
            batch.discovery.validate_verification_record(row, status_key="status", profile=profile_for(state), city=state.get("city", ""))
    except batch.discovery.WorkerControlError as exc:
        raise RunnerError(str(exc)) from exc
    records = [batch.discovery.normalize_verification_record(row) for row in records]
    result_map = {row["candidate_id"]: row for row in records}
    updated = copy.deepcopy(state)
    for cid in active:
        item = result_map[cid]
        status = str(item.get("status", "")).upper()
        note = str(item.get("note", "")).strip()
        if status not in batch.TERMINAL_STATUSES:
            raise RunnerError(f"{cid}分流状态非法：{status}")
        if not note:
            raise RunnerError(f"{cid}缺少分流说明")
        business_fit = str(item.get("business_fit", "")).strip()
        scene = item.get("prospect_fit_evidence")
        followup_reason = str(item.get("followup_reason", "")).strip().upper()
        candidate = batch.find_candidate(updated, cid)
        if cid not in updated["active_workset"] or candidate.get("status") != "IN_WORKSET":
            raise RunnerError(f"{cid}不在当前活动工作集中")
        if status == "FINAL":
            required_discovery = (
                "matched_keywords", "discovery_queries", "discovered_by_query_ids",
                "discovery_task_ids", "keyword_families",
            )
            missing = [field for field in required_discovery if not batch.unique_strings(candidate.get(field))]
            if missing:
                raise RunnerError(f"{cid} FINAL候选缺少检索来源字段：{', '.join(missing)}")
        batch.validate_qualification(status, business_fit, scene, followup_reason, profile_for(state), state.get("city", ""))
        candidate["status"] = status
        candidate["result_note"] = note
        candidate["business_fit"] = business_fit
        candidate["prospect_fit_evidence"] = scene
        candidate["followup_reason"] = followup_reason if status == "FOLLOWUP" else ""
        candidate["official_website"] = str(item.get("official_website", "")).strip()
        candidate["website_verification_status"] = str(item.get("website_verification_status", "")).strip().upper()
        candidate["website_evidence_ref"] = str(item.get("website_evidence_ref", "")).strip()
        candidate["website_entity_match_note"] = str(item.get("website_entity_match_note", "")).strip()
        candidate["evidence_ids"] = list(dict.fromkeys([
            *candidate.get("evidence_ids", []),
            *[
                str(ref.get("ref", "")).strip()
                for ref in scene.get("evidence_refs", [])
                if isinstance(ref, dict) and str(ref.get("ref", "")).strip()
            ],
        ]))
        updated["active_workset"].remove(cid)
        updated["events"].append({
            "time": batch.now(), "action": "COMPLETE", "candidate_id": cid, "status": status,
        })
    updated["events"].append({
        "time": batch.now(), "action": "COMMIT_WORKSET", "candidate_ids": active,
    })
    batch.save_state(state_path, updated)
    progress = len(updated.get("inherited_final_queue", [])) + sum(
        item.get("status") == "FINAL" for item in updated.get("candidates", [])
    )
    pending = sum(item.get("status") == "UNVERIFIED" for item in updated.get("candidates", []))
    print(json.dumps({
        "action": "NEXT_WORKSET" if pending else "CLOSE_BATCH",
        "batch_id": updated["batch_id"],
        "final_progress": progress,
        "loop_target": batch.limits_for(updated)["delivery_size"],
        "unverified_remaining": pending,
    }, ensure_ascii=False, indent=2))


def command_close(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state = batch.load_state(state_path)
    next_path, next_id = next_batch_values(state_path, state)
    close_args = SimpleNamespace(
        state=str(state_path),
        caller_token=getattr(args, "caller_token", ""),
        shortfall_reason_code=args.shortfall_reason_code,
        queries_executed=args.queries_executed,
        reason=args.reason,
        city_state=args.city_state,
        query_log=args.query_log,
        evidence=args.evidence,
        strategy_signature=args.strategy_signature,
        new_types_added=args.new_types_added,
        gaps_closed=args.gaps_closed,
        round_status=args.round_status,
        next_state=str(next_path),
        next_batch_id=next_id,
    )
    batch.command_close(close_args)
    closed = batch.load_state(state_path)
    delivery = closed.get("delivery_queue", [])
    print(json.dumps({
        "action": "LOOP_READY" if len(delivery) == batch.limits_for(closed)["delivery_size"] else "CONTINUE_SEARCH",
        "batch_id": closed["batch_id"],
        "delivery_loop": delivery,
        "delivery_count": len(delivery),
        "pending_final_count": len(closed.get("pending_final_queue", [])),
        "next_state": str(next_path) if next_path.exists() else "",
        "next_batch_id": next_id if next_path.exists() else "",
    }, ensure_ascii=False, indent=2))


def next_search_action_payload(
    city_state_path: Path,
    batch_size: int,
) -> dict[str, Any]:
    if batch_size < 1:
        raise RunnerError("--batch-size 必须大于0")
    city_state_path = city_state_path.resolve()
    state = city_coverage.load_json(city_state_path)
    calculation = city_coverage.calculate(state)
    authority = city_coverage.load_authoritative_base_keywords(asset_path(state, "keywords"))
    missing_keywords, qualifying_query_ids = traversal.traversal_records(
        state, authority
    )
    missing_ids = [item["keyword_id"] for item in missing_keywords]
    required_keyword_count = sum(
        item.get("direct_search", True) for item in authority["base_keywords"]
    )
    registration_complete = calculation["base_keyword_registration_complete"]

    common = {
        "city": str(state.get("city", "")).strip(),
        "city_state": str(city_state_path),
        "base_keyword_registration_complete": registration_complete,
        "required_base_keyword_count": required_keyword_count,
        "completed_base_keyword_count": len(qualifying_query_ids),
        "missing_base_keyword_count": len(missing_ids),
        "missing_base_keyword_ids": missing_ids,
        "coverage_next_strategy": calculation["next_strategy"],
        "base_search_saturated": calculation["base_search_saturated"],
        "keyword_deepening_complete": calculation.get("keyword_deepening_complete", False),
        "expansion_complete": calculation["expansion_complete"],
        "all_batches_closed": calculation["all_batches_closed"],
        "stop_candidate": calculation["stop_candidate"],
        "multi_agent_policy": {
            "required": True,
            "rule": "本轮查询数>=2 或 扩展计划数>=2 时必须创建至少2个Worker；仅1个Worker时必须提供fallback_reason",
            "valid_fallback_reasons": ["SINGLE_QUERY", "SINGLE_CANDIDATE", "SINGLE_EXPANSION_PLAN", "PLATFORM_UNSUPPORTED", "WORKER_UNAVAILABLE"],
            "enforcement": "search_worker_control.py merge 和 batch_context_control.py add 会校验多Agent执行记录，缺少合法manifest或fallback_reason时拒绝合并",
        },
    }

    if not registration_complete:
        return {
            "action": "BLOCKED_BASE_KEYWORD_REGISTRATION",
            "reason": "城市状态中的基础关键词注册与包内权威清单不一致",
            "registration": calculation["base_keyword_registration"],
            **common,
        }
    if calculation["next_strategy"] == "BASE_KEYWORD_TRAVERSAL":
        return {"action":"BASE_KEYWORD_TRAVERSAL", "reason":calculation["stop_or_continue_reason"], **common}
    if calculation["next_strategy"] == "TEST_DISCOVERED_KEYWORDS":
        return {
            "action": "TEST_DISCOVERED_KEYWORDS",
            "reason": "同类扩展阶段存在待测试DISCOVERED新词",
            "pending_discovered_keyword_ids": calculation["pending_discovered_keyword_ids"],
            **common,
        }
    if calculation["next_strategy"] == "REPLAY_APPROVED_KEYWORDS":
        return {
            "action": "REPLAY_APPROVED_KEYWORDS",
            "reason": "同类扩展阶段存在已批准但尚未回流的新词",
            "approved_keyword_pending_ids": calculation["approved_keyword_pending_ids"],
            **common,
        }
    if calculation["next_strategy"] == "YIELD_EXPANSION":
        return {
            "action": "YIELD_EXPANSION",
            "reason": calculation["stop_or_continue_reason"],
            "worker_creation_required": "强制主agent创建多Worker任务：扩展计划数>=2时必须拆分为至少2个research_task_id并行执行",
            "strategy_budget": calculation["strategy_budget"],
            "priority": calculation["priority"],
            "traceability_complete": calculation.get("expansion_traceability_complete", True),
            "recommended_command": {
                "script": "expansion_control.py",
                "command": "plan",
                "arguments": {
                    "--state": str(city_state_path),
                    "--seed-id": "<existing-final-candidate-id>",
                    "--feature-chain-id": "<controlled-feature-chain-id>",
                    "--feature-values": "<json-object>",
                    "--round-id": "<expansion-round-id>",
                    "--expansion-task-id": "<expansion-task-id>",
                    "--coverage-gap": "<target-gap>",
                    "--source-route": "<controlled-source-route>",
                    "--output": "<expansion-search-plan.json>",
                },
            },
            "post_plan_worker_command": {
                "script": "search_worker_control.py",
                "command": "create-discovery",
                "arguments": {
                    "--state": "<正式批次state.json>",
                    "--plan": "<expansion-search-plan.json>",
                    "--workers": ">=2（扩展计划数>=2时）",
                    "--manifest": "<worker-manifest.json>",
                },
            },
            **common,
        }
    if calculation["next_strategy"] == "KEYWORD_DEEPENING" and calculation.get("active_deepening_keyword_ids"):
        return {
            "action": "KEYWORD_DEEPENING",
            "reason": calculation["stop_or_continue_reason"],
            "pending_keyword_ids": calculation["active_deepening_keyword_ids"],
            "recommended_command": {"script": "keyword_deepening_control.py", "command": "plan",
                "arguments": {"--state": str(city_state_path), "--batch-size": batch_size,
                              "--keyword-id": calculation["active_deepening_keyword_ids"],
                              "--output": "<keyword-deepening-plan.json>"}},
            **common,
        }
    if calculation["next_strategy"] == "MANDATORY_COVERAGE" and calculation.get("blocking_gap_ids") and not calculation["next_task_ids"]:
        return {"action": "CLOSE_BLOCKING_GAPS", "reason": "先为未分配的覆盖缺口创建任务", **common}
    if calculation["next_strategy"] == "MANDATORY_COVERAGE" and calculation["next_task_ids"]:
        return {
            "action": "MANDATORY_COVERAGE",
            "reason": calculation["stop_or_continue_reason"],
            "next_task_ids": calculation["next_task_ids"],
            "recommended_command": {
                "script": "base_keyword_traversal_control.py",
                "command": "coverage-plan",
                "arguments": {
                    "--state": str(city_state_path),
                    "--task-id": calculation["next_task_ids"],
                    "--output": "<coverage-plan.json>",
                },
            },
            **common,
        }
    if calculation["next_strategy"] == "SYSTEM_SEARCH":
        action = "SYSTEM_SEARCH"
        payload = {
            "action": action,
            "reason": calculation["stop_or_continue_reason"],
            "worker_creation_required": "强制主agent创建多Worker任务：基础检索阶段查询数>=2时必须拆分为至少2个research_task_id并行执行",
            "approved_keyword_pending_ids": calculation.get("approved_keyword_pending_ids", []),
            "pending_discovered_keyword_ids": calculation.get("pending_discovered_keyword_ids", []),
            "blocking_gap_ids": calculation.get("blocking_gap_ids", []),
            "required_zero_candidate_rounds": policy_for(state)["system_search"]["zero_candidate_rounds"],
            "system_search_min_queries": policy_for(state)["system_search"]["min_queries"],
            "completed_zero_candidate_round_ids": calculation[
                "independent_zero_candidate_system_round_ids"
            ],
            "system_search_brief": system_search.brief(state, max(policy_for(state)["system_search"]["min_queries"], calculation["strategy_budget"])),
            "variant_generation_command": {
                "script": "base_keyword_traversal_control.py", "command": "variants",
                "arguments": {"--state": str(city_state_path), "--keyword-id": "<权威BK编号>",
                              "--intent": "<按目标选择检索用途>"},
            },
            "recommended_command": {
                "script": "system_search_plan_control.py", "command": "plan",
                "arguments": {"--state": str(city_state_path),
                              "--proposal": "<具体查询与来源步骤proposal.json>",
                              "--output": "<system-search-plan.json>"},
            },
            "plan_requirement": "主控从企业、设备、工序、项目与近期高产查询提取线索，构造有依据的CUSTOM查询；BK仅是可选锚点。通过文本去重、预算及溯源校验，再用--city-state分配Worker。",
            **common,
        }
        return payload
    if not calculation["all_batches_closed"]:
        return {
            "action": "CLOSE_OPEN_BATCHES",
            "reason": "基础检索和同类扩展已完成，但仍有批次未关闭",
            **common,
        }
    if calculation["stop_candidate"]:
        return {
            "action": "FREEZE_SEARCH",
            "reason": calculation["stop_or_continue_reason"],
            "recommended_command": {
                "script": "city_coverage_control.py",
                "command": "freeze",
                "arguments": {
                    "--state": str(city_state_path),
                    "--manifest": "<freeze-manifest.json>",
                },
            },
            **common,
        }
    return {
        "action": "BLOCKED_INCONSISTENT_SEARCH_STATE",
        "reason": "当前状态无法映射到合法的下一步检索动作",
        **common,
    }


def command_next_search_action(args: argparse.Namespace) -> None:
    payload = next_search_action_payload(
        Path(args.city_state),
        args.batch_size,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按绑定策略执行核验与交付分组")
    sub = parser.add_subparsers(dest="command", required=True)
    next_search = sub.add_parser(
        "next-search-action",
        help="根据基础关键词完成度和城市覆盖状态选择下一步检索动作",
    )
    next_search.add_argument("--city-state", required=True)
    next_search.add_argument("--batch-size", type=int, default=10)
    next_search.set_defaults(func=command_next_search_action)
    nxt = sub.add_parser("next", help="按绑定策略领取或恢复核验工作集")
    nxt.add_argument("--state", required=True)
    nxt.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    nxt.set_defaults(func=command_next)
    commit = sub.add_parser("commit-workset", help="一次提交当前工作集的全部分流结果")
    commit.add_argument("--state", required=True)
    commit.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    commit.add_argument("--result", required=True)
    commit.set_defaults(func=command_commit_workset)
    close = sub.add_parser("close", help="关闭已完成批次、累计FINAL并在需要时创建下一批")
    close.add_argument("--state", required=True)
    close.add_argument("--caller-token", required=True, help="主控调用方令牌；研究子 Agent 禁止使用写命令")
    close.add_argument("--city-state")
    close.add_argument("--query-log")
    close.add_argument("--evidence")
    close.add_argument("--queries-executed", type=int)
    close.add_argument("--shortfall-reason-code", choices=sorted(batch.SHORTFALL_REASON_CODES))
    close.add_argument("--reason", default="")
    close.add_argument("--strategy-signature", default="")
    close.add_argument("--new-types-added", type=int, default=0)
    close.add_argument("--gaps-closed", type=int, default=0)
    close.add_argument("--round-status", choices=["AUTO", "OPEN", "COMPLETED", "EARLY_STOPPED"], default="AUTO")
    close.set_defaults(func=command_close)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except (
        RunnerError,
        batch.ControlError,
        traversal.TraversalControlError,
        city_coverage.CoverageError,
        StateGuardError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"搜索阶段运行失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
