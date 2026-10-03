#!/usr/bin/env python3
"""Deterministic keyword-level deepening plan and completion audit."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
import base_keyword_traversal_control as traversal

class DeepeningError(ValueError): pass
def text(value): return str(value or "").strip()
def digest(value): return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
def policy(authority):
    value = authority.get("deepening_policy", {})
    if not value.get("families"): raise DeepeningError("base-keywords.json缺少deepening_policy.families")
    return value
def effective_queries(state):
    return [q for q in state.get("queries", []) if q.get("query_purpose") == "KEYWORD_DEEPENING" and q.get("termination_reason") in {"BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED"} and q.get("variant_key")]
def calculate(state, authority):
    rules, by_keyword = policy(authority)["families"], {}
    for query in effective_queries(state): by_keyword.setdefault(text(query.get("keyword_id")), []).append(query)
    items = []
    for keyword in authority.get("base_keywords", []):
        if not keyword.get("direct_search", True): continue
        required = list(rules.get(keyword.get("keyword_family"), {}).get("required_intents", []))
        completed = sorted({text(q.get("search_intent")) for q in by_keyword.get(keyword["keyword_id"], [])})
        missing = [intent for intent in required if intent not in completed]
        items.append({"keyword_id": keyword["keyword_id"], "keyword_family": keyword["keyword_family"], "required_intents": required, "completed_intents": completed, "missing_intents": missing, "effective_query_count": len(by_keyword.get(keyword["keyword_id"], [])), "status": "MINIMUM_COVERED" if not missing else ("IN_PROGRESS" if completed else "NOT_STARTED")})
    pending = [item for item in items if item["missing_intents"]]
    return {"policy_version": policy(authority).get("schema_version", "1.0.0"), "required_keyword_count": len(items), "completed_keyword_count": len(items)-len(pending), "pending_keyword_ids": [item["keyword_id"] for item in pending], "keyword_deepening_complete": not pending, "keywords": items}
def build_plan(state_path, state, authority, templates, batch_size, keyword_ids=None):
    if batch_size < 1: raise DeepeningError("--batch-size必须大于0")
    audit = calculate(state, authority)
    if audit["keyword_deepening_complete"]: raise DeepeningError("关键词深挖已完成")
    keywords = {item["keyword_id"]: item for item in authority["base_keywords"]}
    tasks = [item for item in state.get("tasks", []) if item.get("task_id")]
    if not tasks: raise DeepeningError("城市状态没有可复用搜索任务")
    pending = sorted((item for item in audit["keywords"] if item["missing_intents"]), key=lambda item: (item["effective_query_count"], item["keyword_id"]))
    if keyword_ids:
        selected = set(keyword_ids)
        pending = [item for item in pending if item["keyword_id"] in selected]
        if not pending: raise DeepeningError("指定BK均已完成深挖")
    history, rows = list(state.get("queries", [])), []
    for item in pending:
        if len(rows) >= batch_size: break
        keyword, intent = keywords[item["keyword_id"]], item["missing_intents"][0]
        intent_cfg = templates["purpose_variants"]["intents"][intent]
        task = tasks[(len(history)+len(rows)) % len(tasks)]
        raw = {"search_intent": intent, "variant_mode": "CONTROLLED", "variant_context": [], "discovery_path": intent_cfg["allowed_discovery_paths"][0]}
        rendered = traversal.construct_system_query(text(state.get("city")), keyword, templates, raw, history+rows)
        variant_key = "|".join([keyword["keyword_id"], intent, rendered["variant_id"], rendered.get("combination_term", ""), text(task.get("source_type")) or "公开网页"])
        attempt = 1 + sum(q.get("keyword_id") == keyword["keyword_id"] and q.get("query_purpose") == "KEYWORD_DEEPENING" for q in history)
        row = {**rendered, "query_id": f"KDQ-{keyword['keyword_id']}-{attempt:02d}", "search_task_id": task["task_id"], "query_purpose": "KEYWORD_DEEPENING", "keyword_id": keyword["keyword_id"], "keyword_family": keyword["keyword_family"], "source_type": text(task.get("source_type")) or "公开网页", "district": text(task.get("district")), "role": text(task.get("role")), "industry": text(task.get("industry")), "variant_key": variant_key, "deepening_reason": "REQUIRED_INTENT_GAP"}
        if any(q.get("variant_key") == variant_key for q in history+rows): raise DeepeningError("生成了重复variant_key："+variant_key)
        rows.append(row)
    result = {"schema_version": 1, "plan_type": "KEYWORD_DEEPENING", "city": text(state.get("city")), "state_path": str(state_path.resolve()), "input_version": digest(state), "policy_version": audit["policy_version"], "search_tasks": rows}
    result["plan_id"] = digest(result)
    return result
def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("command", choices=["status","plan"]); parser.add_argument("--state", required=True); parser.add_argument("--output"); parser.add_argument("--batch-size", type=int, default=20); parser.add_argument("--keyword-id", action="append"); args=parser.parse_args()
    try:
        state_path=Path(args.state); state,authority,templates=traversal.load_context(state_path); result=calculate(state,authority) if args.command=="status" else build_plan(state_path,state,authority,templates,args.batch_size,args.keyword_id)
        if args.output: Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except (DeepeningError,traversal.TraversalControlError,OSError,ValueError) as exc: parser.exit(2,str(exc)+"\n")
if __name__ == "__main__": raise SystemExit(main())
