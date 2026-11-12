# 任务耗时与执行编排

本次新增独立的规划和通用编排路径，保留原 GPQA Lite 接口及历史快照。控制台优先展示规划入口，可切换“GPQA Lite 快速验证”入口。AutomationBench 仍未开放。

## 启动与依赖

平台依赖固定 Planner `0ee9abf49a57c5accc62df8900693cdcea88ea9a`，见后端 pyproject.toml 和 uv.lock。不要以修改兄弟仓库代替更新平台锁文件。

先按恢复指南备份、核对数据库和持久化 S3，再执行：

```powershell
cd backend
uv sync --frozen
uv run alembic upgrade head
uv run python -m autoeval.planning_worker
# 另开终端，启动通用执行活动；还需原有 workflow worker 与 dispatcher。
uv run python -m autoeval.worker planning-execution
uv run python -m autoeval.worker workflow
uv run python -m autoeval.dispatcher
```

迁移为 `0027_task_planning`，接续 `0026_run_monitor`。先升级数据库和 worker，再重启 API。现有 .env 不需要覆盖。

每个执行环境（注册的 Docker 容器或 SSH 主机）需要安装：

```sh
python -m pip install 'git+https://github.com/SJTUyh/AutoEval-Task-Planner@0ee9abf49a57c5accc62df8900693cdcea88ea9a'
python -m pip install 'git+https://github.com/SJTUyh/AutoEval-Benchmark-Adaptor@955089ab32ef696c231d628f5ad0d9da28f430c4'
```

执行端通过发行包的 direct_url.json 核对提交号，不能用同名任意版本替代。还需 Git、对应 AISBench/Harbor 依赖及真实数据；Profile 固定工具提交、评分文件路径和校验和。

执行资源必须完成各数据集真实小样本验收后，由环境配置声明 `AE_VALIDATED_DATASETS=GPQA,MMMU-Pro,DeepSWE`；默认没有已验收数据集，不开放真实执行。该声明不能代替验收记录。Harbor 另需 `AE_HARBOR_NETWORK_CAPACITY` 指定配置的 Docker 地址池容量；缺失时不开放 DeepSWE。`AE_MAX_PROBE_CONCURRENCY` 默认 8，上限由资源预检返回。

预检尊重 Linux cgroup CPU、内存上限，记录可用磁盘、节点启动身份和可用网段，并按所选目标服务、编译配置、Profile 和样本清单绑定快照。返回 Planner/Adaptor 实际与预期提交、各数据集工具提交、数据路径及分项阻塞原因；工具提交、评分文件、数据目录或 Harbor 网段不满足时，相应数据集不可执行。新资源预检会产生新快照，输入或资源变化后旧预检不能用于采纳；采纳和提交要求 5 分钟内的成功预检。

当前通用传输在所选容器/SSH 主机内本地运行 Adaptor；编译器的 execution/container_name 视为外层资源选择元数据，不再启动嵌套容器。复杂 mounts、case_images 等运行参数明确拒绝，不静默忽略。Harbor 自身的 trial 环境由 Harbor 管理。

## 页面流程

1. 保存模型服务和执行资源；在标准输入管理中编译固定版本配置，选择正确目标服务、工具根目录和数据路径。
2. 登记全量/Lite 清单与 Profile。每个编译配置只允许一个数据集；一个父任务最多包含三个不同数据集。
3. 选择配置及清单，执行资源预检，选择已有吞吐曲线或启动探测。
4. 计算当前配置耗时，或搜索更优顺序及并发。检查各数据集的单独耗时、计划内耗时和代表性时间线后采纳；采纳后使用后端返回的执行方案报告刷新预计耗时。
5. 确认范围及真实调用费用后提交。修改目标、资源、配置、范围或调度参数会清除旧计划与采纳状态。

探测默认档位 1、2、4、8，每档至少 max(档位, 2) 个请求，输入及最大输出均为 512 token。预算包含一次整档非流式回退，默认最多 32 次请求、16,384 个输出 token。禁用自动 HTTP 重试，拒绝重定向，保持 TLS 校验。探测在实际执行环境发起，密钥仅通过运行时受保护文件注入并在结束/取消时删除。失败仅保存白名单原因码，区分超时、协议不兼容、鉴权失败、限流、连接失败和取消，不持久化上游异常文本。

