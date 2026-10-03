# 正式交付

1. 主控确认STOP_CANDIDATE，执行freeze生成清单，再执行recalculate生成正式快照。
2. 导出时重新校验全部冻结输入并只读复算；coverage_complete、convergence_complete、search_complete须同时为true且增量/全量无差异。`--force`标记诊断冻结，始终禁止正式交付。
3. 推荐一次生成三项文件和交付清单：

```text
python final_delivery_control.py deliver --city-state <城市状态> --manifest <冻结清单> --snapshot <正式快照> --template <主表模板> --output-workbook <名单.xlsx> --output-evidence <企业证据.json> --output-coverage <覆盖.xlsx> --output-manifest <交付清单.json>
```

模板须匹配output_columns；默认工作表为全流程、起始行为3，可用--sheet及--start-row指定。输出路径不得覆盖输入或另一交付物。仍保留export与export-coverage作为分步导出接口。

4. 检索交付只填写前三列；仅FINAL进入名单与企业证据包。空结果但覆盖及收敛完成的任务可交付带表头的空名单，不制造客户。
5. 完成可选业务分析后，可在deliver增加`--workflow-state <主控状态>`写出已登记后续字段。主控须处于DELIVERY_READY或DONE，企业集合及快照一致、主体冲突已解决。后续字段仅包括企业业务补全和竞争对手，结论须有证据支持。
6. 交付清单的artifacts每项为`{"path":"绝对路径","sha256":"文件字节摘要"}`，包括final_workbook、final_evidence_package、coverage_audit_workbook。DONE门禁核对三项文件摘要、名单逐格内容、企业证据内容及重新生成的覆盖审计。

`manifest_sha256`统一指冻结清单核心内容的规范JSON摘要；`manifest_file_sha256`指清单文件字节摘要。二者不可混用。snapshot_id绑定确定性复算内容，时间戳不参与ID生成。

原始状态、查询、发现池和异常记录保留。配置、权限、策略或执行脚本变化后应恢复原版本或新建任务；本版不自动迁移旧任务。
