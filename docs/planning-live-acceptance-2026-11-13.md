# 测评规划到入榜真机闭环验收（2026-11-13）

## 结论

通过。使用本机浏览器、真实前后端、真实客户 SSH 执行资源与真实模型调用，完成“测评规划 → 资源预检 → 提交 → 任务详情进度 → 报告与逐 case 证据 → 提名评审 → 批准入榜 → 榜单展示”全链路，并录屏留存。无 API mock，无历史结果代替本次输出。

## 范围与冻结条件

- 模型服务 `9e3e54a2-b7d8-46d4-94f4-4171d4758167`（`deepseek-flash`），执行资源 `51278e81-b71b-4425-b89f-fd90ce287402`（客户 SSH，`179.214.2.3`）。
- 数据集 GPQA，Lite 5 样本清单 `c43e1352-b3dd-46ad-9876-b714d9d86489`（全量 198 样本），执行配置 `a9d82b1b-d350-4a47-8c20-0011fc96acdd`。
- 数据集并发 1、串行执行、每样本 1 次尝试，最多 5 次模型调用；未做吞吐探测，未采纳耗时优化方案，失败不自动重测。
- 输出预算沿用标准仓锁定值 `max_out_len=262144`。本次未引入运行级输出上限改写：标准锁定仍是唯一权威，平台不在提交链路下调或重置该字段，报告条件如实记录实际配置值。

## 实测链路

1. 控制台选择模型服务、客户 SSH 资源，启用 GPQA，选择执行配置与 Lite 清单，截图 `01-planning.png`。
2. 资源预检通过：`execution_ready=true`，`supported_datasets=[GPQA]`，Planner `0ee9abf4…`、Adaptor `955089ab…`、AISBench `525ed177…` 与预期一致，截图 `02-preflight.png`。
3. 确认范围与费用后单次提交，任务 `1b66d8f8-5152-4768-828f-e78f29e84449`，子任务 `431b29cd-d576-45fc-8458-d20dd48ee1a5`；状态 `accepted → submitting → running → completed`，实测总耗时 216.7 秒。
4. 详情页展示执行阶段、已归档样本与报告入口，截图 `03-detail-start.png`、`03b-progress.png`、`04-detail-final.png`；任务列表可回到该编排任务，截图 `05-task-list.png`。
5. 报告 `8377c0c6-1498-436f-8463-fd9421485046`：`validity=complete`、`evidence_status=verified`、`execution_kind=planned`，准确率 100%（5/5），5 次尝试全部成功，输入/输出 token 1103/13986；报告 SHA-256 `233ded93…5e63`，快照 SHA-256 `59b7d81e…2755`。报告页与样本明细截图 `06-report.png`、`07-case-scores.png`。
6. 数据工作台“报告评审入榜”提名该报告，填写依据后批准入榜；后台在提名与批准时两次复核报告完整性、来源与冻结条件，截图 `08-nominated.png`、`09-approved.png`。
7. 已评审榜单包含该记录，`#leaderboard` 正常展示，截图 `10-leaderboard.png`。

条目 ID `1dc0ac4c-5d6c-42e5-b7bc-bbd5e940b02b`；入榜条件 `scope=lite`、`output=262144`、`concurrency=1`、`temperature=1.0`、`top_p=0.95`、清单 SHA-256 `d976b438…7153`、工具 `525ed177…`。机器可读结果见同目录 `acceptance.json`、`run-result.json`、`report.json`、`steps.json`。

## 验证命令

后端在 `backend/` 执行：

```powershell
uv run ruff check src/autoeval/planning.py src/autoeval/orchestration.py src/autoeval/reports.py src/autoeval/data_workbench.py tests/test_planning.py
uv run pytest -q tests/test_planning.py tests/test_data_workbench.py   # 29 passed, 2 skipped
```

前端在 `frontend/` 执行 `npm run typecheck` 与 `npm run build`，均通过。跳过项为需显式基础设施开关的联调专项，不计为通过。

## 未覆盖边界

- 本次仅 GPQA Lite 5 样本探索成绩，不代表 198 样本全量能力，也不构成正式发布批准；MMMU-Pro、DeepSWE 不在本次范围。
- 规划详情以平台观测的归档进度为准，未提供 case 级实时比例；不以估算代替真实进度。
- 服务器部署、迁移与线上运行属于独立步骤，见内网部署记录；本记录不代表生产环境验收。

## 产物

录屏与截图位于仓库根目录 `.screenshots/live-e2e-standard-20261113/`（本地验收产物，不提交）：`live-validation.webm`（1440×1000）、`01`–`10` 截图、`acceptance.json`、`run-result.json`、`report.json`、`steps.json`、`record.py`。