标准锁重新校验：推荐并发与锁冲突时采纳失败，用户需选择合法配置。平台控制数据集并发，每个隔离子任务中 max_num_workers=1，工具控制 batch_size 或 n_concurrent_trials。

## 接口

前缀 `/api/autoeval/v1/planning`，沿用 `{data, request_id}` 响应包装。

| 方法与路径 | 用途 |
| --- | --- |
| GET /options | 编译版本、已绑定清单、吞吐曲线 |
| POST /resources/preflight | 按 PlanInput 异步检查所选配置的资源、工具版本和数据路径 |
| POST /probes | 显式真实吞吐探测，可传 Idempotency-Key 避免重复启动 |
| GET /probes/{id} | 查询预检、探测或规划作业 |
| POST /probes/{id}/cancel | 请求取消后台作业 |
| POST /throughput-profiles | 导入曲线，绑定目标与执行资源，丢弃请求头/请求体元数据 |
| POST /plans、GET /plans/{id} | 异步规划及查询 |
| POST /plans/{id}/apply | 传 input_sha256，生成不可变 execution_plan_id 并返回刷新后的报告，不启动执行 |
| POST /runs?execution_plan_id=… | 提交已采纳方案；Idempotency-Key 必填 |
| POST /runs | 按当前配置手工提交，无曲线时也允许，但仍须通过执行预检 |
| GET /runs、GET /runs/{id} | 分页运行列表、子任务实际耗时及归档报告 |
| POST /runs/{id}/cancel | 取消父任务和全部未结束子任务 |

PlanInput：target_id、resource_id、resource_kind（docker_local/customer_ssh）、可选 throughput_profile_id、tasks（task_id/compilation_id/manifest_id/dataset）、dataset_concurrency、recommend、runs、seed。模拟默认 100 次、吞吐抖动 0.10、种子 20261111。数据集范围及 attempts 来自编译配置与冻结清单。

## 证据、恢复与口径

- Planner SQLite 每次从真实归档 case 重建为独占临时工作文件，作业结束即删除；它不是持久化历史库，也不新增数据库服务或数据库表。权威历史仍位于 PostgreSQL 与现有对象存储，上游 run/case/attempt 键实现幂等。模拟结果不进入真实历史。缺失 token 不补零；DeepSWE 缺少非 LLM overhead 时降低置信度。
- 连续 accuracy 范围可映射到工具 dataset_test_range；不连续 Lite 明确拒绝。Harbor 按 task_names/exclude_task_names/n_tasks 选择，再按 n_attempts 估算。
- 作业输入、规划、原始统一输出、归一化证据和最终报告归档到 S3；数据库保存运行索引及查询投影。原始工具数据集/case 标识在归档原文中保留，消费时显式映射到冻结清单。
- 总预计耗时为 p50，并展示 p10–p90。时间线是最接近 p50 的代表性仿真，终点单列；不能累加数据集时长代替总时长。
- 实际耗时是平台观测到的子任务执行起止，包含提交、轮询与结果回收开销；父任务总耗时使用最早子任务开始至最晚结束。未知值保留空。
- 节点共享容量租约预留 20%，并校验 CPU/内存/磁盘/网段；远程提交意图先持久化。提交响应不确定时只查证，不重新提交，容量租约保留。
- 子任务失败后继续其他独立任务，保留部分结果，父任务最终失败；不会转换为 Release 批准。新版任务有独立详情和报告投影，不写入旧 GPQA 报告/榜单。
- 每个子任务登记 MonitorBinding 身份。新路径目前未自动轮询工具 HTTP monitor；显示平台作业状态与实际时长，不以监控状态推断评分。独立的现有监控路径保持原状。

## 本次验证与尚未覆盖

本轮默认后端全量 `uv run pytest -q` 为 **240 passed、101 skipped**；跳过项需要显式基础设施或浏览器条件，不计为通过。规划、预检和本地人工 HTTP 探测专项为 **18 passed、1 skipped**，覆盖样本范围、别名、attempts、预检选择身份、工具版本与数据路径、固定参数估算、标准锁、未知 token，以及 SSE、非流式回退、usage 缺失、鉴权、限流、超时和协议不兼容，无真实模型调用。

