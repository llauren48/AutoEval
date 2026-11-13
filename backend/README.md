# AutoEval 后端开发与联调

最新验证（2026-11-12）：`AE_INFRA_TEST=1` 后端269项通过、83项跳过；Ruff、TypeScript及构建通过。GPQA Lite监控路径已完成真实5样本验证，2048-token配置准确率80%；历史基线不替代当前能力及环境验收。

[GPQA Lite 2048-token复测](../docs/gpqa-output2048-validation-2026-11-12.md)：远程AISBench路径支持显式2048输出预算，同一5样本清单实测80%（4/5），5次调用；旧任务仍按原冻结预算执行，其他兼容入口仍限64。

[监控真实任务及 Case 原文展示修复](../docs/monitor-case-evidence-fix-2026-11-12.md)：已从归档原生预测文件恢复新 CLI 路径的模型输入/响应，保留原证据哈希和历史成绩；5样本真实任务、监控、报告、评审入榜及录屏标识见记录。

新增[任务耗时与完整编排接入](../docs/task-planning.md)：固定 Planner、主动探测、耗时明细、不可变执行计划和三数据集调度；需迁移至 `0027_task_planning` 并启动新增 worker。已通过离线、人工输出基础设施与浏览器验证，真实三数据集执行尚待环境验收；下文 GPQA Lite 限制描述旧兼容路径。

AISBench 运行监控先执行迁移 `0026_run_monitor`，再将 `AE_AISBENCH_MONITOR_CONFIG` 指向包含固定 SSH 指纹、容器身份和独立部署根目录的受保护 JSON（不提交该文件）。部署必须匹配 `tool-locks/aisbench-monitor.json` 与 `tool-patches/`：runtime 文件名为 `runtime-<当前 monitored_runtime.py 摘要前12位>.py`，位于部署根目录，预检会核对完整 SHA-256、两个 Git HEAD、容器运行状态、loopback 与未发布端口。手动启动 `uv run python -m autoeval.worker monitored`，或使用 compose `workers` profile 中的 `monitored-worker`。默认离线测试不覆盖容器；`AE_MONITOR_CONTAINER_TEST=1` 仅用于已准备好固定容器和独立夹具根目录的无模型专项，`AE_INFRA_TEST=1` 用于真实 PostgreSQL/S3。历史真实任务不自动重跑，Harbor 真实执行未验收。

人工产物归档验证新增0025迁移及 `uv run python -m autoeval.worker fixture`，使用独立队列和后台受保护配置。[端到端记录](../docs/platform-fixture-validation-2026-11-12.md)包含任务、对象及报告标识；它不代表模型测评执行通过。

新增迁移 `0024_unified_outputs`：统一输出解析、不可变归档、case 工具观测字段及 Result Worker 收集活动。新版输入默认固定适配器 `955089a`，将平台运行参数与工具参数分离。GPQA远程监控路径已完成真实5样本验收，其他基准的远程执行仍待验收；[完整说明](../docs/unified-output-integration.md)。

单团队、单工作空间，无项目/租户层。已验证本机Docker的GPQA Lite探索闭环，尚未完成全部工具、客户生产资源及生产验收。极光仍是独立资源提供方；页面直接展示真实业务数据，未启用能力明确保留边界。

标准仓输入模块新增迁移 `0023_standard_inputs` 与 `/api/autoeval/v1/standard-inputs` 接口，支持输入登记、差异预览与脱敏配置编译；不启动测评、不写入批准基线。运行 `uv run alembic upgrade head` 并重启 API，使用方式和验证边界见[标准输入接入说明](../docs/standard-inputs.md)。

## 历史收口基线（2026-11-11）

- 全量228项通过、无跳过；Ruff、TypeScript及前端构建通过。默认pytest跳过联调专项，不能替代该结果。
- 主机S3端点已切换到http://127.0.0.1:9002，固定摘要的GHCR社区MinIO镜像配独立持久卷；84个现存对象逐个校验并通过容器重启读回。原9000端点仅作协议模拟/迁移回退。
- 两份历史真实报告原文无备份，保留索引并标记证据缺失；隐藏有效成绩、基准不计算差值、榜单排除，不重算或补造成绩。
- SSH模拟器已改为前台sshd，实际重启后验证通过；API之外必须运行Workflow/I/O/GPQA Worker、Outbox派发及对账。
- 运行、备份、精确恢复和验证边界见[恢复指南](../docs/development-recovery.md)与[验证记录](../docs/validation-2026-11-11.md)。

