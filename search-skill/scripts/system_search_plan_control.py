#!/usr/bin/env python3
"""SYSTEM_SEARCH: auditable allocation, proposal validation and execution feedback.

No network calls or formal-state writes. The controller proposes real queries and
source entries from the brief; this module validates and seals the executable plan.
"""
from __future__ import annotations

from b2b_config import policy_for, asset_path, check_if_bound, profile_for

import argparse
from collections import Counter, defaultdict
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import unicodedata

VERSION = "system-search-v1"
METHODS = {"LIST_EXTRACTION", "PRODUCT_PROCESS", "HIRING_PROCUREMENT", "PROJECT_TRACE"}
NORMAL_STOPS = {"BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED", "STRATEGY_STOP"}

def branch_exhausted(state, keyword_id, discovery_path):
    """Only two completed zero-new-pool searches stop an optional BK discovery path."""
    history = [q for q in state.get("queries", [])
               if q.get("query_purpose") == "SYSTEM_SEARCH"
               and q.get("keyword_id") == keyword_id
               and q.get("discovery_path") == discovery_path
               and q.get("termination_reason") in {"BUDGET_EXHAUSTED", "RESULTS_EXHAUSTED"}]
    if len(history) < 2:
        return False
    ledger = state.get("discovery_records", {})
    for query in history[-2:]:
        if any(row.get("disposition") == "ADDED" and row.get("counts_as_unique_entity")
               and any(p.get("query_id") == query.get("query_id")
                       for p in row.get("record", {}).get("provenance", []))
               for row in ledger.values()):
            return False
    return True
FIELDS = ("system_policy", "system_plan_id", "round_id", "path_id", "discovery_path",
          "source_entry", "execution_steps", "allocation_bucket", "selection_basis",
          "target_gap_ids", "change_basis", "retry_reason", "duplicate_check")
FIELDS += ("basis_query_ids", "search_intent", "variant_mode", "variant_id",
           "variant_template_version", "combination_term", "variant_context", "construction_reason")
EXECUTION_FIELDS = ("visited_source_urls",)


class PlanError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def normalized(text):
    # Preserve search operators, quotes, hyphens and substantive terms.
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def comparable(text):
    text = normalized(text)
    for word in ("企业名录", "企业名单", "公司名录", "公司名单", "黄页", "名录"):
        text = text.replace(word, "名单")
    return " ".join(sorted(text.split()))


def compare(query, history):
    matches = []
    for old in history:
        a, b = normalized(query["query_text"]), normalized(old.get("query_text", ""))
        if not b:
            continue
        score = SequenceMatcher(None, comparable(a), comparable(b)).ratio()
        kind = "EXACT" if a == b else "NEAR" if score >= .86 else ""
        if kind:
            matches.append({"query_id": old.get("query_id"), "kind": kind,
                            "similarity": round(score, 4),
                            "same_route": query.get("discovery_path") == old.get("discovery_path") and (
                                query.get("source_entry") == old.get("source_entry") or bool(
                                    set(query.get("visited_source_urls", [])).intersection(old.get("visited_source_urls", []))))})
    return matches


def path_id(task):
    keys = ("discovery_path", "keyword_family", "role", "industry", "source_type")
    if task.get("search_intent"):
        keys += ("search_intent",)
    return "PATH-" + digest([task.get(k, "") for k in keys])[:16]


def history_version(state):
    return digest({k: state.get(k, []) for k in ("queries", "candidates", "tasks", "gaps", "rounds", "base_keywords", "search_space", "b2b_config")})


