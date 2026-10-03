"""One admission rule used by worksets, batches, city ingestion and delivery."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

BUSINESS_FITS = {"IN_SCOPE", "OUT_OF_SCOPE", "UNKNOWN"}
FOLLOWUP_REASONS = {"IDENTITY_PENDING", "LOCATION_PENDING", "BUSINESS_FIT_PENDING",
                    "QUALIFYING_EVIDENCE_MISSING", "SOURCE_BLOCKED"}


class QualificationError(ValueError):
    pass


def text(value):
    return str(value or "").strip()


def accessible_ref(ref):
    if not isinstance(ref, dict):
        return False
    parsed = urlparse(text(ref.get("ref")))
    try:
        datetime.fromisoformat(text(ref.get("accessed_at")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (parsed.scheme in {"http", "https"} and bool(parsed.netloc)
            and ref.get("entity_match") is True
            and text(ref.get("access_status")).upper() == "ACCESSIBLE"
            and ref.get("strength") in {"strong", "medium_strong", "weak"}
            and bool(text(ref.get("source_excerpt"))))


def validate_qualification(status, business_fit, scene, followup_reason="", profile=None, city=""):
    if status not in {"FINAL", "FOLLOWUP", "EXCLUDED"}:
        raise QualificationError("非法核验状态")
    if business_fit not in BUSINESS_FITS:
        raise QualificationError("business_fit必须为IN_SCOPE、OUT_OF_SCOPE或UNKNOWN")
    if not isinstance(scene, dict) or not isinstance(scene.get("evidence_refs"), list):
        raise QualificationError("prospect_fit_evidence及evidence_refs必须提供")
    if status == "FOLLOWUP" and followup_reason not in FOLLOWUP_REASONS:
        raise QualificationError("FOLLOWUP必须提供明确的待补查原因")
    if business_fit == "OUT_OF_SCOPE" and status != "EXCLUDED":
        raise QualificationError("业务明确不匹配时须EXCLUDED")
    if business_fit == "UNKNOWN" and status == "FINAL":
        raise QualificationError("业务匹配未知不能FINAL")
    # Identity or geography can exclude a company even while its business fit is unknown.
    if status != "FINAL":
        return
    if business_fit != "IN_SCOPE":
        raise QualificationError("FINAL必须确认业务符合目标画像")
    if scene.get("identity_verified") is not True or scene.get("location_verified") is not True:
        raise QualificationError("FINAL必须确认企业主体与实际经营地域")
    for key in ("business_activity", "customer_role", "match_basis"):
        if not text(scene.get(key)):
            raise QualificationError(f"FINAL缺少{key}")
    if scene.get("match_basis_type") not in {"FACT", "INFERENCE"}:
        raise QualificationError("匹配理由必须标明FACT或INFERENCE")
    refs = [r for r in scene["evidence_refs"] if accessible_ref(r)
            and r.get("supports_business_activity") is True]
    if not refs:
        raise QualificationError("FINAL至少需要一条可访问、主体匹配并支持业务事实的来源及摘录")
    location_refs = scene.get("location_evidence_refs", [])
    if not isinstance(location_refs, list) or not any(accessible_ref(r) and r.get("supports_location") is True
                                                     and (not city or city in text(r.get("source_excerpt"))) for r in location_refs):
        raise QualificationError("FINAL缺少支持目标地域实际经营地的来源")
    demand = scene.get("purchase_intent", "UNKNOWN")
    if demand not in {"UNKNOWN", "PUBLIC_SIGNAL", "CONFIRMED"}:
        raise QualificationError("purchase_intent必须为UNKNOWN、PUBLIC_SIGNAL或CONFIRMED")
    if demand != "UNKNOWN" and not any(accessible_ref(r) and r.get("supports_purchase_intent") is True
                                      for r in scene.get("purchase_evidence_refs", [])):
        raise QualificationError("采购信号或意向确认须有独立来源，不能由客户匹配推定")
    rules = scene.get("matched_rule_ids")
    if not isinstance(rules, list) or not rules or len(rules) != len(set(rules)):
        raise QualificationError("FINAL必须关联非空且不重复的匹配规则ID")
    if profile is None:
        return  # Preliminary envelope check; formal commit always passes the bound profile.
    if scene.get("customer_role") not in profile["target"]["roles"]:
        raise QualificationError("客户角色不属于本次任务目标")
    catalog = {r["rule_id"]: r for r in profile["fit_rules"]}
    excerpts = [text(r["source_excerpt"]).casefold() for r in refs]
    for rid in rules:
        if rid not in catalog:
            raise QualificationError(f"匹配规则不属于本次配置：{rid}")
        rule = catalog[rid]
        if scene["customer_role"] not in rule["allowed_roles"]:
            raise QualificationError(f"客户角色不符合匹配规则：{rid}")
        # The cited factual passage, rather than an inferred match rationale, must contain the configured clue.
        if not any(any(term.casefold() in excerpt for term in rule["evidence_any"])
                   and all(term.casefold() in excerpt for term in rule.get("evidence_all", []))
                   for excerpt in excerpts):
            raise QualificationError(f"来源摘录未支持匹配规则：{rid}")
    for rule in profile.get("exclude_rules", []):
        if any(term.casefold() in excerpt for term in rule["evidence_any"] for excerpt in excerpts):
            raise QualificationError(f"来源触发排除规则：{rule['rule_id']}；须由主控核对语义并处置")



def seed_eligible(candidate):
    return (str(candidate.get('status', '')).upper() in {'FINAL', 'FOLLOWUP', 'UNVERIFIED'}
            and bool(candidate.get('discovered_by_query_ids')))