## 已实现

- FastAPI应用工厂、lifespan资源回收、请求ID、统一异常和OpenAPI。
- `/health/live`存活检查、`/health/ready`真实检查PostgreSQL/Temporal/S3 bucket；依赖不可用返回503。
- `/api/autoeval/v1/system/capabilities`分别报告GPQA探索、分析配置与尚未启用的正式Release/极光。
- 版本化模型、端点、配置、数据清单、Profile、运行快照、case证据、报告及审计，迁移当前至0027（含标准仓输入、榜单评审、监控与任务规划数据表）。
- Temporal工作流、专用I/O/GPQA/分析Worker及Outbox；未知调用不盲目重发，未实现活动明确失败。
- 真实报告、条件匹配的批准JSON基准、内部榜单、独立分析及引用证据校验；分析需显式配置独立目标/资源。
- 对账器恢复诊断和已归档GPQA attempt；状态未确认保留占用，清理按所有权幂等执行并记录审计。
- PostgreSQL/S3隔离scratch备份恢复演练；不覆盖现有数据库或桶，不宣称生产耐久性已验收。
- Python 3.12隔离环境、uv.lock和带hash的容器依赖清单；Compose开发拓扑与Vite API代理。

## 本地运行（PowerShell）

```powershell
cd backend
uv sync --frozen --python 3.12
Copy-Item .env.example .env
# 编辑.env：真实值只保存在本地，不提交Git
uv run uvicorn autoeval.api:app --host 127.0.0.1 --port 8000 --reload
```

访问 http://127.0.0.1:8000/docs 。API启动不强依赖外部服务，未启动依赖时ready=503是预期结果。前端在仓库的`frontend/`目录运行`npm run dev`，Vite将`/api/autoeval`转发到8000。

## 原Compose协议测试拓扑

以下保留原协议模拟拓扑说明，其中storage为非持久化Moto。当前业务联调使用独立持久化MinIO、主机API和Worker，按[恢复指南](../docs/development-recovery.md)启动，不应直接以此拓扑存储历史证据。

```powershell
Copy-Item deploy/.env.example deploy/.env
# 修改本地密码；以下均在demo根目录执行
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build postgres temporal storage api
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile tools run --rm migrate
# 在backend中运行 uv run python -m autoeval.bootstrap storage，再检查/health/ready
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile workers up -d
```

PostgreSQL 5432；Temporal 7233、开发UI 8233；开发S3 9000；API 8000，仅绑定127.0.0.1。Temporal当前采用独立持久卷上的开发SQLite，**不是生产服务**；不共用业务数据库。正式环境需独立Temporal数据库/账号及真实身份，当前代码主动拒绝production环境使用local身份。

开发基础镜像已拉取并验证PostgreSQL、Temporal和S3协议；生产前仍需锁定digest和完整软件环境。GPQA探索由主机Worker通过Docker CLI控制执行容器；通用后端Compose镜像不自动具备Docker控制能力。

## 开发检查

```powershell
cd backend
uv run pytest -q
uv run ruff check .
uv run alembic upgrade head --sql
# 依赖变更后同步容器hash清单
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock.txt
```

离线SQL生成和SQLAlchemy编译测试不等于真实数据库约束已验证。请勿用SQLite替代PostgreSQL验证JSONB、SKIP LOCKED、唯一约束与并发行为。

## 下一批实现边界

1. 完成MMMU-Pro正式数据、图像对象证据、平台任务、工作流/报告及真实模型联调。
2. 接入AutomationBench并明确工具、数据契约与真实执行验收。
3. 实现正式Release的批准配置、范围与门禁规则。
4. 完成生产身份、镜像封装、监控、部署及生产存储耐久性验收。

Outbox按payload路由运行、恢复与分析工作流，使用稳定Workflow ID去重。CANCEL_RUN仍为schema占位，取消由运行行意图轮询消费。派发采用单行有界事务持锁；多副本生产前仍需验证锁竞争与派发容量。