def path_statistics(state):
    queries = state.get("queries", [])
    by_id = {q["query_id"]: q for q in queries}
    credits = defaultdict(float)
    finals = defaultdict(float)
    rejected_links = 0
    seen_companies = set()
    for candidate in state.get("candidates", []):
        key = candidate.get("dedupe_key") or candidate.get("company_name") or candidate.get("candidate_id")
        if key in seen_companies:
            continue
        seen_companies.add(key)
        texts = set(candidate.get("discovery_queries", []))
        linked = []
        for qid in candidate.get("discovered_by_query_ids", []):
            if qid in by_id and by_id[qid].get("query_text") in texts:
                linked.append(qid)
            else:
                rejected_links += 1
        for qid in set(linked):
            credits[qid] += 1 / len(set(linked))
            finals[qid] += (candidate.get("status") == "FINAL") / len(set(linked))
    groups = {}
    seen_text = set()
    for q in queries:
        if q.get("discovery_path") not in METHODS or not q.get("search_intent"):
            continue  # Legacy labels cannot establish a genuine discovery path.
        pid = path_id(q)
        group = groups.setdefault(pid, {"path_id": pid, "sample": {k: q.get(k, "") for k in
            ("discovery_path", "keyword_family", "role", "industry", "source_type", "search_intent")},
            "effective_queries": 0, "candidate_credit": 0., "final_credit": 0.,
            "blocked_queries": 0, "query_ids": [], "rounds": {}})
        blocked = q.get("termination_reason") == "SOURCE_BLOCKED"
        group["blocked_queries"] += blocked
        rid = q.get("round_id") or q.get("batch_id")
        counts = group["rounds"].setdefault(rid, [0, 0., 0])
        counts[2] += blocked
        norm = normalized(q.get("query_text", ""))
        effective = (q.get("termination_reason") in NORMAL_STOPS and bool(q.get("visited_source_urls"))
                     and norm not in seen_text)
        seen_text.add(norm)
        if not effective:
            continue
        group["effective_queries"] += 1
        group["query_ids"].append(q["query_id"])
        group["candidate_credit"] += credits[q["query_id"]]
        group["final_credit"] += finals[q["query_id"]]
        counts[0] += 1
        counts[1] += credits[q["query_id"]]
    for g in groups.values():
        n = g["effective_queries"]
        streak = 0
        for count, credit, blocked in g.pop("rounds").values():
            streak = streak + 1 if count >= 3 and credit == 0 and blocked == 0 else 0
        g["zero_yield_streak"] = streak
        # Discovery efficiency, smoothed by five queries. FINAL is diagnostic only.
        g["score"] = round(g["candidate_credit"] / (n + 5) * (.25 if streak >= 2 else 1), 6)
        g["eligible_high_yield"] = n >= 5 and g["candidate_credit"] > 0 and streak < 2
    return sorted(groups.values(), key=lambda g: (-g["score"], g["path_id"])), rejected_links


def brief(state, budget=20):
    from base_keyword_traversal_control import load_templates, intent_catalog
    catalog = intent_catalog(load_templates(asset_path(state, "keywords")))
    if budget < policy_for(state)["system_search"]["min_queries"]:
        raise PlanError("发现轮次有效查询预算低于绑定策略下限")
    stats, rejected = path_statistics(state)
    high = [x for x in stats if x["eligible_high_yield"]]
    gaps = [g["gap_id"] for g in state.get("gaps", []) if g.get("status") in {"OPEN", "IN_PROGRESS"}]
    gaps += [t["task_id"] for t in state.get("tasks", []) if t.get("required", True)
             and t.get("status") not in {"COMPLETED", "BLOCKED_ALLOWED"}]
    coverage_targets = []
    if state.get("search_space"):
        from city_coverage_control import calculate
        metrics = calculate(state)["metrics"]
        for metric in metrics:
            if metric.get("metric_name", metric.get("name")) in {"区县覆盖率", "角色覆盖率", "来源覆盖率", "关键词族覆盖率", "产业覆盖率"}:
                for value in metric.get("failed_item_ids", []):
                    target = "COVERAGE:" + metric.get("metric_name", metric.get("name")) + ":" + str(value)
                    gaps.append(target)
                    coverage_targets.append({"target_id": target, "metric": metric.get("metric_name", metric.get("name")), "value": value})
    high_n = int(budget * .60) if high else 0
    gap_n = int(budget * .25) if gaps else 0
    # Rank before allocation; controller cannot arbitrarily pick any historical path.
    selected = high[:3]
    quotas = {}
    if selected:
        total = sum(x["score"] for x in selected)
        shares = [(x["path_id"], high_n * x["score"] / total) for x in selected]
        quotas = {pid: int(n) for pid, n in shares}
        for pid, _ in sorted(shares, key=lambda x: (-(x[1] % 1), x[0]))[:high_n-sum(quotas.values())]:
            quotas[pid] += 1
    return {"policy": VERSION, "history_version": history_version(state), "budget": budget,
            "allocation": {"HIGH_YIELD": high_n, "GAP": gap_n, "EXPLORE": budget-high_n-gap_n},
            "mode": "HISTORY_GUIDED" if high else "COLD_START",
            "high_yield_path_ids": [x["path_id"] for x in high], "target_gap_ids": sorted(set(gaps)),
            "high_yield_path_quotas": quotas, "coverage_targets": coverage_targets,
            "path_statistics": stats, "untrusted_candidate_links": rejected,
            "intent_catalog": catalog, "variant_config_hash": digest(catalog),
            "instructions": "优先沿已发现企业、产品服务、业务场景、项目及高产查询构造具体查询；BK仅作可追溯锚点。根据新增入池候选反馈选择路径，低增量时补覆盖缺口。每条查询说明具体线索与来源。"}


