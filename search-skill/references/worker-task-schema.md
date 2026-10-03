# 发现任务包与主控核验协议

发现任务清单绑定正式批次路径及input_sha256。导出包包含task_type、task_package_id、research_task_id、formal_batch_id、input_version、分配的search_tasks以及b2b_brief。brief提供我方产品服务、目标角色、匹配规则与配置摘要；不含调用方令牌。

发现返回的信封沿用task_package_id、research_task_id、formal_batch_id及input_version。queries必须逐一覆盖分配查询，candidates可为空。企业线索不能返回正式状态、ID或采购结论。完整合法字段由`search_worker_control.py`中的白名单检查。

查询返回：query_id、query_text、results_examined、unique_candidates、duplicate_candidates、visited_source_urls、termination_reason。候选至少返回company_name、source_url、discovery_query_id、discovery_query、matched_keywords、preliminary_role、discovery_reason、lead_summary。

## 主控整组核验

`search_stage_runner.py next`返回当前工作集及版本。提交文件顶层严格为batch_id、input_version、candidate_ids、results，candidate_ids须保持分配顺序。每个结果含：

```json
{
  "candidate_id": "C001",
  "status": "FINAL",
  "business_fit": "IN_SCOPE",
  "note": "主体、地域与业务符合本次目标；采购意向未知",
  "prospect_fit_evidence": {
    "identity_verified": true,
    "location_verified": true,
    "business_activity": "来源支持的企业具体业务",
    "application_scene": "有来源时填写",
    "customer_role": "直接买方",
    "matched_rule_ids": ["FIT-BUSINESS"],
    "match_basis": "结合已核实业务说明与我方产品的关联",
    "match_basis_type": "INFERENCE",
    "purchase_intent": "UNKNOWN",
    "evidence_refs": [{
      "ref": "https://example.com/business",
      "strength": "medium_strong",
      "supports_business_activity": true,
      "source_excerpt": "须替换为真实来源的业务原文摘录",
      "entity_match": true,
      "access_status": "ACCESSIBLE",
      "accessed_at": "2026-09-27T08:00:00Z"
    }],
    "location_evidence_refs": [{
      "ref": "https://example.com/contact",
      "strength": "medium_strong",
      "supports_location": true,
      "source_excerpt": "须替换为包含目标城市实际经营地的原文摘录",
      "entity_match": true,
      "access_status": "ACCESSIBLE",
      "accessed_at": "2026-09-27T08:00:00Z"
    }]
  },
  "official_website": "",
  "website_verification_status": "NOT_FOUND",
  "website_evidence_ref": "",
  "website_entity_match_note": "",
  "followup_reason": ""
}
```

示例网址和摘录只是协议说明，不是实际证据；不能复制它们提交真实企业。规则ID、角色、业务与地域必须对应绑定配置和本次城市。FOLLOWUP和EXCLUDED同样提供prospect_fit_evidence对象及evidence_refs数组，允许留空；FOLLOWUP另有明确原因。

官网VERIFIED时提供归属明确的官网URL、核验来源URL和主体匹配说明；NOT_FOUND或CONFLICT时官网字段为空。

正常使用commit-workset整组提交。单项complete只接受绑定batch_id与input_version的核验文件，禁止从几个命令行碎片构造不完整的FINAL证据。

扩展发现必须原样继承受控计划的种子、轮次、特征链、特征值、查询和来源路由，并提供similarity_basis。核验不得用相似性替代企业自身证据。