已完成Docker和SSH的有限GPQA模型生成联调及本机OpenSSH恢复验证；客户生产环境尚未验收。本地凭据引用不能替代生产密钥服务。已有开发操作审计，正式身份、最小权限及生产密钥服务尚未完成。

## Docker诊断联调

完成迁移与资源登记后，同时启动API、`uv run python -m autoeval.worker workflow`、`uv run python -m autoeval.worker io`和`uv run python -m autoeval.dispatcher`，然后打开前端任务管理页的“本机 Docker 诊断”。该区域使用真实数据库和执行容器，支持15秒诊断、取消及刷新后找回历史。

诊断API前缀为`/api/autoeval/v1/diagnostics/runs`，提交必须提供Idempotency-Key。相同键/正文返回同一运行，正文冲突返回409。取消意图持久化在运行行，由活动轮询消费，不依赖尚未启用的CANCEL_RUN Outbox。执行状态不明保留占用并记录execution_unresolved。

另行启动`uv run python -m autoeval.reconciler`，每60秒检查创建超过5分钟的非终态运行，以及已终态但仍持有执行租约的诊断任务。活跃Workflow不竞争写入；仅对已关闭Workflow的诊断任务恢复投影，使用原作业键查证，不调用submit。作业已终止才收集结果、补齐事件并在确认清理后释放占用；运行中取消继续按进程身份查验，找不到作业或外部不可达时保留占用。对账器同时恢复已归档GPQA attempt和到期清理，长期运行心跳策略仍需生产验证。

该链路仅验证框架，不调用模型、不计分、不判定Release通过。浏览器测试`tests/test_browser_diagnostics.py`需要运行中的Vite/API、两个Worker、投递器及Docker基础服务；以AE_BROWSER_URL和AE_CHROME_PATH启用。

## 开发存储更新

原MinIO官方镜像当前不可访问，Compose开发环境改为官方Moto server 5.1.22提供S3协议模拟。它不提供生产身份与持久化承诺；重建/重启可能丢失模拟产物，不用于正式历史case证据。生产仍使用受支持的S3兼容存储。Temporal的/data卷先由一次性初始化容器设为UID 1000所有，避免非root启动失败。

Docker守护进程代理异常而Windows HTTPS可达时，可用deploy/pull_image_via_host.py下载官方DockerHub镜像、校验blob与层哈希后docker load；不修改全局代理。

## 数据集清单

配置页可登记JSON清单，或调用POST `/api/autoeval/v1/dataset-manifests`：

```json
{
  "dataset_key": "example-fixture",
  "name": "自建联调样本（非正式基准）",
  "version": "v1",
  "manifest_key": "full",
  "scope": "full",
  "samples": [{"sample_id": "case-001", "sha256": "实际样本内容的64位小写SHA-256"}]
}
```

样本内容hash由数据导入方生成；清单登记不验证原题是否存在或评分是否正确。Lite需scope=lite、不同manifest_key及parent_id=同版本Full清单UUID，samples为原清单中ID/hash完全相同的成员。已登记版本不覆盖；Full样本集改变须新建数据版本。清单顺序进入hash，影响执行身份。

GET支持`?offset=0&limit=50`，next_offset=null为末页。选择器可按分页继续加载，不再以首页200条作为完整数据。对象存储缺失或完整性失败返回503，禁止静默使用空清单。

## Profile与配置绑定

POST `/api/autoeval/v1/profiles`登记profile_key/version/dataset_name、full_manifest_id、可选lite_manifest_id及tool契约。tool明确family（aisbench/harbor）、source_url、revision（固定Git提交hash）、scorer_reference、scorer_sha256和parallelism_owner（tool/platform）。所有登记当前仍tool_verified=false，注册不代表工具或评分已验收。

创建配置时可提供`profile_bindings: {"GPQA": "Profile UUID"}`，必须覆盖items全部条目；按每条scope解析Full/Lite，缺失Lite拒绝。已有绑定的配置后续版本必须显式保留或替换绑定。配置页独立执行绑定面板支持此操作，普通参数编辑区缺少绑定时会提示后端冲突。

GET `/api/autoeval/v1/test-configs/{id}/execution-spec`查看配置、Profile与清单摘要，bindings_complete仅表示引用齐备。GPQA探索提交还会校验固定原生工具契约并冻结端点、资源及数据包；读取配置规格本身不会创建运行，其他工具不能据绑定完整推断可执行。

