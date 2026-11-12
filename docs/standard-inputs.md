# 标准仓输入接入

更新：新版默认输入契约已升级为适配器 `955089a`，Docker 参数移至 `runtime_config`，不再生成 `--no-template`；下文旧版本说明保留为历史输入契约参考。统一输出新增能力及尚未开放的远程执行边界见[新版接入说明](./unified-output-integration.md)。

本模块只登记标准输入、建立模型映射并生成适配器启动配置，不执行测评。输出格式、结果收集、报告、基线审批及门禁均未接入。本机已增加迁移 `0023_standard_inputs`；其他环境先执行 `uv run alembic upgrade head`，再重启 API。

## 版本与职责

- 标准仓：`SJTUyh/AutoEval-Standard`。远程分支先解析完整提交，再按同一提交获取目录及文件。
- 测试来源固定为 `2f8f167a68f86ee022afcc7f2ae2b62bc2e6d2a2`，包含两个模型、三个基准的六组配置。测试附件是上游原文字节，不代表平台批准或官方成绩核验。
- 适配器输入契约固定为 `SJTUyh/AutoEval-Benchmark-Adaptor@1fb877ace4a087cb805e9a3d5878dcce6349bc35`。升级需重新检查参数契约及配置生成。
- GPQA_Diamond 映射为 GPQA/diamond，MMMU-Pro 为 MMMU-Pro/options10，DeepSWE 为独立的 DeepSWE/v1.1。后两者同样只校验输入，不因此开放任务执行。
- `standard_config.json` 保存为锁定约束，`benchmark_config.json` 为可选启动配置；缺失时登记为“待补全启动配置”。`baseline.json` 原样保存为来源附件，不转换为门禁基线或榜单数据。

## 页面与离线导入

列表、详情和同步预览按完整提交匹配标准仓当前 Tag，支持输入 Tag 同步。例如 `2026-RP0` 对应 `2f8f167a68f86ee022afcc7f2ae2b62bc2e6d2a2`。标签通过 `GET /standard-inputs/tags` 查询；它是仓库当前标签，不改写已登记原文、提交或哈希。网络失败显示“Tag 信息暂不可用”，不误判为无标签；标签移动或删除后，以历史提交号继续追溯。

开发环境进入“数据工作台 → 标准仓输入”，填写分支或提交并同步预览；核对提交、文件哈希、标准字段差异和原文后确认登记。测评配置页的“标准仓输入与版本”抽屉提供同一入口；已有测评配置与探索流程保持独立。

离线目录不通过 HTTP 传入后台路径，先在后台命令行生成 JSON 包，再由页面选择文件并预览：

```powershell
cd backend
uv run python -m autoeval.import_standard_inputs --directory '<离线标准仓目录>' --revision '<40位提交号>' --output '<输入包.json>'
```

离线包的提交号由操作者提供，哈希保证存储内容可追溯，不代表平台验证了离线文件与 GitHub 提交一致；确认前需自行核对来源。远程同步只允许上述标准仓，最多 300 个文件、原文字节总计 2 MB；重复键、非有限数值和明文凭据拒绝。目录结构须为 `模型/基准/文件.json`。

## 补全与编译

测评配置页默认展示标准仓输入版本，详情直接展示嵌套的 `tool/subcommand/params` 原文，锁定字段路径仅用于校验，不替代配置格式。自定义启动模板同样使用此结构，经编译接口校验后追加快照；旧版五项参数配置保留在历史标准配置、历史通用自定义配置页签，不自动转换、不覆盖历史任务。

在输入详情中查看来源、锁定字段及基线附件，再打开“补全启动参数与模型映射”抽屉：

1. 选择已登记模型及属于该模型的服务，后台保存 ID 映射；凭据只通过服务引用，在本阶段不会读取密钥。
2. 填写版本化启动模板及可调整参数，结构为 `{"params": {...}}`，也可附带 `tool`、`subcommand`。完整模板修改必须使用新的版本名；版本名约束在同一标准输入下生效。仓库缺少 `benchmark_config.json` 时，平台启动模板必须包含非空 `params`；只允许调整参数不能替代，否则会返回 `startup_template_required` 且 `input_ready=false`。
3. 显式选择 Docker 或执行服务器本机环境，填写执行环境中的绝对 POSIX 路径。AISBench 源码目录必填；LLM 输入另需数据目录。DeepSWE 任务路径与标准锁定路径冲突时阻断，不擅自改写锁定值。
4. 点击“校验并保存输入快照”，查看缺失项、参数来源、覆盖差异和脱敏配置。修改表单即清除旧校验结果。

编译顺序为仓库启动配置 → 平台模板及用户调整 → 标准递归覆盖 → 平台环境参数。对象逐键合并，数组和标量整体替换。最终锁定字段再次检查；`model_overrides` 和非空 `extra_cli_args` 禁用，避免绕过锁定。端点、执行方式、容器标识及作业目录不允许在用户调整中注入。容器名和目录由输入内容确定，**仅是配置准备标识，尚不具备运行提交键及资源所有权语义**。

示例模板：

```json
{
  "tool": "ais_bench",
  "subcommand": "llm_accuracy",
  "params": {
    "batch_size": 1,
    "max_num_workers": 1,
    "dump_eval_details": true
  }
}
```

