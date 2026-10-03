# 城市 B2B 潜客检索执行流程

## 初始化

1. 选择或填写行业配置，明确我方产品服务、客户角色、业务场景和纳入规则。只给出城市且没有可用配置时，先补齐这一信息，禁止全行业无目标检索。
2. 使用 `b2b_profile_control.py validate --profile <配置>` 校验画像、词库、扩展链、权限及策略整包；可先运行 `preview --profile <配置> --city <城市>` 只读预览。三个示例在 `examples/equipment`、`examples/software`、`examples/services`。
3. 使用 `b2b_profile_control.py prepare --profile <配置> --city <城市> --district <区县> --output-space <空间JSON> --output-template <主表xlsx>` 准备空间和模板；有多个行政区时重复传入 `--district`，不能把示例区县当作真实地域覆盖。
4. 主控使用 `state_guard.py init-token --workdir <运行目录>` 初始化令牌；后续写命令提供 `--caller-token`。令牌不进入Worker包。
5. 使用 `city_coverage_control.py init --state <城市状态> --space <空间JSON> --profile <配置> --caller-token <令牌>`。初始化注册当前配置全部词条，不再有固定词条数量。

## 发现与核验

优先 `workflow_orchestrator.py prepare-next --city-state <城市状态> --output-dir <计划目录> --caller-token <令牌>`。它依据已有企业线索、历史产出与覆盖缺口生成下一计划；`search_stage_runner.py next-search-action` 可检查下一动作。

SYSTEM_SEARCH计划优先沿具体企业产品服务、业务场景、项目、来源名单和高产路径推进。基础词库是锚点和审计工具，不要求先把词与全部用途穷举完再发现企业。覆盖缺口随低增量情况补搜。预算与阈值来自绑定的策略档。standard 为每轮至少8条有效查询、3个独立零新增轮次、建议预算10；strict 为12条、4轮、预算16；light 为8条、3轮、预算8。所有档位要求覆盖矩阵完成率100%。

主控用 `batch_context_control.py init` 建立批次，必须传入同配置的 `--city-state`，指定 `--batch-id`、`--mode`、`--round-id`、`--strategy-id`、`--query-budget`。首次SYSTEM_SEARCH可使用计划中对应的轮次与预算。

`search_worker_control.py create-discovery` 将查询分组，`export`导出只读任务包。Worker按分配查询实际浏览，返回全部查询及企业来源线索。每个查询保留实际访问网址、检查数量和终止原因；零结果和访问失败也返回记录。不得自己改变查询ID、分配查询或写入正式状态。

需要委派时遵循当前会话授权。单查询、单扩展计划、平台不支持或Worker不可用时，使用脚本支持的fallback_reason记录事实；没有委派权限的环境采用单Worker并记录平台不可用的原因说明，不绕过结果校验。

主控通过 `validate-result`、`merge` 校验合并发现结果。使用 `batch_context_control.py register-discovery` 登记，再逐条入池或处置：精确主体重复可自动解决，别名和集团关系由主控核对。所有发现线索均保留处置记录。

用 `search_stage_runner.py next` 开启工作集，主控依 [核验规则](references/qualification-rules.md) 一次性提交 `commit-workset`。身份、地域、业务事实和匹配依据明确时不重复补查；未知采购意向不妨碍合格潜客入选。身份或地域有歧义时允许定向重查；业务匹配和来源缺口按缺口类型处理，不能借补查伪造采购需求。

## 扩展和批次推进

可追溯的FINAL、FOLLOWUP、UNVERIFIED入池企业可作为探索种子。用 `expansion_control.py plan` 从本任务配置的特征链生成扩展查询，继承种子与查询追溯信息。扩展所得企业独立核验。

沿用 `workflow_orchestrator.py batch-next`、`search_stage_runner.py close` 与底层批次控制；未开始核验的候选显式结转，活动工作集不得直接关闭。关闭时汇入城市累计记录并重新计算覆盖，按返回动作继续。单批上限、工作集上限、交付分组及扩展停止阈值均从绑定策略档读取。

## 停止和交付

以城市累计底层查询、任务、候选和扩展记录检查停止。名单数量不作目标，覆盖不能只统计FINAL企业。来源受限不能当作完成搜索。

达到STOP_CANDIDATE后依次执行 `city_coverage_control.py freeze` 与 `recalculate`。全量重算的覆盖、收敛和search_complete均通过且无差异后，再通过 `final_delivery_control.py deliver` 同时传入城市状态、冻结清单和正式快照，生成三项产物及交付清单。导出器会独立只读复算，不接受快照内自报的完成标志。

交付三项：B2B主表Excel、仅FINAL的企业证据包JSON、覆盖核验Excel。检索阶段主表只写前三列；其余字段留后续。原始候选池、查询日志和异常保留。中途按候选ID与计数说明进度，最终产物提供企业名称。

批次、状态、词库及配置一经绑定不得混用；运行中修改配置会明确阻断。另建任务才能切换行业。脚本不会主动访问网站，实际获客检索需要执行环境提供浏览能力。
