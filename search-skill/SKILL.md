---
name: city-b2b-prospect-search
description: 按明确的产品服务和客户画像检索一个中国城市的企业潜客，核验主体、地域及业务匹配，并导出企业名单、证据包和覆盖核验表。
---

# 城市 B2B 潜客检索

使用显式行业配置寻找企业客户；不处理产品目录、项目库或个人线索。配置必须说明我方产品服务、目标客户角色、具体业务匹配线索、词库和输出表头。城市不能替代客户画像。

依照 [执行流程](search-skill.md) 初始化任务、组织发现、主控核验、同类扩展和最终交付。

按需读取：

- 配置和版本绑定：[profile-schema.md](references/profile-schema.md)。
- 客户匹配和FINAL条件：[qualification-rules.md](references/qualification-rules.md)。
- 查询与来源：[search-rules.md](references/search-rules.md)、[system-search.md](references/system-search.md)。
- Worker包和主控核验返回：[worker-task-schema.md](references/worker-task-schema.md)。
- 扩展与覆盖停止：[expansion.md](references/expansion.md)、[coverage-rules.md](references/coverage-rules.md)。
- Excel和证据交付：[output-schema.md](references/output-schema.md)、[export-rules.md](references/export-rules.md)、[quality-gates.md](references/quality-gates.md)。

FINAL表示符合本次目标客户条件，不代表已确认采购需求。必须分别标记来源支持的业务事实、匹配推断和采购信号。候选的种子关系、关键词命中、行业标签或招聘信息都不能单独替代企业自身业务证据。

只有同一配置、同一正式冻结快照下的覆盖与收敛检查通过，才能正式交付。网页访问与事实判断由执行Agent完成；脚本负责计划、记录、权限与一致性检查，不自行浏览网页或联系企业。