输入齐备仅表示已知参数类型、必填字段和锁定规则通过静态检查。数据是否存在、端点协议兼容、视觉输入、工具依赖、模型能力及 Agent 环境均需后续真实联调。生成配置的使用标志为 `--no-template`，避免适配器再引入默认模板；此处不提供执行按钮。配置不含 `api_key`，将来执行桥接必须在临时受保护配置中注入凭据。

## API

统一前缀 `/api/autoeval/v1/standard-inputs`，仅 development/test，沿用受控本地操作员身份。所有响应均包裹 `data`、`request_id`。

| 方法与路径 | 请求及行为 |
| --- | --- |
| POST `/sync-preview` | `{ "ref": "main" }`，固定提交并返回 bundle、sha256、entries及差异，不写业务数据 |
| POST `/preview` | `{repository, revision, files}`；files 为相对路径到 UTF-8 原文的映射 |
| POST `/import` | `{bundle, expected_sha256}`；与预览包哈希一致才登记；整个包同一事务 |
| GET `?offset=0&limit=20` | 分页输入版本，支持 model_id 查询已编译映射；返回 next_offset |
| GET `/{id}` | 来源原文、锁定参数及最近20条编译历史 |
| POST `/{id}/compile` | model_id、target_id、template_version、template、adjustments、execution、tool_root、datasets_dir、task_path；返回 config、sha256、input_ready、blocking_reasons、field_sources、overrides |

编译响应始终 `execution_enabled=false`、`approval_status=not_approved`。缺少输入可保存为有阻断项的快照；未知参数、锁定绕过等非法请求返回422，版本或哈希冲突409，输入不存在404，非开发环境403。GitHub访问失败返回422，可改用离线包。不会因网络错误写入部分版本。

`standard_input_version` 按仓库、完整提交、来源模型、基准唯一，哈希涵盖完整原文；重复登记返回原 ID，冲突拒绝。`standard_input_compilation` 保存模型映射和脱敏编译快照，按内容哈希去重。两表均用数据库触发器阻止更新和删除；变更追加新版本。编译快照 SHA-256 使用去掉 sha256 字段后的规范 JSON（排序键、UTF-8、紧凑分隔符）计算。

## 验证命令与边界

```powershell
cd backend
$env:AE_INFRA_TEST='1'
$env:AE_ADAPTOR_SOURCE='<固定提交的适配器仓本地目录>'
uv run pytest tests/test_standard_inputs.py -q
$env:AE_BROWSER_TEST='1'
$env:AE_CHROME_PATH='<Chrome可执行文件路径>'
uv run pytest tests/browser_checks/test_browser_standard_inputs.py -q
```

适配器专项读取并验证本地 HEAD，与固定提交一致才执行 prepare；使用临时的模型/数据集配置定位夹具，不导入真实测评工具、不运行预检、不调用 Docker 或模型。浏览器专项全部模拟输入管理 API，不能代替真实资源及测评正确性验收。数据库专项使用真实 PostgreSQL，保留不可变的 TEST 来源测试记录。


## 自动补全启动配置

输入详情 → 补全启动参数与模型映射 → 自动补全配置。后台 `POST /api/autoeval/v1/standard-inputs/{id}/autocomplete` 只生成候选，不保存、不探测服务器、不执行模型。点击“校验并保存输入快照”后按现有不可变编译机制保存。

优先复用固定提交的 benchmark_config.json；缺少时由 platform-standard-template.v1 规则从标准约束生成模板，不引入隐式工具默认值。模板版本包含内容哈希；标准锁定字段仍最终覆盖。详情展示来源、补全前后差异、最终字段来源、覆盖及阻断项。

模型按 asset_key 忽略大小写匹配（保留原始名称），绑定模型只有一个服务时才自动选择；用户明确选定的模型和服务由后台验证归属。忽略大小写后多个候选不猜测。已有手填环境路径和用户调整保留；没有明确资源绑定时，不扫描或复制其他任务的容器、工具及数据目录，也不猜测 Agent 参数。缺失项继续阻断输入齐备状态，按原表单补充。该预览不是资源预检或可执行性承诺。

列表显示最近一次保存的编译完整性，审批和执行状态保持独立。原始标准输入与历史编译不覆盖。自动补全不修改标准模型的输出上限，也不沿用先前 GPQA Lite 的调用预算；执行前仍须独立授权。


## 官方目录与平台模型登记

数据工作台“模型列表”在已披露完整 ModelScope 标识的官方条目提供“登记模型身份”。后台 `POST /api/autoeval/v1/models/from-catalog` 接收 `catalog_id`，只从已评审的官方成绩记录中确认标识，按忽略大小写的完整标识加事务锁登记，重复请求复用同一模型及 `catalog-identity.v1` 版本。版本元数据记录来源记录 ID，明确 `identity_only=true`，不自动创建目标服务、凭据或执行权限。

标准输入补全按平台模型标识或名称忽略大小写匹配，候选不唯一时仍要求显式选择。完整标识保留厂商命名空间，目录和已登记模型按相同标识汇总；原有运行快照不回写。

2026-11-12 已登记 moonshotai/Kimi-K3、MiniMax/MiniMax-M3.1-Flash，18条标准输入已追加编译快照并完成模型映射。仍缺目标服务、工具及部分数据集目录；未调用模型、未批准配置或开放执行。