def make_plan(state, proposal):
    from base_keyword_traversal_control import load_templates, construct_system_query, TraversalControlError
    templates = load_templates(asset_path(state, "keywords"))
    tasks = proposal.get("search_tasks", [])
    budget = proposal.get("budget", 20)
    guide = brief(state, budget)
    discovery_first = proposal.get("planning_mode", "DISCOVERY_FIRST") == "DISCOVERY_FIRST"
    rid = proposal.get("round_id")
    if not rid or any(r.get("round_id") == rid for r in state.get("rounds", [])):
        raise PlanError("必须提供尚未使用的SYSTEM_SEARCH round_id；续批复用原计划")
    if not isinstance(tasks, list) or not tasks:
        raise PlanError("缺少search_tasks")
    known_tasks = {t["task_id"] for t in state.get("tasks", [])}
    known_keywords = {k["keyword_id"]: k for k in state.get("base_keywords", [])}
    counts, high_counts, methods, ids = Counter(), Counter(), set(), set()
    history = list(state.get("queries", []))
    output = []
    for raw in tasks:
        q = dict(raw)
        q.setdefault("keyword_id", "")
        for field in ("district", "role", "industry"):
            q.setdefault(field, "")
        required = ("query_id", "search_task_id", "keyword_family",
                    "source_type", "discovery_path", "source_entry", "selection_basis", "allocation_bucket")
        if any(not q.get(k) for k in required):
            raise PlanError("查询缺少必填字段：" + ",".join(k for k in required if not q.get(k)))
        if q["query_id"] in ids or any(x.get("query_id") == q["query_id"] for x in history):
            raise PlanError("查询ID重复")
        ids.add(q["query_id"])
        if q["search_task_id"] not in known_tasks:
            raise PlanError("搜索任务不可反查")
        kw = known_keywords.get(q.get("keyword_id"))
        if kw:
            try:
                q = construct_system_query(state.get("city", ""), kw, templates, q, history)
            except TraversalControlError as exc:
                raise PlanError(str(exc)) from exc
            terms = kw.get("match_terms") or [kw["keyword"]]
            if not any(normalized(t) in normalized(q["query_text"]) for t in terms):
                raise PlanError("查询未命中所引用的基础关键词")
            if kw.get("requires_combination") and not any(normalized(t) in normalized(q["query_text"]) for t in kw.get("combination_with", [])):
                raise PlanError("组合型BK查询缺少受控组合词")
            if q["keyword_family"] != kw["keyword_family"]:
                raise PlanError("关键词族与BK不符")
        elif discovery_first and not q.get("keyword_id") and q.get("variant_mode") == "CUSTOM":
            if not q.get("query_text") or not q.get("construction_reason") or not q.get("search_intent"):
                raise PlanError("线索驱动查询必须注明具体检索式、用途及构造依据")
            if normalized(str(state.get("city", "")).removesuffix("市")) not in normalized(q["query_text"]):
                raise PlanError("线索驱动查询必须包含目标城市")
            q.update(variant_id="CUSTOM", variant_template_version=templates["purpose_variants"]["schema_version"],
                     combination_term="", variant_context=q.get("variant_context", []))
        else:
            raise PlanError("非BK查询必须采用有依据的CUSTOM模式")
        if q["discovery_path"] not in METHODS:
            raise PlanError("发现路径非法")
        steps = q.get("execution_steps")
        if not isinstance(steps, list) or len(steps) < 2 or any(not isinstance(s, str) or not s.strip() for s in steps):
            raise PlanError("必须提供至少两个具体发现步骤")
        q.update(query_purpose="SYSTEM_SEARCH", round_id=rid, system_policy=VERSION, path_id=path_id(q))
        matches = compare(q, history)
        exact = any(m["kind"] == "EXACT" for m in matches)
        near = [m for m in matches if m["kind"] == "NEAR"]
        if exact and (discovery_first or not q.get("retry_reason")):
            raise PlanError(f"完全重复查询必须替换或记录重试理由：{q['query_id']}")
        if near and not exact and not discovery_first:
            old_by_id = {x.get("query_id"): x for x in history}
            changed = all(any((q.get(k) or "") != (old_by_id[m["query_id"]].get(k) or "") for k in
                          ("discovery_path", "search_intent", "source_entry", "execution_steps", "district", "role", "industry")) for m in near)
            if not changed or not q.get("change_basis"):
                raise PlanError(f"近似查询必须有实际路径或对象变化及change_basis：{q['query_id']}")
        q["duplicate_check"] = {"matches": matches, "effective_for_budget": not exact}
        bucket = q["allocation_bucket"]
        if bucket not in guide["allocation"]:
            raise PlanError("非法预算桶")
        if not discovery_first and bucket == "HIGH_YIELD" and q["path_id"] not in guide["high_yield_path_ids"]:
            raise PlanError("高产查询没有合格历史路径依据")
        q["basis_query_ids"] = next((g["query_ids"] for g in guide["path_statistics"] if g["path_id"] == q["path_id"]), []) if bucket == "HIGH_YIELD" else []
        if not discovery_first and bucket == "GAP" and not set(q.get("target_gap_ids", [])).intersection(guide["target_gap_ids"]):
            raise PlanError("缺口查询没有开放缺口或待办任务依据")
        if bucket == "GAP" and not discovery_first:
            dimensions = {"区县覆盖率":"district", "角色覆盖率":"role", "产业覆盖率":"industry", "来源覆盖率":"source_type", "关键词族覆盖率":"keyword_family"}
            for target in guide["coverage_targets"]:
                if target["target_id"] in q.get("target_gap_ids", []) and q.get(dimensions[target["metric"]]) != target["value"]:
                    raise PlanError("查询维度与所补覆盖目标不一致")
        if not exact:
            counts[bucket] += 1
            if bucket == "HIGH_YIELD":
                high_counts[q["path_id"]] += 1
            if bucket == "EXPLORE":
                methods.add(q["discovery_path"])
        output.append(q)
        history.append(q)
    if not discovery_first and dict(counts) != {k: v for k, v in guide["allocation"].items() if v}:
        raise PlanError(f"有效查询分配不符：actual={dict(counts)}, expected={guide['allocation']}")
    if not discovery_first and dict(high_counts) != {k: v for k, v in guide["high_yield_path_quotas"].items() if v}:
        raise PlanError("高产路径配额不符合历史得分排序")
    required_methods = min(guide["allocation"]["EXPLORE"], 3 if guide["mode"] == "COLD_START" else 2)
    if not discovery_first and len(methods) < required_methods:
        raise PlanError("探索预算的发现路径种类不足")
    if discovery_first and sum(counts.values()) != budget:
        raise PlanError("发现轮次有效查询数必须等于预算")
    result = {"schema_version": 1, "plan_type": "SYSTEM_SEARCH", "policy": VERSION,
              "round_id": rid, "budget": budget, "planning_mode": "DISCOVERY_FIRST" if discovery_first else "LEGACY", "decision": guide, "search_tasks": output}
    result["plan_id"] = digest(result)
    for q in output:
        q["system_plan_id"] = result["plan_id"]
    return result