## Case回溯

内部begin_attempt在外部调用前持久化started，finish_attempt归档确认结果。重试使用新attempt_no与operation_key；同一operation_key不得对应不同样本或次数。结束后不可覆盖，原生计分与执行成功状态分开。

任务管理页的case历史回溯面板按运行UUID查询每次尝试和校验后的证据。API：GET `/api/autoeval/v1/runs/{id}/case-executions?dataset_key=...&sample_id=...&offset=0&limit=50`；GET `/api/autoeval/v1/case-executions/{id}/evidence`。暂无面向外部的结果上传/改写接口，工具Worker集成仍在进行。

PostgreSQL触发器保护归档行及执行身份；对象存储耐久性仍需生产S3验证，Moto只用于协议模拟。数据库触发器不能防止超级用户绕过，不代替正式最小权限和备份。当前没有自动删除case历史或证据的操作。

## 显式原生模型联调

`uv run python -m autoeval.native_smoke --target-id <登记的官方DeepSeek目标UUID> --resource-id <Docker资源UUID>` 会发出一次真实模型生成请求，最多32输出tokens，无自动重试。必须先布置docs/aisbench-integration.md中的固定Linux工具目录。该命令只用自建算术fixture，不能作为GPQA成绩或正式门禁验收。凭据从受保护配置按引用读取并通过stdin传给容器，不接收命令行明文密钥。

结果给出run_id/attempt_id，可在任务页case回溯面板查证。若state=unresolved，不要直接重跑；先查attempt/作业与占用，当前native-smoke恢复仍为人工调查路径，不适用DockerDiagnosticWorkflow的自动对账。正式测评还需要Temporal活动、批准数据/配置、运行快照、完整报告及原生工具差分验收。

## GPQA公开数据导入

作者仓库固定来源见tool-locks/gpqa-source.json。将CSV和source.json放入受控本地目录后，运行：

`uv run python -m autoeval.import_gpqa --csv <gpqa_diamond.csv绝对路径> --source <source.json绝对路径> --resource-id <Docker资源UUID> --lite-count 5`

该命令不调用模型；固定全量原生加载后再生成Lite，数据正文只在本地缓存、Docker导入目录和S3数据包内，Git保留摘要。版本由CSV摘要与工具提交构成，改变任何一方会生成不同版本。来源状态not_approved，不能自动用于正式Release验收。配置页可查看清单来源及摘要。

AE_GPQA_CSV指向该实际CSV时，tests/test_gpqa_import_live.py会启用真实工具/数据库/S3专项，不发出付费请求。数据导入和S3协议验证不能替代生产存储耐久性或正式测评正确性验收。

## GPQA Lite探索任务

开发启动API、workflow Worker、`uv run python -m autoeval.worker gpqa`及Outbox投递器。gpqa Worker需要主机Docker CLI、已登记执行容器和固定/work/tools/aisbench目录；现有通用Compose后端容器没有自动配置Docker CLI/daemon权限，不能直接声称容器化GPQA Worker已就绪。Docker本地联调由主机Worker控制执行容器，SSH路径待实现。

任务页新增真实GPQA Lite入口。POST `/api/autoeval/v1/runs`请求：config_version_id、target_id、resource_id、run_mode=exploration、max_model_requests（1—5），必须Idempotency-Key。首批固定Lite<=5、单GPQA、并发1、max output<=64、temperature=0/top_p=1。配置需要绑定与原生锁一致的Profile及实际导入的数据包。其他组合明确拒绝，不回退示例成绩。

GET `/runs`支持offset/limit，GET `/runs/{id}`返回进度和报告摘要，GET `/runs/{id}/report`校验并返回完整报告证据。POST `/runs/{id}/cancel`仅保存取消意图，当前请求返回后停止后续样本。未知状态不重发请求，当前正式探索任务仍需人工查证，不套用诊断任务恢复器。

报告complete指完整覆盖所选Lite清单，不能解读为全量198题。partial只显示成功样本范围指标；failed_summary无有效执行，invalid表示评分失败、accuracy=null。正式发布门禁始终not_applicable。后续继续完成分析、基准、正式Release规则及其他工具。

