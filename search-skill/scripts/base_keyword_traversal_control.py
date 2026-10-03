#!/usr/bin/env python3
"""基础关键词遍历计划控制器：只读城市状态并生成受控查询计划。"""

from __future__ import annotations

from b2b_config import check_if_bound, asset_path, profile_for, binding_for, same_binding, bind_profile, ConfigError

import argparse
import hashlib
import json
import os
import string
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import city_coverage_control as coverage


BASE_PURPOSE = "BASE_KEYWORD_TRAVERSAL"
DEFAULT_BATCH_SIZE = 10
ALLOWED_TEMPLATE_FIELDS = {"city", "keyword", "combination_term"}
VARIANT_FIELDS = ALLOWED_TEMPLATE_FIELDS | {"context"}
SEARCH_INTENTS = {"PRODUCT_SERVICE_SUPPLY", "SOLUTION_APPLICATION", "END_USER_APPLICATION",
                  "HIRING_DISCOVERY", "PROCUREMENT_DISCOVERY"}
ROLE_MARKERS = {}  # Task roles are business configuration, not industry constants.


class TraversalControlError(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise TraversalControlError(f"JSON不存在：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TraversalControlError(f"无法读取JSON {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise TraversalControlError(f"JSON顶层必须为对象：{path}")
    return check_if_bound(value)


def save_json(path: Path, value: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if temporary.exists():
            temporary.unlink()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def text(value: Any) -> str:
    return str(value or "").strip()


def template_fields(pattern: str) -> set[str]:
    fields: set[str] = set()
    try:
        for _, field_name, _, _ in string.Formatter().parse(pattern):
            if field_name:
                fields.add(field_name)
    except ValueError as exc:
        raise TraversalControlError(f"查询模板格式非法：{pattern}：{exc}") from exc
    return fields


def load_templates(authority_path: Path) -> dict[str, Any]:
    document = load_json(authority_path)
    templates = document.get("query_templates")
    if not isinstance(templates, dict):
        raise TraversalControlError("权威清单缺少 query_templates")
    if set(templates.get("allowed_placeholders", [])) != ALLOWED_TEMPLATE_FIELDS:
        raise TraversalControlError("query_templates.allowed_placeholders 非法")
    family_templates = templates.get("family_templates")
    combination_template = templates.get("combination_template")
    aliases = templates.get("coverage_family_aliases")
    if not isinstance(family_templates, dict) or not isinstance(combination_template, dict):
        raise TraversalControlError("权威清单缺少族模板或组合模板")
    if not isinstance(aliases, dict):
        raise TraversalControlError("权威清单缺少 coverage_family_aliases")
    seen_template_ids: set[str] = set()
    for item in [*family_templates.values(), combination_template]:
        if not isinstance(item, dict):
            raise TraversalControlError("查询模板项必须为对象")
        template_id = text(item.get("template_id"))
        pattern = text(item.get("pattern"))
        if not template_id or not pattern:
            raise TraversalControlError("查询模板缺少 template_id 或 pattern")
        if template_id in seen_template_ids:
            raise TraversalControlError(f"查询模板ID重复：{template_id}")
        seen_template_ids.add(template_id)
        fields = template_fields(pattern)
        if not fields <= ALLOWED_TEMPLATE_FIELDS:
            raise TraversalControlError(
                f"查询模板 {template_id} 使用未授权占位符：{sorted(fields - ALLOWED_TEMPLATE_FIELDS)}"
            )
        if not {"city", "keyword"} <= fields:
            raise TraversalControlError(f"查询模板 {template_id} 必须包含 city 和 keyword")
    variants = templates.get("purpose_variants", {})
    intents = variants.get("intents", {})
    if set(intents) != SEARCH_INTENTS or set(variants.get("allowed_placeholders", [])) != VARIANT_FIELDS:
        raise TraversalControlError("缺少完整的五类SYSTEM_SEARCH用途模板")
    for intent, config in intents.items():
        if not config.get("variants") or not config.get("custom_markers") or not config.get("allowed_discovery_paths"):
            raise TraversalControlError(f"用途 {intent} 缺少模板、标记词或发现方式")
        if not set(config.get("applicable_families", [])) <= set(family_templates):
            raise TraversalControlError(f"用途 {intent} 引用了未知关键词族")
        for variant in config["variants"]:
            vid, pattern = variant.get("variant_id"), variant.get("pattern", "")
            fields = template_fields(pattern)
            if not vid or vid in seen_template_ids or not {"city", "keyword", "combination_term"} <= fields or not fields <= VARIANT_FIELDS:
                raise TraversalControlError(f"用途模板非法或ID重复：{vid}")
            seen_template_ids.add(vid)
    return templates


def intent_catalog(templates: dict) -> dict:
    """Small decision aid, not an instruction to exhaust every keyword × intent."""
    return templates["purpose_variants"]


def render_variant(city: str, keyword: dict, templates: dict, search_intent: str,
                   history: list | None = None, variant_id: str = "",
                   combination_term: str | None = None, context: list | None = None) -> dict:
    config = templates["purpose_variants"]["intents"].get(search_intent)
    if not config or keyword["keyword_family"] not in config["applicable_families"]:
        raise TraversalControlError("检索用途不存在或不适用于该关键词族")
    if not text(city):
        raise TraversalControlError("生成用途变体必须提供城市")
    context = [] if context is None else context
    if not isinstance(context, list) or any(not isinstance(v, str) or not v.strip() for v in context):
        raise TraversalControlError("variant_context必须为非空字符串组成的列表，或空列表")
    history = [q for q in (history or []) if q.get("keyword_id") == keyword["keyword_id"]]
    variants = config["variants"]
    if variant_id:
        variant = next((v for v in variants if v["variant_id"] == variant_id), None)
        if not variant:
            raise TraversalControlError("variant_id不属于所声明检索用途")
    else:
        variant = min(variants, key=lambda v: sum(q.get("variant_id") == v["variant_id"] for q in history))
    if keyword.get("requires_combination"):
        terms = [text(v) for v in keyword.get("combination_with", []) if text(v)]
        if not terms:
            raise TraversalControlError("组合型关键词没有允许的组合词")
        if combination_term is None:
            combination_term = min(terms, key=lambda term: sum(
                term in str(q.get("query_text", "")) for q in history))
        if combination_term not in terms:
            raise TraversalControlError("变体组合词不属于权威允许集合")
    elif combination_term:
        raise TraversalControlError("非组合型关键词不能额外指定组合词；具体对象使用variant_context")
    else:
        combination_term = ""
    query = " ".join(variant["pattern"].format(city=city, keyword=keyword["keyword"],
                      combination_term=combination_term, context=" ".join(context)).split())
    return {"query_purpose": "SYSTEM_SEARCH", "search_intent": search_intent,
            "variant_mode": "CONTROLLED", "variant_id": variant["variant_id"],
            "variant_template_version": templates["purpose_variants"]["schema_version"],
            "combination_term": combination_term, "variant_context": context,
            "query_text": query}


def construct_system_query(city: str, keyword: dict, templates: dict, raw: dict, history: list) -> dict:
    """Build controlled variants or validate specifically justified project queries."""
    intent = raw.get("search_intent", "")
    config = templates["purpose_variants"]["intents"].get(intent)
    if not config or keyword["keyword_family"] not in config["applicable_families"]:
        raise TraversalControlError("SYSTEM_SEARCH必须声明适用的search_intent，不默认使用设备供应")
    if raw.get("discovery_path") not in config["allowed_discovery_paths"]:
        raise TraversalControlError("发现步骤类别与检索用途不一致")
    mode = raw.get("variant_mode", "CONTROLLED")
    if mode == "CONTROLLED":
        rendered = render_variant(city, keyword, templates, intent, history,
                                  raw.get("variant_id", ""), raw.get("combination_term"),
                                  raw.get("variant_context", []))
        if raw.get("variant_context") and not text(raw.get("construction_reason")):
            raise TraversalControlError("加入具体对象或地域限定时必须说明construction_reason")
        if raw.get("query_text") and raw["query_text"] != rendered["query_text"]:
            raise TraversalControlError("查询文本与所选用途模板不一致；具体项目查询请使用CUSTOM并说明依据")
        return {**raw, **rendered}
    if mode != "CUSTOM" or not text(raw.get("construction_reason")) or not text(raw.get("query_text")):
        raise TraversalControlError("CUSTOM查询必须提供query_text和construction_reason")
    # Keyword occurrences cannot stand in for a purpose term, e.g. a keyword
    # containing '采购' is not by itself evidence of procurement discovery.
    remainder = raw["query_text"]
    for term in keyword.get("match_terms") or [keyword["keyword"]]:
        remainder = remainder.replace(term, "")
    if not any(marker in remainder for marker in config["custom_markers"]):
        raise TraversalControlError("CUSTOM查询未体现所声明用途；不能仅以标签代替实际检索构造")
    combination = ""
    if keyword.get("requires_combination"):
        actual_terms = [t for t in keyword.get("combination_with", []) if t in raw["query_text"]]
        combination = raw.get("combination_term") or next(iter(actual_terms), "")
        if not combination or combination not in actual_terms:
            raise TraversalControlError("CUSTOM组合词必须来自权威集合且实际出现在查询中")
    elif raw.get("combination_term"):
        raise TraversalControlError("非组合型关键词不得伪填组合词")
    return {**raw, "combination_term": combination, "variant_mode": "CUSTOM", "variant_id": "CUSTOM",
            "variant_template_version": templates["purpose_variants"]["schema_version"]}


def command_variants(args: argparse.Namespace) -> None:
    state, authority, templates = load_context(Path(args.state))
    keyword = next((k for k in authority["base_keywords"] if k["keyword_id"] == args.keyword_id), None)
    if not keyword:
        raise TraversalControlError("未知keyword_id")
    intents = args.intent or [k for k, v in intent_catalog(templates)["intents"].items()
                             if keyword["keyword_family"] in v["applicable_families"]]
    result = {"keyword_id": args.keyword_id, "note": "仅提供候选变体，不登记BK完成，不要求全部执行；按缺口、来源和历史产出选用。",
              "variants": [render_variant(text(state.get("city")), keyword, templates, intent,
                            state.get("queries", []), context=args.context) for intent in intents]}
    if args.output:
        save_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def load_context(state_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state_path = state_path.resolve()
    state = load_json(state_path)
    authority = coverage.load_authoritative_base_keywords(asset_path(state, "keywords"))
    registration = coverage.base_keyword_registration_status(
        state.get("base_keywords", []), authority
    )
    if not registration["passed"]:
        raise TraversalControlError(
            "城市状态基础关键词注册不完整："
            f"缺失={registration['missing_keyword_ids']}，"
            f"多余={registration['unexpected_keyword_ids']}，"
            f"内容不符={registration['mismatched_keyword_ids']}"
        )
    authority_document = load_json(Path(authority["path"]))
    raw_by_id = {
        text(item.get("keyword_id")): item
        for item in authority_document.get("base_keywords", [])
        if isinstance(item, dict)
    }
    authority["base_keywords"] = [
        {
            **item,
            "combination_with": list(
                raw_by_id.get(item["keyword_id"], {}).get("combination_with", [])
            ),
        }
        for item in authority["base_keywords"]
    ]
    templates = load_templates(Path(authority["path"]))
    return state, authority, templates


def traversal_records(
    state: dict[str, Any], authority: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    qualifying_query_ids: dict[str, list[str]] = {}
    by_id = {item["keyword_id"]: item for item in authority["base_keywords"]}
    for query in state.get("queries", []):
        if not isinstance(query, dict):
            continue
        keyword_id = text(query.get("keyword_id"))
        keyword = by_id.get(keyword_id)
        if keyword and coverage.qualifies_for_base_traversal(query, keyword):
            qualifying_query_ids.setdefault(keyword_id, []).append(text(query["query_id"]))
    missing = [
        item for item in authority["base_keywords"]
        if item.get("direct_search", True) and item["keyword_id"] not in qualifying_query_ids
    ]
    return missing, qualifying_query_ids


def status_payload(
    state_path: Path,
    state: dict[str, Any],
    authority: dict[str, Any],
) -> dict[str, Any]:
    missing, qualifying = traversal_records(state, authority)
    required = [
        item for item in authority["base_keywords"] if item.get("direct_search", True)
    ]
    deferred = [
        item["keyword_id"] for item in authority["base_keywords"]
        if not item.get("direct_search", True)
    ]
    completed = [
        item["keyword_id"] for item in required if item["keyword_id"] in qualifying
    ]
    return {
        "schema_version": 1,
        "city": text(state.get("city") or state.get("search_space", {}).get("city")),
        "state_path": str(state_path.resolve()),
        "state_sha256": file_hash(state_path.resolve()),
        "authority_path": str(authority["path"]),
        "authority_sha256": authority["sha256"],
        "required_keyword_count": len(required),
        "completed_keyword_count": len(completed),
        "missing_keyword_count": len(missing),
        "completed_keyword_ids": completed,
        "missing_keyword_ids": [item["keyword_id"] for item in missing],
        "deferred_pending_test_keyword_ids": deferred,
        "base_keyword_traversal_complete": not missing,
    }


def choose_task(
    keyword: dict[str, Any],
    tasks: list[dict[str, Any]],
    aliases: dict[str, Any],
    used_task_ids: set[str],
) -> dict[str, Any]:
    family = keyword["keyword_family"]
    accepted_families = set(str(value) for value in aliases.get(family, [family]))
    compatible = [
        task for task in tasks
        if text(task.get("task_id")) not in used_task_ids
        and (
            not text(task.get("keyword_family"))
            or text(task.get("keyword_family")) in accepted_families
        )
    ]
    fallback = [
        task for task in tasks if text(task.get("task_id")) not in used_task_ids
    ]
    choices = compatible or fallback
    if not choices:
        raise TraversalControlError(
            "当前批次没有足够的不重复搜索任务可承载关键词；请缩小 --batch-size 或补充覆盖任务"
        )
    return choices[0]


def render_query(
    city: str,
    keyword: dict[str, Any],
    templates: dict[str, Any],
) -> tuple[str, str, str]:
    family = keyword["keyword_family"]
    family_templates = templates["family_templates"]
    if family not in family_templates:
        raise TraversalControlError(f"关键词族没有受控查询模板：{family}")
    # 宽查询不提前选择组合词；组合约束在KEYWORD_DEEPENING的用途模板中执行。
    combination_term = ""
    template = family_templates[family]
    pattern = text(template["pattern"])
    fields = template_fields(pattern)
    if "combination_term" in fields and not combination_term:
        raise TraversalControlError(
            f"模板 {template['template_id']} 需要 combination_term"
        )
    query_text = " ".join(
        pattern.format(
            city=city,
            keyword=keyword["keyword"],
            combination_term=combination_term,
        ).split()
    )
    if not coverage.base_keyword_match_term({"query_text": query_text}, keyword):
        raise TraversalControlError(
            f"生成查询未命中关键词 match_terms：{keyword['keyword_id']}"
        )
    # 首轮宽查询不强制组合词；组合约束保留在后续用途变体中。
    return query_text, text(template["template_id"]), combination_term


def next_query_id(keyword_id: str, queries: list[dict[str, Any]]) -> str:
    attempts = sum(
        1 for query in queries if text(query.get("keyword_id")) == keyword_id
    )
    return f"BKQ-{keyword_id}-{attempts + 1:02d}"


def coverage_keyword(task: dict[str, Any], authority: dict[str, Any], aliases: dict[str, Any]) -> dict[str, Any]:
    target = text(task.get("keyword_family"))
    for keyword in authority["base_keywords"]:
        family = text(keyword.get("keyword_family"))
        accepted = set(str(value) for value in aliases.get(family, [family]))
        if keyword.get("direct_search", True) and (not target or target == family or target in accepted):
            return keyword
    raise TraversalControlError(f"覆盖任务没有可用基础词：{task.get('task_id')}")


def render_coverage_query(city: str, task: dict[str, Any], keyword: dict[str, Any]) -> str:
    parts = [text(task.get("district")) or city, text(task.get("industry")), keyword["keyword"]]
    parts.append(ROLE_MARKERS.get(text(task.get("role")), text(task.get("role"))))
    return " ".join(part for part in parts if part)


def build_coverage_plan(
    state_path: Path,
    state: dict[str, Any],
    authority: dict[str, Any],
    templates: dict[str, Any],
    task_ids: list[str],
    batch_size: int,
) -> dict[str, Any]:
    calculation = coverage.calculate(state)
    pending_ids = calculation.get("next_task_ids", [])
    selected_ids = task_ids or pending_ids[:batch_size]
    unknown = set(selected_ids) - set(pending_ids)
    if unknown:
        raise TraversalControlError("覆盖计划包含非待办任务：" + ", ".join(sorted(unknown)))
    task_by_id = {text(item.get("task_id")): item for item in state.get("tasks", [])}
    aliases = templates["coverage_family_aliases"]
    search_tasks = []
    existing_ids = {text(item.get("query_id")) for item in state.get("queries", [])}
    for task_id in selected_ids:
        task = task_by_id[task_id]
        keyword = coverage_keyword(task, authority, aliases)
        attempt = 1 + sum(1 for item in state.get("queries", []) if text(item.get("task_id")) == task_id)
        query_id = f"GAPQ-{task_id}-{attempt:02d}"
        if query_id in existing_ids:
            raise TraversalControlError(f"生成的 query_id 已存在：{query_id}")
        search_tasks.append({
            "search_task_id": task_id,
            "query_id": query_id,
            "query_text": render_coverage_query(text(state.get("city")), task, keyword),
            "query_purpose": "GAP_SEARCH",
            "keyword_id": keyword["keyword_id"],
            "keyword_family": keyword["keyword_family"],
            "source_type": text(task.get("source_type")) or "公开网页",
            "district": text(task.get("district")),
            "role": text(task.get("role")),
            "industry": text(task.get("industry")),
        })
    return {
        "schema_version": 1,
        "plan_type": "COVERAGE_GAP_SEARCH",
        "city": text(state.get("city")),
        "state_path": str(state_path.resolve()),
        "input_version": file_hash(state_path.resolve()),
        "planned_task_count": len(search_tasks),
        "search_tasks": search_tasks,
        "created_at": now(),
    }


def build_plan(
    state_path: Path,
    state: dict[str, Any],
    authority: dict[str, Any],
    templates: dict[str, Any],
    batch_size: int,
) -> dict[str, Any]:
    if batch_size < 1:
        raise TraversalControlError("--batch-size 必须大于0")
    city = text(state.get("city") or state.get("search_space", {}).get("city"))
    if not city:
        raise TraversalControlError("城市状态缺少 city")
    missing, _ = traversal_records(state, authority)
    selected = missing[:batch_size]
    tasks = [
        task for task in state.get("tasks", [])
        if isinstance(task, dict) and text(task.get("task_id"))
    ]
    if selected and not tasks:
        raise TraversalControlError("城市状态没有可用搜索任务")
    aliases = templates["coverage_family_aliases"]
    used_task_ids: set[str] = set()
    search_tasks = []
    existing_query_ids = {
        text(query.get("query_id")) for query in state.get("queries", [])
        if isinstance(query, dict)
    }
    for keyword in selected:
        task = choose_task(keyword, tasks, aliases, used_task_ids)
        task_id = text(task["task_id"])
        used_task_ids.add(task_id)
        query_text, template_id, combination_term = render_query(
            city, keyword, templates
        )
        query_id = next_query_id(keyword["keyword_id"], state.get("queries", []))
        if query_id in existing_query_ids:
            raise TraversalControlError(f"生成的 query_id 已存在：{query_id}")
        search_tasks.append({
            "search_task_id": task_id,
            "query_id": query_id,
            "query_text": query_text,
            "query_purpose": BASE_PURPOSE,
            "keyword_id": keyword["keyword_id"],
            "keyword": keyword["keyword"],
            "keyword_family": keyword["keyword_family"],
            "source_type": text(task.get("source_type"))
                or text(state.get("search_space", {}).get("source_types", ["公开网页"])[0]),
            "district": text(task.get("district")),
            "role": text(task.get("role")),
            "industry": text(task.get("industry")),
            "template_id": template_id,
            "combination_term": combination_term,
        })
    return {
        "schema_version": 1,
        "plan_type": "BASE_KEYWORD_TRAVERSAL",
        "city": city,
        "state_path": str(state_path.resolve()),
        "input_version": file_hash(state_path.resolve()),
        "authority_sha256": authority["sha256"],
        "template_schema_version": text(templates.get("schema_version")),
        "requested_batch_size": batch_size,
        "planned_keyword_count": len(search_tasks),
        "assigned_keyword_ids": [item["keyword_id"] for item in search_tasks],
        "remaining_after_plan": len(missing) - len(search_tasks),
        "search_tasks": search_tasks,
        "created_at": now(),
    }


def validate_plan(
    state_path: Path,
    state: dict[str, Any],
    authority: dict[str, Any],
    templates: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    if plan.get("plan_type") != "BASE_KEYWORD_TRAVERSAL":
        errors.append("plan_type 非 BASE_KEYWORD_TRAVERSAL")
    if text(plan.get("input_version")) != file_hash(state_path.resolve()):
        errors.append("input_version 与当前城市状态不一致")
    if text(plan.get("authority_sha256")) != authority["sha256"]:
        errors.append("authority_sha256 与当前权威清单不一致")
    tasks = plan.get("search_tasks")
    if not isinstance(tasks, list):
        errors.append("search_tasks 必须为数组")
        tasks = []
    missing, _ = traversal_records(state, authority)
    missing_by_id = {item["keyword_id"]: item for item in missing}
    state_tasks = {
        text(item.get("task_id")): item for item in state.get("tasks", [])
        if isinstance(item, dict)
    }
    seen_query_ids: set[str] = set()
    seen_task_ids: set[str] = set()
    seen_keyword_ids: set[str] = set()
    existing_query_ids = {
        text(item.get("query_id")) for item in state.get("queries", [])
        if isinstance(item, dict)
    }
    for index, item in enumerate(tasks, start=1):
        prefix = f"search_tasks[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须为对象")
            continue
        query_id = text(item.get("query_id"))
        task_id = text(item.get("search_task_id"))
        keyword_id = text(item.get("keyword_id"))
        if not query_id or query_id in seen_query_ids or query_id in existing_query_ids:
            errors.append(f"{prefix} query_id 缺失、重复或已存在")
        if not task_id or task_id in seen_task_ids or task_id not in state_tasks:
            errors.append(f"{prefix} search_task_id 缺失、重复或无法反查")
        if not keyword_id or keyword_id in seen_keyword_ids or keyword_id not in missing_by_id:
            errors.append(f"{prefix} keyword_id 缺失、重复或不属于待遍历集合")
        seen_query_ids.add(query_id)
        seen_task_ids.add(task_id)
        seen_keyword_ids.add(keyword_id)
        keyword = missing_by_id.get(keyword_id)
        if not keyword:
            continue
        try:
            expected_text, expected_template, expected_combination = render_query(
                text(state.get("city") or state.get("search_space", {}).get("city")),
                keyword,
                templates,
            )
        except TraversalControlError as exc:
            errors.append(f"{prefix} 无法按权威模板重建：{exc}")
            continue
        expected = {
            "query_text": expected_text,
            "query_purpose": BASE_PURPOSE,
            "keyword": keyword["keyword"],
            "keyword_family": keyword["keyword_family"],
            "template_id": expected_template,
            "combination_term": expected_combination,
        }
        for field, value in expected.items():
            if item.get(field) != value:
                errors.append(f"{prefix}.{field} 与权威模板不一致")
        task = state_tasks.get(task_id, {})
        expected_source = text(task.get("source_type")) or text(
            state.get("search_space", {}).get("source_types", ["公开网页"])[0]
        )
        if text(item.get("source_type")) != expected_source:
            errors.append(f"{prefix}.source_type 与搜索任务不一致")
    declared_count = plan.get("planned_keyword_count")
    if declared_count != len(tasks):
        errors.append("planned_keyword_count 与 search_tasks 数量不一致")
    if errors:
        raise TraversalControlError("计划校验失败：" + "；".join(errors))
    return {
        "valid": True,
        "planned_keyword_count": len(tasks),
        "keyword_ids": [text(item.get("keyword_id")) for item in tasks],
        "query_ids": [text(item.get("query_id")) for item in tasks],
    }


def command_status(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state, authority, _ = load_context(state_path)
    print(json.dumps(
        status_payload(state_path, state, authority),
        ensure_ascii=False,
        indent=2,
    ))


def command_plan(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state, authority, templates = load_context(state_path)
    plan = build_plan(
        state_path, state, authority, templates, args.batch_size
    )
    validate_plan(state_path, state, authority, templates, plan)
    output_path = Path(args.output).resolve()
    save_json(output_path, plan)
    print(json.dumps({
        "planned": True,
        "output": str(output_path),
        "planned_keyword_count": plan["planned_keyword_count"],
        "remaining_after_plan": plan["remaining_after_plan"],
    }, ensure_ascii=False, indent=2))


def command_coverage_plan(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state, authority, templates = load_context(state_path)
    plan = build_coverage_plan(
        state_path, state, authority, templates, args.task_ids, args.batch_size
    )
    output_path = Path(args.output).resolve()
    save_json(output_path, plan)
    print(json.dumps({
        "planned": True,
        "output": str(output_path),
        "planned_task_count": plan["planned_task_count"],
    }, ensure_ascii=False, indent=2))


def command_validate_plan(args: argparse.Namespace) -> None:
    state_path = Path(args.state).resolve()
    state, authority, templates = load_context(state_path)
    plan = load_json(Path(args.plan).resolve())
    result = validate_plan(state_path, state, authority, templates, plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="基础关键词遍历受控计划生成器")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="查看基础关键词遍历缺口")
    status.add_argument("--state", required=True)
    status.set_defaults(func=command_status)

    variants = subparsers.add_parser("variants", help="为SYSTEM_SEARCH生成指定BK的用途变体建议，不改BK登记")
    variants.add_argument("--state", required=True)
    variants.add_argument("--keyword-id", required=True)
    variants.add_argument("--intent", action="append", choices=sorted(SEARCH_INTENTS))
    variants.add_argument("--context", action="append", default=[])
    variants.add_argument("--output")
    variants.set_defaults(func=command_variants)

    plan = subparsers.add_parser("plan", help="按权威模板生成下一批搜索计划")
    plan.add_argument("--state", required=True)
    plan.add_argument("--output", required=True)
    plan.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    plan.set_defaults(func=command_plan)

    coverage_plan = subparsers.add_parser("coverage-plan", help="为未完成覆盖任务生成三维GAP_SEARCH计划")
    coverage_plan.add_argument("--state", required=True)
    coverage_plan.add_argument("--output", required=True)
    coverage_plan.add_argument("--task-id", dest="task_ids", action="append", default=[])
    coverage_plan.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    coverage_plan.set_defaults(func=command_coverage_plan)

    validate = subparsers.add_parser("validate-plan", help="校验搜索计划未偏离权威模板")
    validate.add_argument("--state", required=True)
    validate.add_argument("--plan", required=True)
    validate.set_defaults(func=command_validate_plan)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.func(args)
        return 0
    except (
        TraversalControlError,
        coverage.CoverageError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(f"基础关键词遍历控制失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
