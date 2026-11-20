# AutoEval Platform

<p align="center">
  <img src="./frontend/public/brand/autoeval-logo.svg" alt="AutoEval 智能测评平台" width="300" />
</p>

AutoEval 是面向单团队、单工作空间的模型测评平台。通过统一入口提交测评，跟踪执行状态，归档样本证据与报告，并从 Harbor 样本记录跳转查看 Langfuse Agent 轨迹。

代码仓库：[llauren48/AutoEval-Platform](https://github.com/llauren48/AutoEval-Platform)。仓库名称为 `AutoEval-Platform`，页面与产品名称统一为 `AutoEval`。

技术栈：React / TypeScript / Vite、FastAPI、Temporal、PostgreSQL、S3 兼容对象存储。当前用于受控开发与演示，正式身份、生产运维和 Release 门禁尚未完成验收。

[快速启动](#快速启动) · [当前能力](#当前能力与验证范围) · [服务器访问](#langfuse-与服务器访问) · [开发与检查](#开发与检查) · [文档导航](#文档导航)

## 当前能力与验证范围

更新日期：2026-11-17。功能实现、固定产物联调与真实模型测评分开记录。

| 能力 | 当前状态 | 说明 |
| --- | --- | --- |
| 统一测评任务 | 已接入 | 参数与清单冻结、环境预检、顺序 / 并发调度、取消、状态恢复；估算与参数建议为可选操作 |
| GPQA / GPQA-Diamond | 已有真实 Lite 验收 | 提交、归档、Case、报告、分析及评审入榜；精简清单成绩不代表全量成绩 |
| AutomationBench → Langfuse | 轨迹查看已获用户验收确认 | 单样本原生执行、自动导入、工具层级、平台跳转及 full 正文展示通过；原任务失败历史保留，修复后的正式报告链路未重跑验收 |
| 报告与榜单 | 已接入 | 证据校验、可比条件、评审与撤回；执行成功、评分与证据完整性分别展示 |
| 标准输入与适配器 | 已接入 | 标准仓版本、锁定参数、配置编译和统一输出；登记数据集不等于执行环境就绪 |
| MMMU-Pro、DeepSWE、多数据集真实闭环 | 待分别验收 | 不以模拟或固定产物验证代替真实执行 |
| 性能测评、极光资源、生产发布 | 尚未完成验收 | 不以总耗时或 Token 数替代吞吐量、延迟及性能成绩 |

本次具体结果、标识与限制见 [GPQA 20 题验收](docs/live-e2e-gpqa20-2026-11-16.md)和 [AutomationBench 真机验收](docs/automationbench-langfuse-acceptance-2026-11-17.md)。真实模型请求需明确目标、样本、尝试次数与预算；结果未知时不自动重发。

### 基线与报告

导入标准输入时，`baseline.json` 随输入版本同步并保留来源、提交和校验值。全量报告按标准库定义展示基线和百分点差值，Lite 报告标注样本范围。具体阈值、比较口径和验收范围见 [标准输入与基线规则](docs/standard-inputs.md)。

一次测评任务生成一份报告，各数据集分别展示成绩、基线和证据。恢复归档采用追加修订，保留原始样本记录和任务状态。详见 [恢复归档与多数据集报告](docs/task-report-recovery.md)。

## 快速启动

### 1. 准备环境

- Node.js 22+、Python 3.12、uv、Docker / Docker Compose。
- 可用的 PostgreSQL、Temporal 与持久化 S3；执行工具需要另行准备并登记 Docker / SSH 资源。
- 首次从 `backend/.env.example` 建立 `backend/.env` 并配置真实连接；已有配置不要覆盖，凭据不提交。

持久化对象存储的准备、备份及恢复见[恢复指南](docs/development-recovery.md)。开发用 `deploy/compose.yaml` 包含 Moto 协议模拟器，不用于保存真实测评证据；持久化存储配置为 [compose.storage-durable.yaml](deploy/compose.storage-durable.yaml)。

### 2. 启动 API

在仓库根目录打开终端：

```powershell
cd backend
uv sync --frozen --python 3.12
uv run alembic upgrade head
uv run python -m autoeval.infrastructure.bootstrap storage
uv run uvicorn autoeval.entrypoints.api:app --host 127.0.0.1 --port 8000
```

API 文档：`http://127.0.0.1:8000/docs`。`/health/live` 检查进程存活，`/health/ready` 检查数据库、Temporal 和对象存储。

### 3. 启动后台进程

仅启动 API 不会执行测评。下列常驻命令分别在 `backend/` 的独立终端运行：

| 进程 | 命令 |
| --- | --- |
| 持久化任务分发 | `uv run python -m autoeval.entrypoints.dispatcher` |
| 测评工作流 | `uv run python -m autoeval.entrypoints.worker workflow` |
| 预检与规划协调 | `uv run python -m autoeval.planning.planning_worker` |
| 测评执行活动 | `uv run python -m autoeval.entrypoints.worker planning-execution` |
| 状态对账 | `uv run python -m autoeval.entrypoints.reconciler` |
| Harbor 轨迹导入（启用 Langfuse 时） | `uv run python -m autoeval.entrypoints.worker langfuse` |

结果收集、独立分析和旧兼容路径所需角色见[后端说明](backend/README.md)。默认规划协调仅处理 `preflight,plan`；吞吐探测与独立分析需单独配置并明确授权，不能为补齐页面数据自动触发。

### 4. 启动前端

在仓库根目录另开终端：

```powershell
cd frontend
npm ci
npm run dev
```

访问 `http://127.0.0.1:5173`，Vite 将 API 请求代理到本机 8000 端口。进入控制台选择模型服务、执行资源、配置与样本清单，预检通过并确认范围后提交。

## Langfuse 与服务器访问

Langfuse 采用指定 fork [llauren48/harbor-otel-stack](https://github.com/llauren48/harbor-otel-stack)，固定提交及依赖锁随代码维护。独立部署数据库和持久卷，不替换 demo 的业务基础设施。

- demo：`https://179.214.2.3/`
- Langfuse：`https://179.214.2.3:8443/`
- 首次访问配置：Windows 普通用户 PowerShell 运行 `powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\langfuse\setup-client.ps1`，配置当前用户证书信任及代理例外，无需 hosts。

仍需连接可访问服务器的 VPN。项目 API keys 仅保存在后端；普通私有 Trace 首次需要 Langfuse 登录，登录后返回原记录。当前仅一条指定的 structural 演示 Trace 开启免登录分享，其他轨迹和后续导入不自动公开。

新归档默认使用 `full` 展示正文，历史记录保留其冻结模式。该样本的私有正文版与公开摘要版使用不同 Trace ID；平台按钮当前指向正文版，首次需登录 Langfuse。

配置、worker、公开分享及撤销步骤见 [Langfuse 接入说明](docs/langfuse-integration.md)；真实任务失败历史、评分修复和验收边界见[真机验收记录](docs/automationbench-langfuse-acceptance-2026-11-17.md)。

## 开发与检查

目标为 `main` 的 PR 会运行两项 GitHub Actions 检查。后端运行 Ruff，在临时 PostgreSQL 上执行 Alembic 迁移，再运行默认 pytest。前端检查模块边界、TypeScript 类型和 Vite 构建。合并前需通过两项检查、同步最新的 `main` 并解决评审对话。

```powershell
# backend/
uv run pytest -q
uv run ruff check .

# frontend/
npm run typecheck
npm run build
```

默认测试不触发真实模型调用；被条件跳过的 PostgreSQL、S3、SSH 或浏览器专项不算验收通过。页面交互检查见[浏览器回归说明](backend/tests/browser_checks/README.md)。开发约定见 [AGENTS.md](AGENTS.md)。

## 目录

```text
frontend/src/app/        页面入口与路由
frontend/src/features/   按业务划分的页面与组件
frontend/src/shared/     API、状态词汇与共享组件
backend/src/autoeval/    API、领域服务、工作流与执行适配
backend/migrations/      Alembic 数据库迁移
backend/tests/           单元、基础设施与浏览器检查
deploy/                  容器、远程部署与客户端配置
docs/                    接入、设计、验收与恢复说明
deliverables/            已核对的截图、录屏与验收摘要
```

本地草稿、凭据、备份与调试产物按 `.gitignore` 保留在工作区。`deliverables/` 用于存放已核对的交付材料。

## 文档导航

| 主题 | 文档 |
| --- | --- |
| 后端配置与进程 | [后端 README](backend/README.md)、[架构重构清单](docs/refactor-inventory.md) |
| 统一测评与规划 | [统一任务](docs/unified-evaluation-tasks.md)、[任务规划](docs/task-planning.md)、[吞吐探测验收](docs/probe-live-acceptance-2026-11-16.md) |
| 数据集与工具 | [标准输入](docs/standard-inputs.md)、[统一输出](docs/unified-output-integration.md)、[AISBench](docs/aisbench-integration.md)、[AutomationBench](docs/automationbench-integration.md) |
| 报告与榜单 | [数据工作台](docs/data-workbench.md)、[最优成绩选择](docs/leaderboard-best-selection.md)、[原生进度与输入输出](docs/progress-native-fix-2026-11-16.md) |
| Langfuse | [配置与访问](docs/langfuse-integration.md)、[服务器部署历史](docs/langfuse-server-validation-2026-11-16.md)、[真实轨迹验收](docs/automationbench-langfuse-acceptance-2026-11-17.md) |
| 部署与恢复 | [持久化与恢复](docs/development-recovery.md)、[内网发布操作](deploy/intranet/README.md) |
| 验收与展示 | [GPQA 20 题验收及录屏](docs/live-e2e-gpqa20-2026-11-16.md)、[AutomationBench 平台截图](deliverables/automationbench-langfuse-platform.png)、[免登录轨迹截图](deliverables/automationbench-langfuse-public-trace.png) |

早期版本限制、按日期追加的开发记录和页面预览已移至[整理前 README 历史快照](docs/readme-history-2026-11-17.md)。历史快照只用于追溯，不能代替当前运行说明及分项验收。
