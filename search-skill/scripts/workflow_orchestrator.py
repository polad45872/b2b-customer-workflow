#!/usr/bin/env python3
"""Deterministic search workflow orchestrator.

This controller advances every code-computable transition and stops only when
external research or semantic judgement is required.  It replaces the former
pattern where an LLM had to remember which CLI command to construct next.
"""
from __future__ import annotations

from b2b_config import policy_for
import argparse
import json
from pathlib import Path

import base_keyword_traversal_control as traversal
import batch_context_control as batch
import city_coverage_control as coverage
import search_stage_runner as runner
import system_search_plan_control as system_search
import keyword_deepening_control as keyword_deepening
from state_guard import StateGuardError, verify_caller_token


class OrchestratorError(RuntimeError):
    pass


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_next(city_state_path: Path, output_dir: Path, batch_size: int,
                 caller_token: str = "", proposal_path: Path | None = None) -> dict:
    """Materialize deterministic repairs and generate the next executable plan."""
    city_state_path = city_state_path.resolve()
    output_dir = output_dir.resolve()
    action = runner.next_search_action_payload(city_state_path, batch_size)
    if action["action"] == "CLOSE_BLOCKING_GAPS":
        verify_caller_token(caller_token, state_path=city_state_path)
        state = coverage.load_json(city_state_path)
        materialized = coverage.materialize_blocking_gap_tasks(state)
        state["latest_calculation"] = coverage.calculate(state)
        coverage.sync_coverage_followups(state, state["latest_calculation"])
        coverage.save_json(city_state_path, state)
        action = runner.next_search_action_payload(city_state_path, batch_size)
        action["materialized_gap_tasks"] = materialized

    state, authority, templates = traversal.load_context(city_state_path)
    plan_path = output_dir / "next-search-plan.json"
    if action["action"] == "BASE_KEYWORD_TRAVERSAL":
        safe_batch_size = min(batch_size, policy_for(state)["base_keyword_batch_max"], max(1, len(state.get("tasks", []))))
        plan = traversal.build_plan(city_state_path, state, authority, templates, safe_batch_size)
        action["selected_keyword_ids"] = [item["keyword_id"] for item in plan["search_tasks"]]
        write_json(plan_path, plan)
    elif action["action"] == "MANDATORY_COVERAGE":
        plan = traversal.build_coverage_plan(
            city_state_path, state, authority, templates,
            action.get("next_task_ids", []), batch_size,
        )
        write_json(plan_path, plan)
    elif action["action"] == "KEYWORD_DEEPENING":
        plan = keyword_deepening.build_plan(city_state_path, state, authority, templates,
                                            batch_size, action.get("pending_keyword_ids"))
        write_json(plan_path, plan)
    elif action["action"] == "SYSTEM_SEARCH":
        system_search.require_system_stage(state)
        if proposal_path is None:
            return {**action, "next_external_action": "CONSTRUCT_QUERY_PROPOSAL",
                    "proposal_schema": "system_search_plan_control.py plan --proposal <具体线索查询JSON>",
                    "planning_brief": system_search.brief(state, max(policy_for(state)["system_search"]["min_queries"], action.get("strategy_budget", policy_for(state)["system_search"]["budget"])))}
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        plan = system_search.make_plan(state, proposal)
        write_json(plan_path, plan)
    else:
        return action
    return {
        **action,
        "plan_path": str(plan_path),
        "plan_type": plan["plan_type"],
        "round_id": plan.get("round_id", ""),
        "query_count": len(plan["search_tasks"]),
        "next_external_action": "EXECUTE_DISCOVERY_WORKERS",
    }


def batch_next_action(batch_state_path: Path, max_followup_attempts: int = 2) -> dict:
    state = batch.load_state(batch_state_path)
    batch.ensure_open(state)
    pending_records = [rid for rid, row in state.get("discovery_records", {}).items()
                       if row.get("disposition") == "PENDING"]
    if pending_records:
        return {"action": "AUTO_RESOLVE_DISCOVERY", "record_ids": pending_records}
    if state.get("active_workset"):
        return {"action": "VERIFY_ACTIVE_WORKSET", "executor": "CONTROLLER", "next_command": "search_stage_runner.py next; controller verification; search_stage_runner.py commit-workset", "candidate_ids": state["active_workset"]}
    unverified = [item["candidate_id"] for item in state.get("candidates", [])
                  if item.get("status") == "UNVERIFIED"]
    if unverified:
        return {"action": "START_VERIFICATION_WORKSET", "executor": "CONTROLLER", "next_command": "search_stage_runner.py next", "candidate_ids": unverified[:batch.limits_for(state)["workset_max"]]}
    retryable = [item["candidate_id"] for item in state.get("candidates", [])
                 if item.get("status") == "FOLLOWUP"
                 and item.get("followup_reason") in {"IDENTITY_PENDING", "LOCATION_PENDING"}
                 and int(item.get("followup_attempts", 0)) < max_followup_attempts]
    if retryable:
        return {"action": "RETRY_FOLLOWUP", "candidate_ids": retryable[:batch.limits_for(state)["workset_max"]]}
    unresolved = [item["candidate_id"] for item in state.get("candidates", [])
                  if item.get("status") == "CARRYOVER"]
    if unresolved:
        return {"action": "SEMANTIC_REVIEW_REQUIRED", "candidate_ids": unresolved}
    return {"action": "CLOSE_BATCH", "round_status": "AUTO"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-next", help="自动修复确定性缺口并生成下一阶段计划")
    prepare.add_argument("--city-state", required=True)
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--batch-size", type=int, default=10)
    prepare.add_argument("--caller-token", default="")
    prepare.add_argument("--proposal", help="主控依据候选线索构造的SYSTEM_SEARCH查询计划")
    batch_action = sub.add_parser("batch-next", help="计算批次内部下一原子动作")
    batch_action.add_argument("--state", required=True)
    batch_action.add_argument("--max-followup-attempts", type=int, default=2)
    args = parser.parse_args()
    try:
        if args.command == "prepare-next":
            result = prepare_next(Path(args.city_state), Path(args.output_dir),
                                  args.batch_size, args.caller_token, Path(args.proposal) if args.proposal else None)
        else:
            result = batch_next_action(Path(args.state), args.max_followup_attempts)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OrchestratorError, traversal.TraversalControlError, batch.ControlError,
            coverage.CoverageError, system_search.PlanError, keyword_deepening.DeepeningError, StateGuardError,
            OSError, ValueError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
