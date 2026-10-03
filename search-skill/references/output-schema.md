# B2B 检索交付结构

三项产物为企业主表Excel、FINAL企业证据包JSON、城市覆盖核验Excel。原始JSON候选池及查询记录仍为权威工作记录。

## 公共主表

前三列固定为序号、企业名称、官网地址。序号使用城市任务已分配的企业ID，不在导出时重编号。官网必须已核验归属，无法确认留空。数据默认从第三行开始，第二行为模板说明行。

本版公共表头如下。后续字段虽然保留，检索阶段均不填写。

|序列|字段|检索阶段|
|---|---|---|
|1|序号|填写|
|2|企业名称|填写|
|3|官网地址|填写|
|4|具体地址|留空|
|5|成立时间|留空|
|6|注册资金/万|留空|
|7|参保人数|留空|
|8|交付的核心产品|留空|
|9|客户类型|留空|
|10|下游行业|留空|
|11|下游客户（企业交付物卖给哪些企业）|留空|
|12|客户的竞争对手|留空|
|13|CRM录入（是否）|留空|
|14|归属区域|留空|
|15|归属销售|留空|

公共主表不固定26列。任务所需的其他字段由output_columns追加；新增字段不代表检索阶段应填值。导出模板全部表头须与绑定配置完全一致，避免不同配置混用。后续工作流按字段名更新，不依赖原列字母。

## FINAL证据包

manifest_sha256为清单核心摘要，manifest_file_sha256为清单字节摘要。

顶层保留city、snapshot_id、source_city_state、source_city_state_sha256、冻结校验元数据、final_count及enterprises，并增加profile_id、profile_version、config_fingerprint。

每个企业包含company_id、company_name、official_website、business_fit、prospect_fit_evidence、发现来源数组、扩展追溯字段、evidence_ids、原始evidence_records。prospect_fit_evidence保存业务事实、客户关系、匹配规则与理由、地域来源及采购意向口径。

证据包从冻结的原始记录过滤，不补造业务结论。主表与证据包企业集合完全一致；FOLLOWUP、EXCLUDED、CARRYOVER不进入最终名单，但完整保留在工作池中。

## 覆盖核验表

沿用城市控制器的正式快照导出：区县、角色、行业、词族、来源、关键词注册、查询执行、SYSTEM_SEARCH收敛与扩展审计。统计读取正式全量重算结果，不以最终名单数量作为覆盖。