AE_GPQA_PIPELINE_TEST=1启用真实SQL/S3/Temporal、模拟模型边界专项；专用随机测试队列避免与真实模型Worker混用，该测试不发出付费请求。真实模型联调须明确提交任务，禁止由普通测试自动重复触发。

## 报告目录与产物来源

报告管理页已读取真实后端，GET `/api/autoeval/v1/reports?offset=0&limit=20`返回目录及next_offset，GET `/reports/{id}`读取详情，GET `/reports/{id}/evidence`校验S3正文。`#report/<UUID>`可刷新直达。原页面直接展示真实报告；未采集或不支持的指标保持空态，不使用静态示例补齐。

execution_origin由服务端运行环境冻结，默认real；模拟测试应用显式设为simulation。历史来源不明的报告默认unverified。普通报告目录只展示real，不把模拟成绩当真实结果。历史来源核验只补充元数据/审计，不重写原始成绩或快照。自动分析需要独立配置，未启用时不展示虚构结论。

## 独立分析与恢复

仅`execution_origin=real`的普通测评报告可分析。配置AE_ANALYSIS_TARGET_ID和AE_ANALYSIS_RESOURCE_ID后，仅开放手动分析能力，报告生成不自动创建分析意图；分析与被测的target绑定、模型版本、remote_model、credential_ref、resource绑定及实际Docker container必须全部不同，未满足条件时拒绝手动启动，不影响原始报告归档。`GET /reports/{id}/analysis`查询结果；未排队报告通过`POST /reports/{id}/analysis:start`创建`manual_start`任务且`retry_count=0`，失败或待查证后可通过`POST /reports/{id}/analysis:retry`复用同一任务手动重新分析，保留历史元数据；每次重新分析均可能产生新的模型费用。分析输入对象、摘要和证据目录在进入`running`前完成冻结，模型引用只能使用服务端证据目录；重试不重测、不改报告。

启动`uv run python -m autoeval.worker analysis-workflow`、`uv run python -m autoeval.worker result`及dispatcher。该功能可能产生模型费用，普通回归测试使用明确的模拟边界。

reconciler可恢复已有归档GPQA attempt，绝不重放状态未知的生成请求；unknown会保留占用及审计，等待证据查证。诊断清理使用持久状态和所有权边界，不删除客户既有对象。`autoeval.backup`提供备份及空scratch目标恢复；不得将其开发演练当成生产RPO/RTO证明。

AE_REPORTS_TEST=1启用已存在真实报告的只读目录/证据验证；搭配AE_BROWSER_URL及AE_CHROME_PATH可运行浏览器详情测试。它们不发出模型生成请求。

## 客户SSH资源（首批控制层）

控制台提供独立SSH资源登记/预检。API前缀`/api/autoeval/v1/ssh-resources`，POST `/credentials`保存password/private_key并返回引用，POST登记绑定，GET分页，POST `/{id}/validate`显式预检。主机指纹必须为批准的SHA256格式，不提供自动接受首次连接。密钥类型支持未加密RSA/Ed25519/ECDSA；加密私钥尚未支持。

SSHExecutor只开放固定framework_smoke和0—30秒持续时间，不接收任意命令。远程目录限定在workdir/.autoeval/绑定UUID，结果经SFTP收集，只有已确认终止的自有作业允许清理。既有进程和数据不纳入清理范围。控制面需要直连，无常驻Agent或反向组件。

本机协议联调：可用`deploy/ssh-sim.Dockerfile`构建OpenSSH开发镜像，启动名为autoeval-ssh-sim、带autoeval.role=ssh-simulator标签的容器，仅映射127.0.0.1:2222:22；再从backend执行`uv run python ../deploy/setup_ssh_sim.py`。脚本生成本机专用客户端密钥并存于受保护.secrets，私钥不复制进容器。实际本轮使用已安装OpenSSH的独立Python开发容器验证；该Dockerfile构建尚未在受限网络中验收。

AE_SSH_SIM_TEST=1启用真实SSH/SFTP专项，配合AE_BROWSER_URL和AE_CHROME_PATH可验证页面登记预检，不调用模型。填写native_tool_root后还需通过批准桥接代码、源码锁和原生Prompt预检，才会返回GPQA探索可用；仅连接成功不会返回可测评。

