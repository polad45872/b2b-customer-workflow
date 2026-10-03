# B2B 同类企业扩展

种子是城市池内可追溯的FINAL、FOLLOWUP或UNVERIFIED候选。来源不足或待核验的种子可用于探索，不能把自身资格传给新企业。

绑定配置决定特征链。本版公共链为“产品服务—应用场景—目标角色”“业务活动—业务流程—目标角色”“交付物—下游客户—目标角色”。这些是检索特征，不是对企业未知事实的自动填充。事实与探索推断分开描述。

`expansion_control.py plan`生成查询，并校验seed_id、seed_name、seed_role、feature_chain_id、feature_values、similarity_dimensions、coverage_gap、source_route、round_id、expansion_task_id、search_task_id、query_id和input_version。城市或配置变化后旧计划须重建。

Worker返回扩展计划字段与similarity_basis；主控对企业独立完成主体、地域、去重和业务匹配。种子可以是供方或合作方线索，但新候选的商业关系按本次目标角色重新判断。

扩展停止阈值来自绑定策略：standard/light为3家不同主体连续无新增或至少6条查询零新增；strict为4家或至少8条。未处置记录、来源阻断及候选缺口不能充数，后续出现新增时重新评估。扩展完成不能替代城市覆盖与独立SYSTEM_SEARCH收敛条件。
