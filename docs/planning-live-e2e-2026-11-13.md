# 测评规划真机录屏验证（2026-11-13）

> 后续进展：本记录中的失败与缺口已修复，完整闭环（含报告投影、进度展示与入榜）于同日重新验收通过，见 [planning-live-acceptance-2026-11-13.md](./planning-live-acceptance-2026-11-13.md)。本文件保留为当次失败记录。

结论：未通过完整闭环。使用本机浏览器访问真实前后端，后端经固定指纹 SSH 调用 179.214.2.3 的独立 GPQA 容器；无 API mock、无历史结果代替本次输出。

任务：145aef26-0e4b-4d34-b71d-8108389368b7，子任务：978b9264-01c3-4ac3-bc04-553310844548。
范围：GPQA Lite 5 样本，现有固定配置、串行、直接提交，未主动吞吐探测，未采纳估算方案。

1. 浏览器真实选择目标/SSH/执行配置/清单，截图 01-planning.png。
2. 真实资源预检通过，截图 02-preflight.png。
3. 浏览器确认范围与调用费用后单次提交；任务 accepted → running → failed。初始缺少 workflow 与 planning-execution Worker，已启动，沿用原任务，没有重复提交。
4. 子任务实际耗时约 80.97 秒，工具退出码 1；统一输出 observed_cases=0、missing_cases=5、scored_cases=0，准确率未知，不填零。Token usage 缺失，无法据此确认上游请求次数或费用。
5. planned-report.v1 失败报告已由接口返回；原始结果与报告归档路径存在于正常执行链路。本次未另行抽样核验 S3 对象哈希。
6. 页面详情仅展示任务/数据集状态及实际耗时，尚无 case 级实时进度；标准报告列表不承接规划任务报告。
7. 入榜未完成：本次无有效成绩；且现有数据工作台仅接受 EvaluationReport、local-operator:evaluation 的已验证完整报告，不接受 orchestration/planned-report.v1。没有通过手工来源绕过报告校验。

录屏：.screenshots/live-e2e-20261113/live-validation.webm（1440×1000，约15MB）。截图 01–07 包含规划、预检、任务详情、任务列表、报告列表、榜单。录屏展示实际失败过程，不是成功演示。完整 API 结果：同目录 run-result.json。

未更改业务实现；未重新提交失败任务。需另行修复工具执行失败、规划报告投影、进度明细与入榜契约后再验证完整成功闭环。