def next_round_id(state):
    numbers = []
    for item in state.get("rounds", []):
        match = re.fullmatch(r"R(\d+)", str(item.get("round_id", "")))
        if match:
            numbers.append(int(match.group(1)))
    return f"R{max(numbers, default=0) + 1:03d}"


def auto_proposal(state, budget=20, round_id=""):
    """Build a controlled proposal without inventing companies, projects or URLs."""
    from base_keyword_traversal_control import load_templates
    templates = load_templates(asset_path(state, "keywords"))
    guide = brief(state, budget)
    keywords = [item for item in state.get("base_keywords", []) if item.get("direct_search", True)]
    tasks = list(state.get("tasks", []))
    if not keywords or not tasks:
        raise PlanError("自动计划需要非空基础词和覆盖任务")
    intents = templates["purpose_variants"]["intents"]
    source_types = list(state.get("search_space", {}).get("source_types", [])) or ["公开网页"]
    task_by_id = {item["task_id"]: item for item in tasks}
    gap_by_id = {item["gap_id"]: item for item in state.get("gaps", [])}
    coverage_target_by_id = {item["target_id"]: item for item in guide.get("coverage_targets", [])}
    high_by_id = {item["path_id"]: item for item in guide["path_statistics"]}
    rid = round_id or next_round_id(state)
    rows = []

    def compatible_keyword(family="", intent="", discovery_path=""):
        options = [k for k in keywords if (not family or k.get("keyword_family") == family)]
        if intent:
            options = [k for k in options if k.get("keyword_family") in intents[intent]["applicable_families"]]
        if not options:
            options = [k for k in keywords if not intent or k.get("keyword_family") in intents[intent]["applicable_families"]]
        if not options:
            raise PlanError("没有与自动计划用途兼容的权威BK")
        if discovery_path:
            options = [k for k in options if not branch_exhausted(state, k["keyword_id"], discovery_path)]
        if not options:
            raise PlanError("当前关键词族及发现路径均已连续两次零新增；请选择新的路径或用途")
        return options[len(rows) % len(options)]

    def append_row(bucket, sample=None, target_id=""):
        sample = dict(sample or {})
        task = task_by_id.get(target_id) or tasks[len(rows) % len(tasks)]
        dimensions = {key: sample.get(key, "") or task.get(key, "") for key in ("district", "role", "industry")}
        source_type = sample.get("source_type") or task.get("source_type") or source_types[len(rows) % len(source_types)]
        family = sample.get("keyword_family", "")
        intent = sample.get("search_intent", "")
        if not intent:
            possible = [name for name, cfg in intents.items() if not family or family in cfg["applicable_families"]]
            intent = possible[len(rows) % len(possible)]
        allowed_paths = intents[intent]["allowed_discovery_paths"]
        discovery_path = sample.get("discovery_path") or allowed_paths[len(rows) % len(allowed_paths)]
        if discovery_path not in allowed_paths:
            discovery_path = allowed_paths[0]
        if all(branch_exhausted(state, k["keyword_id"], discovery_path) for k in keywords
               if (not family or k.get("keyword_family") == family)
               and k.get("keyword_family") in intents[intent]["applicable_families"]):
            discovery_path = next((path for path in allowed_paths
                                   if any(not branch_exhausted(state, k["keyword_id"], path)
                                          for k in keywords if (not family or k.get("keyword_family") == family)
                                          and k.get("keyword_family") in intents[intent]["applicable_families"])), discovery_path)
        keyword = compatible_keyword(family, intent, discovery_path)
        family = keyword["keyword_family"]
        target_ids = [target_id] if bucket == "GAP" and target_id else []
        gap = gap_by_id.get(target_id, {})
        field = gap.get("dimension")
        if field in dimensions and gap.get("value"):
            dimensions[field] = gap["value"]
        target = coverage_target_by_id.get(target_id, {})
        metric_fields = {"区县覆盖率": "district", "角色覆盖率": "role", "产业覆盖率": "industry",
                         "来源覆盖率": "source_type", "关键词族覆盖率": "keyword_family"}
        target_field = metric_fields.get(target.get("metric"))
        if target_field in dimensions:
            dimensions[target_field] = target.get("value", "")
        elif target_field == "source_type":
            source_type = target.get("value", source_type)
        elif target_field == "keyword_family":
            requested_family = target.get("value", family)
            keyword = compatible_keyword(requested_family, intent, discovery_path)
            family = keyword["keyword_family"]
        rows.append({
            "query_id": f"SYS-{rid}-{len(rows)+1:03d}",
            "search_task_id": task["task_id"],
            "keyword_id": keyword["keyword_id"],
            "keyword_family": family,
            "source_type": source_type,
            "discovery_path": discovery_path,
            "source_entry": f"PUBLIC_SOURCE:{source_type}",
            "selection_basis": f"{bucket}预算由system_search brief自动分配",
            "allocation_bucket": bucket,
            "search_intent": intent,
            "variant_mode": "CONTROLLED",
            "variant_context": [],
            "execution_steps": [f"在{source_type}入口执行受控检索式", "逐条打开结果并记录实际访问URL与企业线索"],
            "target_gap_ids": target_ids,
            "change_basis": "自动轮换权威BK、用途、来源或发现路径",
            "retry_reason": "",
            **dimensions,
        })

    for pid, count in guide["high_yield_path_quotas"].items():
        for _ in range(count):
            append_row("HIGH_YIELD", sample=high_by_id[pid]["sample"])
    targets = guide["target_gap_ids"]
    for index in range(guide["allocation"]["GAP"]):
        append_row("GAP", target_id=targets[index % len(targets)])
    method_order = ["LIST_EXTRACTION", "PRODUCT_PROCESS", "HIRING_PROCUREMENT", "PROJECT_TRACE"]
    for index in range(guide["allocation"]["EXPLORE"]):
        intent = list(intents)[index % len(intents)]
        allowed = intents[intent]["allowed_discovery_paths"]
        discovery_path = next((m for m in method_order[index % len(method_order):] + method_order[:index % len(method_order)] if m in allowed), allowed[0])
        append_row("EXPLORE", sample={"search_intent": intent, "discovery_path": discovery_path})
    return {"round_id": rid, "budget": budget, "search_tasks": rows}


