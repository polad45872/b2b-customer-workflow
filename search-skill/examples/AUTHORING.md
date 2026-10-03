# 行业配置作者说明

复用现有equipment、software、services目录作为行业模板库，保持文件层级。每个示例含profile.json、keywords.json，共同引用assets/expansion-feature-chains.json；复制到你自己的外部配置目录后再修改，避免运行中修改已绑定资产。

1. 填写我方产品或服务、客户角色、目标行业、实际业务匹配及排除规则。
2. 修改词库，保持keyword_family与族模板、覆盖别名、深化策略和用途适配对应。
3. 修改扩展特征链；来源必须属于profile.target.source_types，模板只使用city与所声明链维度。
4. 需要改变第4列起的业务表头时，同步修改任务所指向的字段权限文件；内部metadata不加到Excel。
5. profile.assets中的相对路径从profile目录解析。权限和策略可指向本包默认文件或外部独立副本。选择policy为standard、strict或light；低于核心下限的策略整包会被拒绝。
6. 执行`python scripts/config_lint.py --profile <配置>`校验；执行`python scripts/b2b_profile_control.py preview --profile <配置> --city <城市>`预览画像、策略、权限与基础查询，不创建或改动状态。
7. 确认后使用prepare生成城市空间与中性Excel模板，并在外部运行目录启动新任务。

三个行业示例是结构模板；不构成完备行业策略或真实客户数据。
