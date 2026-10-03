# 可选业务分析工作流

当前版本已移除企查查导入、等待、回填、冲突评审阶段及resume-qcc命令。安装目录保留config、scripts与本说明；运行输出保存到外部任务工作目录。

## 两条路径

- 直接交付：SEARCH_RUNNING → SEARCH_DONE → DELIVERY_READY → DONE。
- 可选分析：SEARCH_DONE → INFERENCE_RUNNING → INFERENCE_DONE → BASE_FIELDS_LOCKED → COMPETITOR_SEARCH_RUNNING → COMPETITOR_SEARCH_DONE → DELIVERY_READY → DONE。

主控state/与search-control/生成在外部任务工作目录，不写入技能安装目录。先准备并初始化检索城市状态，再运行：

```text
python scripts/workflow.py --workdir <同一检索运行目录> init --city <城市> --search-root <本包search-skill路径> --city-state <城市状态JSON>
python scripts/workflow.py --workdir <运行目录> next --caller-token <令牌>
```

主控与检索使用同一运行目录state/.caller_token。SEARCH_RUNNING的next交由检索编排器准备下一动作；正式停止时主控freeze、recalculate、独立复算，登记SEARCH_DONE与完整企业注册表。

## 阶段字段与提交

后续阶段统一由本主控领取和提交批次，不依赖包外的集中推断控制器。advance仅接受config/field_permissions.yml白名单迁移。阶段开始后用seed装载每行一个企业ID的文本文件；ID集合须等于已核验名单且不能重复装载。

```text
python scripts/workflow.py --workdir <运行目录> advance --to INFERENCE_RUNNING --caller-token <令牌>
python scripts/workflow.py --workdir <运行目录> seed --stage INFERENCE_RUNNING --ids <企业ID.txt> --caller-token <令牌>
python scripts/workflow.py --workdir <运行目录> next --caller-token <令牌>
python scripts/workflow.py --workdir <运行目录> commit --batch <领取批次ID> --result <结果JSON> --caller-token <令牌>
```

提交结果为数组或含records数组的对象；每项提供企业ID和本阶段允许字段。INFERENCE_RUNNING处理核心产品、客户类型、下游行业、下游客户；COMPETITOR_SEARCH_RUNNING处理客户的竞争对手。权限按字段名映射到本任务output_columns，不依赖列字母。

每阶段全部企业完成提交后才允许进入对应DONE。空队列或pending计数为0不能替代真实完成记录。未找到的业务信息可明确填“未知”；业务推断和竞争对手结论须有证据支持。

## 主体冲突

提交可包含内部metadata对象，如`{"冲突状态":"OPEN","主体核验状态":"PENDING"}`，不写入Excel。冲突门禁从company_metadata重算，忽略自报的conflicts=0。提交发现冲突后，可通过resolve-conflict登记处理依据；处理结果提供企业ID、主体核验状态VERIFIED、冲突状态RESOLVED、处理依据。原始决策JSON按哈希保留。

## 最终交付

SEARCH_DONE可直接advance至DELIVERY_READY。使用search-skill/scripts/final_delivery_control.py deliver生成三项产物及带文件摘要的交付清单。仅检索名单无需填写竞争对手。分析后的字段导出增加--workflow-state。

最后`advance --to DONE --evidence <交付清单.json> --caller-token <令牌>`。门禁复核冻结链、三项文件哈希、企业ID/字段/证据及覆盖表实际内容，不接受任意工作簿或空证据JSON。

本包提供受控执行与交付接口，尚不包含网页前端、API服务、客户数据库、CRM写入或计费功能。
