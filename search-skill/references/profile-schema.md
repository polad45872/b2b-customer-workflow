# B2B 行业配置

配置为JSON；相对资产路径从配置所在目录解析。查看 `examples/*/profile.json` 与对应 `keywords.json` 的完整可运行示例。

|字段|含义|
|---|---|
|schema_version、profile_id、version|配置结构、业务标识与版本|
|offering.name、description、problem_solved|我方产品服务、用途与解决的问题|
|target.roles|本次允许的客户关系角色，必须具体限定|
|target.industries|本次目标企业行业；保留行业这个B2B维度|
|target.source_types|覆盖来源类型|
|fit_rules|客户匹配规则，含rule_id、description、relevance_to_offering、allowed_roles、evidence_any，可加evidence_all|
|exclude_rules|可选的明确排除规则，含rule_id、description、evidence_any|
|assets.keywords|关键词及查询模板文件|
|assets.expansion|扩展特征链文件|
|assets.permissions|字段权限文件；默认使用现有workflow-control/config/field_permissions.yml|
|assets.policy、policy|策略文件及standard / strict / light档位；默认assets/policies.json与standard|
|output_columns|完整有序表头，前三项固定为序号、企业名称、官网地址|

`fit_rules`不能只有“是企业”或“属于某行业”。业务条件须有能在企业自身来源摘录中核对的线索，匹配理由说明它为何与我方产品服务相关。脚本检查摘录是否含配置线索；主控仍须判断上下文是否描述实际业务，不能用否定句、免责声明或搜索关键词堆砌满足规则。

`evidence_any`配置同义表达；至少一个词在同一条合格来源摘录中出现。若配置`evidence_all`，这些词也须同时出现。关键词只用于发现；核验线索是另一套条件。排除线索触发时须核对来源语义，不能继续写入FINAL。

角色按商业关系配置。直接买方、渠道或合作伙伴可以在不同任务中使用；角色符合不自动证明采购意向。员工规模、营收或其他门槛本版不提供专门计算接口，若有明确业务要求须在规则说明和主控核验中处理，不能猜测未知数值。

词库结构继续使用`base_keywords`、`total_keywords`、词族与`query_templates`。标准用途为PRODUCT_SERVICE_SUPPLY、SOLUTION_APPLICATION、END_USER_APPLICATION、HIRING_DISCOVERY、PROCUREMENT_DISCOVERY。这五类用于生成发现方式；无须对每个词穷举全部用途。覆盖使用规范词族ID，避免隐藏的行业别名字典。

初始化绑定配置、词库、扩展链、字段权限、策略文件的路径及SHA-256，同时记录全部执行脚本摘要与execution_version。fingerprint覆盖整包；批次继承绑定，冻结清单纳入资产及执行脚本。跨文件校验检查词族、用途、占位符、来源、业务输出列及字段权限。内部主体/冲突元数据不要求加入Excel。后续读取检测配置漂移，禁止混合行业结果。

实际初始化必须显式指定profile及其词库。三个行业示例不是完备行业策略，应用前应调整真实产品、业务匹配条件、词库、排除规则和地域。

新版本更改了准入字段与输出模板，旧状态需另建任务；本版没有自动迁移旧行业记录的功能。已有文件不能通过批量改字段名变成已核验的新行业潜客。
