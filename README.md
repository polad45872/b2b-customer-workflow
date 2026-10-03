# 多行业 B2B 潜客检索

供通用 Agent 加载的 Skill，依据产品服务和目标客户画像，检索中国城市的企业潜客，核验主体、地域和业务匹配，交付企业名单、来源证据和覆盖核验表。

## 功能

- 通过行业配置注入产品服务、客户角色、业务匹配规则、词库和输出字段。
- 管理发现、核验、去重、扩展、覆盖检查、冻结和交付。
- 按需补全企业核心产品、客户类型、下游行业、下游客户及竞争对手。
- 提供设备、企业软件和专业服务三个演示配置。

当前版本不包含企查查导入、经营及采购估算，也不包含网页前端、API 服务、跨任务客户数据库和 CRM 集成。

## 使用

1. 将本仓库整体放入宿主 Agent 的 Skills 目录，文件夹命名为 `b2b-customer-workflow`。
2. 使用 Python 3.12，在自己的运行环境安装依赖：`python -m pip install -r requirements.txt`。
3. 按 [配置作者说明](search-skill/examples/AUTHORING.md) 准备符合实际业务的行业配置。
4. 调用 Skill，提供目标城市、产品服务、客户画像以及需要的交付范围。

```text
使用 $b2b-customer-workflow，
我的产品或服务是……，目标客户是……，
请检索苏州市的企业潜客，交付名单、证据包和覆盖核验表。
```

宿主 Agent 需要具备网页检索、网页读取和本地脚本执行能力。脚本负责计划、记录和校验，网页事实核验及业务判断由 Agent 执行。示例配置需按实际业务调整，不能直接视作已确认的获客策略。

任务状态和运行输出应保存在 Skill 目录外的独立工作目录；配置及执行版本在任务中绑定，修改后需要另建任务。

## 文件入口

- [Skill 主入口](SKILL.md)
- [检索执行流程](search-skill/search-skill.md)
- [行业配置规范](search-skill/references/profile-schema.md)
- [企业入选与核验规则](search-skill/references/qualification-rules.md)
- [交付结构](search-skill/references/output-schema.md)
- [可选业务补全与竞争对手流程](single-agent-workflow/README.md)

默认主表为 15 列，检索阶段填写序号、企业名称和已核验官网，业务补全阶段按字段权限写入后续信息。FINAL 表示符合本次目标客户条件，不表示已确认采购预算或采购意向。覆盖矩阵完成并且增量收敛是停止条件，不承诺绝对穷尽。
