# B2B 潜客准入与核验

## 核验顺序

先使用发现来源确认企业主体、城市实际经营地、全局去重、业务活动和客户匹配。名称、官网、集团成员或合作关系有歧义时补查；不能把集团一处经营地址自动赋给所有子公司，也不能将有独立经营主体的企业全部合并。

业务事实、客户匹配推断、采购意向分别记录。注册经营范围、搜索摘要、招聘职位和种子关系是线索，单独不足以确认实际业务。弱证据可以使用，但必须支持明确业务事实，含主体匹配、可访问URL、访问时间与来源摘录。证据强度继续为strong、medium_strong、weak。

## FINAL条件

1. `business_fit=IN_SCOPE`，客户角色属于本次配置。
2. `prospect_fit_evidence.identity_verified=true`，企业主体确认且完成全局去重。
3. `location_verified=true`，至少一条可访问、主体匹配的地域来源支持实际经营地；正式提交时来源摘录包含本次目标城市。
4. `business_activity`非空，描述来源支持的具体业务；`application_scene`可在未知时留空。
5. `match_basis`非空并标明`match_basis_type=FACT|INFERENCE`，说明与我方产品服务的关联。
6. `matched_rule_ids`关联本次配置中的匹配规则，并满足角色与来源线索条件。
7. 至少一条企业业务证据具备HTTP(S)网址、访问时间、可访问状态、主体匹配、`supports_business_activity=true`和原文摘录。
8. 完整保留发现词、查询、查询ID、任务ID、词族及成组来源追溯。官网无法确认可以留空，不能用第三方页面充作官网。

上述程序检查能证明记录一致及来源字段完整；主控负责真实主体、地域、原文含义及商业关联判断。

## 业务匹配与采购意向

`business_fit`为IN_SCOPE、OUT_OF_SCOPE、UNKNOWN。未知业务不能FINAL；明确业务不匹配须EXCLUDED。即使业务未知，也可因主体或地域明确不符合而EXCLUDED，记录原因。

`purchase_intent`默认为UNKNOWN。PUBLIC_SIGNAL表示有采购或项目公开信号；CONFIRMED只用于来源明确证明的意向。后二者必须提供单独`purchase_evidence_refs`，其来源支持采购信号；不能从业务匹配、角色或招聘岗位推定采购意向。年度采购金额与预算不是搜索阶段必填字段。

## FOLLOWUP与EXCLUDED

FOLLOWUP原因：IDENTITY_PENDING、LOCATION_PENDING、BUSINESS_FIT_PENDING、QUALIFYING_EVIDENCE_MISSING、SOURCE_BLOCKED。保留已取得事实及缺口。自动定向重查沿用身份、地域歧义规则，其余缺口由主控决定路径。

EXCLUDED须写明主体、地域、配置匹配或重复关系依据。是否排除同行、供应商和渠道由本次客户目标决定，不存在统一的“技术同类就排除”规则。

探索种子允许可追溯的FINAL、FOLLOWUP、UNVERIFIED候选，但其资格与采购意向不传递给新企业。扩展企业仍满足上述独立准入。