def validate_plan(state, plan):
    proposal = {k: plan[k] for k in ("round_id", "budget", "search_tasks", "planning_mode")}
    proposal["search_tasks"] = [{k: v for k, v in q.items() if k != "system_plan_id"} for q in proposal["search_tasks"]]
    expected = make_plan(state, proposal)
    if expected != plan:
        raise PlanError("SYSTEM_SEARCH计划已变化、历史已更新或计算依据不一致；请重建计划")


def round_audit(state, round_item):
    queries = [q for q in state.get("queries", []) if q.get("round_id") == round_item["round_id"]]
    reasons = []
    plans = state.get("system_search_plans", {})
    plan_ids = {q.get("system_plan_id") for q in queries}
    if len(plan_ids) != 1 or not next(iter(plan_ids), None) in plans:
        reasons.append("MISSING_VALIDATED_PLAN")
    else:
        plan = plans[next(iter(plan_ids))]
        planned = {q["query_id"]: q for q in plan["search_tasks"]}
        for q in queries:
            if any(q.get(k) != planned.get(q["query_id"], {}).get(k) for k in FIELDS + ("query_text",)):
                reasons.append("EXECUTION_PLAN_MISMATCH")
        if {q["query_id"] for q in queries} != {q["query_id"] for q in plan["search_tasks"]}:
            reasons.append("INCOMPLETE_PLAN_EXECUTION")
    effective = []
    history = []
    target_ids = {q["query_id"] for q in queries}
    for q in state.get("queries", []):
        if q.get("query_id") in target_ids:
            if q.get("query_purpose") != "SYSTEM_SEARCH" or q.get("system_policy") != VERSION:
                reasons.append("LEGACY_OR_MIXED_ROUND")
            elif q.get("termination_reason") not in NORMAL_STOPS:
                reasons.append("SOURCE_BLOCKED_OR_INVALID_STOP")
            elif not isinstance(q.get("visited_source_urls"), list):
                reasons.append("MISSING_SOURCE_URL_LIST")
            elif not any(x["kind"] == "EXACT" for x in compare(q, history)):
                effective.append(q)
        history.append(q)
    if len(effective) < policy_for(state)["system_search"]["min_queries"]:
        reasons.append("INSUFFICIENT_EFFECTIVE_QUERIES")
    return {"eligible": not reasons, "reasons": sorted(set(reasons)),
            "executed_queries": len(queries), "effective_queries": len(effective),
            "queries": effective}


def independent_round(current, previous):
    if not previous:
        return True
    # At least half of the queries must provide a new text/object or actual route.
    overlap = sum(any(m["same_route"] for m in compare(q, previous)) for q in current)
    return overlap / max(1, len(current)) < .5


def require_system_stage(state):
    from city_coverage_control import calculate
    gate = calculate(state)
    if gate["stop_candidate"]:
        raise PlanError("城市已满足停止条件")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["brief", "plan", "auto-plan", "validate-plan"])
    parser.add_argument("--state", required=True)
    parser.add_argument("--proposal")
    parser.add_argument("--plan")
    parser.add_argument("--output")
    parser.add_argument("--budget", type=int, default=20)
    parser.add_argument("--round-id", default="")
    args = parser.parse_args()
    state = read(args.state)
    try:
        if args.command == "brief":
            result = brief(state, args.budget)
        elif args.command == "plan":
            require_system_stage(state)
            result = make_plan(state, read(args.proposal))
        elif args.command == "auto-plan":
            require_system_stage(state)
            result = make_plan(state, auto_proposal(state, args.budget, args.round_id))
        else:
            validate_plan(state, read(args.plan))
            result = {"valid": True}
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (PlanError, OSError, TypeError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