SSHNativeTransport要求批准的工具路径、桥接hash和源码锁hash，远程native_request_runtime仅接收固定GPQA推理操作，秘密仅走stdin。结果以attempt UUID作为稳定作业键查证并通过SFTP校验，未知状态不重发。原生进程工作目录位于自有作业目录。

## SSH的GPQA Lite探索

登记SSH资源时可提供native_tool_root，目录中需部署与后台批准版本完全一致的bridge.py、lock.json、source/与site-packages/。后台分别冻结桥接和源码锁摘要，不能随意替换工具。工具安装不由页面自动执行；不新增客户常驻Agent。

任务页选择客户SSH服务器，POST `/api/autoeval/v1/runs`使用resource_kind=customer_ssh和SSH资源UUID；其余配置、Lite预算与目标模型限制同Docker路径。省略resource_kind时继续按Docker处理，并保留既有幂等键摘要。SSH快照不包含凭据引用和API Key，不依赖Docker CLI或极光账号。

原有gpqa Worker和Temporal工作流同时处理两种路径。SSH断连后按attempt UUID读取已持久化的远程结果、校验请求/结果摘要并归档；started attempt没有确认结果时保持占用，不重发。真实Worker拒绝模拟来源任务，对账器只扫描与运行环境匹配的测评及诊断任务。

本机已验证一次真实SSH Lite1模型调用与浏览器报告刷新；这不代表Full或正式Release通过。AE_SSH_NATIVE_PIPELINE_TEST=1启用真实SSH/Temporal/S3与模拟模型边界的编排/恢复测试，不产生费用。不得递归清理客户执行目录。

## SSH归档后清理与恢复

迁移0020新增execution_cleanup，独立于运行终态及报告有效性，状态为pending/running/failed/cleaned。报告和清理意图同事务提交，分析意图仅在手动启动时创建；SSH占用保留至全部已执行attempt的清理确认，Docker探索路径保持原行为。

启动`uv run python -m autoeval.reconciler`，每60秒扫描同execution_origin的到期清理。后台先验证冻结快照、报告、全体case及数据集内容的对象摘要和身份，再核验远程请求/结果；删除前持久化verified_proofs，防止删除中断后无法继续。仅清理attempt UUID目录内本次创建的临时结果，保留提交摘要及清理回执，防止同一UUID重新执行。归档case和报告始终保留。

清理使用10分钟可接管claim及token防止过期写回；远程操作按归档证明幂等。失败保留占用、记录审计并从60秒退避至最多1小时。进程中断后claim到期自动接管；若部署进程已确认停止，可由运维确认后调整特定清理claim到期时间，禁止以重提测评替代恢复。清理异常不触发模型或修改成绩。

GET `/api/autoeval/v1/runs/{id}`返回cleanup状态、次数和分类错误；POST同路径`/cleanup`持久化重试意图（202，不同步等待SSH），适用于当前来源的终态SSH运行，也可为旧报告补建清理意图。重复调用不重测，不抢占有效claim。页面展示清理进度与失败重试入口。框架终态与清理成功相互独立；清理失败不能被视为报告无效，也不能提前释放资源。

验证需启用上述SSH流水线标志；AE_BROWSER_URL、AE_CHROME_PATH和AE_SSH_SIM_TEST同时启用可验证已回收任务的页面及刷新。测试明确模拟模型边界，不替代客户生产服务器验收。

## GPQA业务导航与分页

控制台的已保存服务可进入`#evaluate?target=<UUID>`，按服务关联的模型读取当前默认配置版本；服务或默认配置不在当前页时，通过精确ID查询恢复选择。进入页面不会探测或调用模型，必须明确选择执行资源并提交。默认配置不适用于GPQA Lite时显示原因，允许显式选择其他已绑定Profile的配置，不悄悄替换。

服务查询`GET /target-bindings`支持可选target_id，返回模型ID/业务键、模型版本和当前默认配置版本；不返回凭据引用。配置查询`GET /test-configs`支持可选config_id，均保留offset/limit/next_offset语义；精确ID不存在返回空列表，非法UUID返回422。

`#run/<UUID>`是可刷新和分享的真实运行详情，原任务列表与详情均读取该运行接口。任务页的配置、服务、资源和运行均按20条分页，翻页保留已选项；无符合条件的选项时可以继续翻页，不以第一页为空推断全库无可用项。运行列表返回模型、资源类型；详情返回白名单冻结参数和摘要，排除凭据、私有扩展及原始样本正文。

