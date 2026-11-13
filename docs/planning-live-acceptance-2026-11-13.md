# 测评规划到入榜真机闭环验收（2026-11-13）

## 结论

通过。使用本机浏览器、真实前后端、真实 SSH 执行资源与真实模型调用，完成“测评规划 → 资源预检 → 提交 → 任务详情进度 → 报告与逐 case 证据 → 提名评审 → 批准入榜 → 榜单展示”全链路，并录屏留存。无 API mock，无历史结果代替本次输出。

## 范围与冻结条件

- 模型服务 `9e3e54a2-b7d8-46d4-94f4-4171d4758167`（`deepseek-flash`），执行资源 `51278e81-b71b-4425-b89f-fd90ce287402`（客户 SSH，`179.214.2.3`）。
- 数据集 GPQA，Lite 5 样本清单 `c43e1352-b3dd-46ad-9876-b714d9d86489`（全量 198 样本），执行配置 `a9d82b1b-d350-4a47-8c20-0011fc96acdd`。
- 本次运行级输出上限 2048 token/样本，数据集并发 1、串行执行、每样本 1 次尝试，最多 5 次模型调用；未做吞吐探测，未采纳耗时优化方案，失败不自动重测。

## 本次新增能力：运行级输出上限收窄

标准仓输入把 `params.max_out_len` 锁定为 262144，编译页面不能下调。本次新增规划参数 `output_token_limit`（`POST /planning/resources/preflight`、`/planning/plans`、`/planning/runs` 共用 `PlanInput`）：

- 只允许向下收窄，超过编译预算返回 422；不修改标准输入、编译快照与历史任务。
- 收窄后的值写入该次任务自身的 `locked_fields`、预检快照身份与冻结运行快照，因此锁定校验、预检失效判断与报告入榜条件一致。
- 前端“执行策略”新增“本次输出上限”，并进入任务摘要与预检指纹；改变该值会使旧预检和旧规划失效。
- 该参数仅适用于 `llm_accuracy`；`harbor_agent` 明确拒绝。

## 实测链路

1. 控制台选择模型服务、客户 SSH 资源，启用 GPQA，选择执行配置与 Lite 清单，填写输出上限 2048，截图 `01-planning.png`。
2. 资源预检通过：`execution_ready=true`，`supported_datasets=[GPQA]`，Planner `0ee9abf4…`、Adaptor `955089ab…`、AISBench `525ed177…` 与预期一致，冻结 `max_out_len=2048`，截图 `02-preflight.png`。
3. 确认范围与费用后单次提交，任务 `a8c0ca2f-0f55-4d6f-a8fd-d61c61d74916`，`accepted → running → completed`；详情页展示阶段、已归档样本、实际耗时与报告入口，截图 `03-detail-start.png`、`04-detail-final.png`。
4. 任务列表可回到该编排任务，截图 `05-task-list.png`。
5. 报告 `365d58a9-4249-4935-9b46-138eaead672b`：`validity=complete`、`evidence_status=verified`、`execution_kind=planned`，准确率 80%（4/5），5 次尝试全部成功，输入/输出 token 1103/5051，`nomination_conditions.output=2048`；报告页与样本明细截图 `06-report.png`、`07-case-scores.png`。
6. 数据工作台“报告评审入榜”提名该报告，再填写依据并批准入榜；后台在提名与批准时两次复核报告完整性、来源与冻结条件，截图 `08-nominated.png`、`09-approved.png`。
7. 已评审榜单包含该记录，`#leaderboard` 正常展示，截图 `10-leaderboard.png`。

条目 ID `69dd03aa-a6ae-4fa7-ac01-48435689c9fd`；机器可读结果见同目录 `acceptance.json`、`run-result.json`、`report.json`、`steps.json`。

## 验证命令

后端在 `backend/` 执行：

```powershell
uv run ruff check src/autoeval/planning.py tests/test_planning.py
uv run pytest -q tests/test_planning.py                 # 21 passed, 1 skipped
uv run pytest -q tests/test_data_workbench.py           # 10 passed, 1 skipped
$env:AE_INFRA_TEST='1'
uv run pytest -q tests/test_planning_live.py            # 8 passed
```

前端在 `frontend/` 执行 `npm run typecheck` 与 `npm run build`，均通过。

## 未覆盖边界

- 本次仅 GPQA Lite 5 样本探索成绩，不代表 198 样本全量能力，也不构成正式发布批准；MMMU-Pro、DeepSWE 未在本次范围内。
- 规划详情仍以平台观测的归档进度为准，未提供 case 级实时比例；不以估算代替真实进度。
- 服务器部署、迁移与线上运行属于独立步骤，见部署记录；本记录不代表生产环境验收。

## 产物

录屏与截图位于仓库根目录 `.screenshots/live-e2e-acceptance-20261113/`（本地验收产物，不提交）：`live-acceptance.webm`（1440×1000）、`01`–`10` 截图、`acceptance.json`、`record.py`。