设置 `AE_INFRA_TEST=1` 后，真实 PostgreSQL 和持久化 S3 配合**人工模拟输出**的编排专项为 **8 passed**，覆盖三数据集串行/并行、部分失败、容量租约、取消、恢复不重发及运行中探测取消。设置 `AE_PLANNING_DOCKER_TEST=1` 后，真实 Docker 无模型预检 **1 passed**。当前容器内固定 Planner/Adaptor 的实际提交均为空，`execution_ready=false`，阻塞原因为 `planner_revision_mismatch`、`adaptor_revision_mismatch`、`no_supported_datasets`。

浏览器使用本机 Chrome 和全部拦截的人工 API，规划页面专项 **1 passed**，验证预检版本/路径展示、采纳后报告刷新及输入修改后失效；截图不代表真实测评。通过现有 Playwright Node 运行时直接执行 TypeScript `--noEmit` 与 Vite 生产构建均通过。本轮涉及文件 Ruff 通过；全仓 Ruff 另有 5 个本轮未修改的监控与自定义参数文件问题，因此不记为全仓 Ruff 通过。

尚未验收：三个数据集实际远程执行及评分、真实模型吞吐、SSH 通用 Adaptor 路径、Harbor 取消后的外部容器回收，以及工具 HTTP monitor 自动轮询。真实执行入口保持未就绪。补齐这些条件后再逐个声明已验证数据集，不以本轮测试结果宣称全量真实端到端已完成。

## 真实环境验收记录（2026-11-12，179.214.2.3）

授权范围：SSH 主机 `179.214.2.3`（`chenluwen`），DeepSeek 官方目标 `deepseek-flash`（模型版本 `v4.1-flash-1017`），本轮仅小样本；DeepSWE 按用户选择跳过，不计为通过。

已登记与固定：

- 模型/目标：模型资产 `e17e2a63-b2b3-4e1d-b24e-f7f7b95c4fb1`，目标 `9e3e54a2-b7d8-46d4-94f4-4171d4758167`。
- 执行资源：SSH 绑定 `51278e81-b71b-4425-b89f-fd90ce287402`，容器 `autoeval-planning`，AISBench `698ff2d1e027c07df37bdd158b62aace2a41ae3b`，Planner `0ee9abf49a57c5accc62df8900693cdcea88ea9a`，Adaptor `955089ab32ef696c231d628f5ad0d9da28f430c4`。
- GPQA 全量清单 `cc6d02ae-78f3-42dc-88a0-927550484707`（198 样本，CSV SHA-256 `41d1213c…fcd305`）；Profile `ed06387b-ed90-470a-8b40-1bc22a6650fc`（评分文件 `ais_bench/benchmark/datasets/gpqa.py`，SHA-256 `ae9d787f…a5bc75`）；最终编译 `14eb1260-e996-461b-b02e-3a38a42cf504`（`platform.v2`，`dump_eval_details=true`），`input_ready=true`。
- MMMU-Pro 全量清单 `07a16ded-d022-4f94-a416-f5c27bbf78e5`（1730 样本，OpenCompass/VLMEval `MMMU_Pro_10c.tsv`，SHA-256 `360e0f93…c907f`，140919931 字节）；Profile `87732c58-dfc0-457f-b08b-b275feddc7e8`（`ais_bench/benchmark/datasets/mmmu_pro.py`，SHA-256 `d8e9ab2d…3992f6`）；编译 `cba8cfa8-c8a2-4af5-86cd-4333d020935e`，`input_ready=true`。数据放在挂载目录并以符号链接暴露到 `/benchmark/ais_bench/datasets/mmmu_pro`。

通过项：

- 资源预检（作业 `13c290a8-07f9-49da-8192-92f2f97189e6`）：`execution_ready=true`，`supported_datasets=[GPQA, MMMU-Pro]`，两项工具版本与评分文件校验通过；节点 72 核、可用内存约 727 GiB、磁盘约 3.2 TiB。
- 真实吞吐探测（作业 `03447e35-db99-4b38-981c-e169e458bace`，曲线 `66ba9de7-8b86-4798-b92c-f318f62d1566`）：并发 1/2，8 次请求，错误率 0；TTFT 0.80/0.73 s，decode 208.8/342.7 tok/s，峰值 prefill 638.6 tok/s。仅覆盖该次 512 输入/128 输出预算，不外推其他上下文长度。
- **GPQA 真实小样本通过**：运行 `b55ededb-3156-4499-876e-9c82210fee7c`（执行方案 `24272099-4014-4993-8cc8-bd2203955306`）状态 `completed`，子任务 `succeeded`，`validity=complete`、1/1 样本已评分、`upstream_total_score=1.0`，输入/输出 token 164/601，子任务实际耗时 131.3 s，结果已归档。显式 `dump_eval_details=true` 使 AISBench 逐样本 `correct` 可被 Adaptor 映射为 case score，是 `validity=complete` 的必要条件。
- 规划、采纳、提交、轮询、终态归档链路在真实 SSH 资源上贯通；失败子任务不会中断其他子任务，父任务按子任务终态汇总。