运行详情自动带入case历史查询，报告可返回来源运行，运行可进入报告。默认配置更新不改变历史运行快照。列表每5秒、详情每2秒刷新；隐藏标签页暂停轮询，切换运行/分页会中止旧请求并清除旧详情，避免过期响应覆盖当前任务。控制台与任务页直接读取后端，未启用能力显示原因，不展示静态示例。

探索提交的未确认意图在当前标签页sessionStorage保留相同请求摘要和幂等键；仅含配置/服务/资源UUID、模式与请求上限，绝不存凭据。刷新后重新选择同一请求仍使用原键；成功确认后删除，修改请求则生成新键。存储受限时降级为组件内意图。浏览器故障测试在到达后台前拦截两次提交，验证刷新后同键；后台真实幂等性另由SQL/流水线测试验证，不调用付费模型。

## SSH工具版本固定

迁移0021为资源固定native_bridge_sha256、native_lock_sha256、native_tool_revision。填写工具目录的新登记绑定固定当时后台批准的文件与版本；预检及快照使用该绑定的固定值，不读取后台升级后的当前摘要。工具目录、摘要及版本由数据库触发器保护，升级需独立目录和新绑定，保留旧运行所依赖的目录；凭据不进入快照。

历史资源只从同一资源ID和工具目录的唯一历史快照版本回填。缺少或存在冲突证据时保持未固定状态，禁止隐式批准当前代码；按批准部署重新登记。预检仍逐字节核验实际工具，不因存在历史记录就声明测评可用。页面展示固定版本和摘要。迁移不会修改任何既有运行快照。

## MMMU-Pro多模态桥接（工具层）

固定AISBench路径新增request_mmmu_pro_options10_cot（只构造请求，不连接模型）和infer_mmmu_pro_smoke（显式推理）操作。原生Loader、MMPromptTemplate、APITemplateParser、VLLMCustomAPIChat及MMMUProCotEvaluator保持批准revision；只将原生Prompt中的冻结图像引用转换为内联data URL，文本和图像位置不自行重排，不读取任意file://或远程图片链接，也不写共享运行时图片目录。

输入图像逐一校验base64、字节摘要、实际格式和尺寸，并要求与样本原生标签中的引用集合一致。返回不含图像正文的input_images身份清单及可复现的逻辑Prompt；请求预览另返回原生请求体和摘要，不能将含图像正文的预览直接写入普通日志。API Key只进入原生模型的鉴权头，不进入请求体、Prompt或图像清单。

当前桥接工程保护上限为8张图、单图4MiB、总图像8MiB、单边8192像素及64输出tokens，只支持批准的官方DeepSeek Flash目标；这些是开发桥接边界，不能作为基准合格阈值或正式测评参数。实际图像格式由Pillow验证，支持JPEG/PNG/GIF/WebP；空输出和length终止保持原生执行/评分语义，诊断不擅自判无效。模型HTTP调用至多一次，禁止自动重发。

官方视觉协议核对来源：[DeepSeek Vision](https://api-docs.deepseek.com/guides/vision/)（2026-11-10读取）。AE_MMMU_NATIVE_TEST=1运行固定工具与HTTP替身测试，覆盖原生请求差分、图像篡改/未绑定/非法格式、截断/空输出及请求失败。替身不产生真实测评成绩，也不证明账号的图像推理已联调。

当前不开放MMMU-Pro平台任务入口：正式数据版本导入、对象存储图像证据、工作流/取消恢复、报告及真实模型联调尚需继续。GPQA入口仍只接受GPQA；不得将工具层测试通过扩展为MMMU-Pro端到端验收或正式Release通过。SSH升级该桥接应新建独立工具目录和绑定，旧目录按原固定版本保留。

## 开发数据工作台与官方数据脚本

`/api/autoeval/v1/data-workbench` 提供榜单来源记录、版本校验、评审、撤回及发布查询，限 development/test。界面 JSON 导入创建待评审记录；`scripts/` 中部分定向导入脚本会进一步调用批准接口，属于写操作，运行前检查脚本与来源快照。当前没有通用官网连接器或定时调度。详见 [工作台说明](../docs/data-workbench.md)。
