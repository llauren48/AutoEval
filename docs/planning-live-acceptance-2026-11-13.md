# 测评规划到入榜真机闭环验收（2026-11-13）

## 结论

通过。使用本机浏览器、真实前后端、真实客户 SSH 执行资源与真实模型调用，完成“环境检查 → 提交测评 → 任务管理/任务详情进度 → 报告与逐 case 证据 → 提名评审 → 批准入榜 → 榜单展示”全链路，并录屏留存。无 API mock，无历史结果代替本次输出。

## 范围与冻结条件

- 模型服务 `9e3e54a2-b7d8-46d4-94f4-4171d4758167`（`deepseek-flash`），执行资源 `51278e81-b71b-4425-b89f-fd90ce287402`（客户 SSH，`179.214.2.3`）。
- 数据集 GPQA，Lite 5 样本清单 `c43e1352-b3dd-46ad-9876-b714d9d86489`（全量 198 样本），执行配置 `a9d82b1b-d350-4a47-8c20-0011fc96acdd`。
- 数据集并发 1、串行执行、每样本 1 次尝试，最多 5 次模型调用；未做吞吐探测，未采纳耗时优化方案，失败不自动重测。
- 输出预算沿用标准仓锁定值 `max_out_len=262144`，平台不在提交链路下调或重置该字段。

## 环境检查与测评的边界

- 预检只做环境检查：工具提交哈希、数据目录、CPU/内存/磁盘、Planner/Adaptor 版本；不创建 `EvaluationRun`、不调用模型。回归用例 `test_preflight_job_does_not_create_evaluation_run` 锁定这一点。
- 只有“提交测评（当前配置）”按钮会创建任务并进入任务详情；提交后跳转 `#run/<id>`，不再停留在编排视图。
- 规划提交的测评现在与普通测评一致地出现在“任务管理 → 测评任务”列表，详情可查看执行进度、事件、Case 历史与报告入口。回归用例 `test_planned_run_is_visible_in_task_management_detail` 覆盖列表可见性、样本数与清理视图。

## 实测链路

1. 控制台选择模型服务、客户 SSH 资源，启用 GPQA，选择执行配置与 Lite 清单，截图 `01-planning.png`。
2. 环境检查通过：`execution_ready=true`，`supported_datasets=[GPQA]`，Planner `0ee9abf4…`、Adaptor `955089ab…`、AISBench `525ed177…` 与预期一致；此时没有测评任务记录，截图 `02-environment-check.png`。
3. 确认范围与费用后点击“提交测评（当前配置）”，任务 `9d48fed0-1cb3-4207-8572-c029c78bd697` 创建并进入任务详情，截图 `03-task-detail-start.png`。
4. 任务详情展示执行状态、事件与进度，`accepted → running → completed`，截图 `04-task-progress.png`、`05-task-detail-final.png`；返回“任务管理 → 测评任务”列表可见该任务，截图 `06-task-list.png`。
5. 报告 `aea6bde4-691a-430d-847b-dd4afcaf8ba4`：`validity=complete`、`evidence_status=verified`、`execution_kind=planned`，准确率 100%（5/5），输入/输出 token 1103/17033；报告 SHA-256 `4adfe22e…b81b`，快照 SHA-256 `66b65309…b2e2e`。报告页与样本明细截图 `07-report.png`、`08-case-scores.png`。
6. 数据工作台“报告评审入榜”提名该报告，填写依据后批准入榜；后台在提名与批准时两次复核报告完整性、来源与冻结条件，截图 `09-nominated.png`、`10-approved.png`。
7. 已评审榜单包含该记录，`#leaderboard` 正常展示，截图 `11-leaderboard.png`。

条目 ID `ecfe586c-a7a0-4342-a5d6-1e31fc95d228`；入榜条件 `scope=lite`、`output=262144`、`concurrency=1`、`temperature=1.0`、`top_p=0.95`、清单 SHA-256 `d976b438…7153`、工具 `525ed177…`。机器可读结果见同目录 `acceptance.json`、`run-result.json`、`report.json`、`steps.json`。

## 验证命令

后端在 `backend/` 执行：

```powershell
uv run ruff check src/autoeval/evaluations.py src/autoeval/planning.py tests/test_planning.py tests/test_planning_live.py
uv run pytest -q tests/test_planning.py tests/test_data_workbench.py
$env:AE_INFRA_TEST='1'
uv run pytest -q tests/test_planning_live.py -k "task_management_detail or preflight_job_does_not"
```

前端在 `frontend/` 执行 `npm run typecheck` 与 `npm run build`，均通过。跳过项为需显式基础设施开关的联调专项，不计为通过。

## 未覆盖边界

- 本次仅 GPQA Lite 5 样本探索成绩，不代表 198 样本全量能力，也不构成正式发布批准；MMMU-Pro、DeepSWE 不在本次范围。
- 任务详情展示平台观测的运行状态与事件；工具在单次运行结束时统一归档 case，因此执行中不提供 case 级实时比例，也不以估算代替真实进度。
- 服务器部署、迁移与线上运行属于独立步骤，见内网部署记录；本记录不代表生产环境验收。

## 产物

录屏与截图位于仓库根目录 `.screenshots/live-e2e-taskdetail-20261113/`（本地验收产物，不提交）：`live-acceptance-taskdetail.webm`（1440×1000）、`01`–`11` 截图、`acceptance.json`、`run-result.json`、`report.json`、`steps.json`、`record.py`。