阻塞项（未通过）：

- **MMMU-Pro 仍被阻塞**：子任务 `660dce18-5257-4190-9fba-309beddaa44c` 在 warmup 阶段收到 HTTP 400，AISBench 报 `TINFER-RUNTIME-001`，`case_count=0`。原因：标准输入锁定 `model_type=vllm_api_general_chat`，该模型配置把 MMMU 本地图片路径作为 `image_url` 直接发给服务；DeepSeek 官方 API 只接受可访问的 http(s) 或 data URL，本地路径无效。平台不能覆盖标准锁定的模型类型，因此该数据集在本固定组合下不可执行。对比：平台原生 GPQA Lite 路径已有“本地图片转内联 data URL”的桥接，Adaptor 路径没有。已提上游 issue：https://github.com/SJTUyh/AutoEval-Standard/issues/1 ，文档 PR：https://github.com/SJTUyh/AutoEval-Standard/pull/2 。
- **耗时估算偏差**：GPQA 实测 131.3 s，采纳方案估计 p50 仅 3.8 s。历史 case 只记录推理段 `duration_sec≈4.3 s`，未包含 AISBench 启动、数据加载、评估与结果收集约 127 s 的非 LLM 开销；当前估算模型对 accuracy 路径没有工具开销项，需在后续版本引入子任务实际开销或工具基线。该偏差不改变“估算与实测分开呈现”的口径，但说明当前估计不可用于容量承诺。

本轮应用的容器内兼容补丁（用户批准的最小改动，未改评分与提示）：

- `ais_bench/benchmark/cli/config_manager.py`：在 `_dump_and_reload_config()` 落盘前剔除 `types.ModuleType` 类型的配置键。原因：Adaptor `955089ab…` 生成的入口配置同时使用 `with read_base()` 与顶层 `import copy`，mmengine 惰性导入把 `copy` 当作配置变量，落盘为 `copy=<module 'copy' …>` 后重载报 `TMAN-CFG-001`。原文件 SHA-256 `c0ceec7a…2718ce`，补丁后 `6e996609…957f`，备份 `~/.autoeval/…` 外的 `/home/chenluwen/autoeval-data/patches/config_manager.py.orig-698ff2d1`（备份目录在挂载盘）。上游 issue：https://github.com/SJTUyh/AutoEval-Benchmark-Adaptor/issues/1 ，修复 PR：https://github.com/SJTUyh/AutoEval-Benchmark-Adaptor/pull/2 。
- `/benchmark` 目录对执行用户（uid 1008）开放写权限，用于 AISBench 运行期创建/清理 `tmp/`；`/benchmark/ais_bench/datasets/mmmu_pro` 指向挂载数据目录。上述均为环境状态，容器重建后需重放；工具 Git HEAD 与评分文件未改动。

本轮同时修复的平台缺陷（均有实际复现与回归）：

- `job_runtime.py` 使用 `Path.open(opener=…)`，SSH 受控作业提交必现 `TypeError`；改为 `os.open` + `os.fdopen` 保持 `O_EXCL` 与 `0600`，补充 Linux 条件回归用例（Windows 默认跳过）。
- `planning_engine.calculate()` 调用 `profile_from_rows` 少传 `source_runs`，一旦选中样本存在真实历史就必现 `TypeError`；已按历史观测补齐来源运行并新增回归用例。修复前含历史样本的规划作业会直接失败。
- 容器包装解释器补齐 `AE_JOB_ROOT` 透传、按宿主机 uid 1008 运行、容器内用户与 `git safe.directory`；`AE_VALIDATED_DATASETS=GPQA,MMMU-Pro` 为运维声明，不作为验收结论。

未覆盖：MMMU-Pro 真实评分与归档、DeepSWE（本轮跳过）、Harbor 取消回收、工具 HTTP monitor 自动轮询；MMMU-Pro 需要在 Adaptor 增加本地图片内联转换或改用支持多模态的固定模型配置后重测。
