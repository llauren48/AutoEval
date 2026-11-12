# 收口验证记录（2026-11-11）

初轮结论：未通过；修复后结果见末节，首期其他未实现范围不因此关闭。基线 HEAD=4b228fd，并包含当前未提交的MMMU桥接及其测试；本轮未提交或覆盖这些既有改动。

| 检查 | 结果 |
| --- | --- |
| Ruff | 通过 |
| TypeScript | 通过 |
| Vite生产构建 | 通过，1694模块 |
| 数据库迁移 | 0021，head |
| 启用全部既有联调开关的回归 | 183通过、26失败、0跳过，311.63秒 |

## 环境与限制

- 初始Docker、数据库和前后端未启动。已启动既有五个容器、API及Vite，未重建数据卷。
- 初始默认测试为132通过、70跳过、7个数据库连接错误，不能作为全量通过证据。
- S3协议模拟器重启后缺少证据桶，已显式初始化开发桶；初始化期间首轮部分测试遇到NoSuchBucket。
- 数据库有2份真实报告，对应2个报告对象均缺失；初始化空桶不能恢复历史证据。
- SSH容器虽显示运行，但握手出现EOF/banner错误；尚未证明是业务适配缺陷。
- 浏览器诊断任务停在accepted，后续提交出现409/占用冲突；需确认Worker、Outbox与既有运行状态后恢复，不能直接删除占用。
- 本轮未发送真实模型生成请求。历史测试成绩不替代本次结果。

## 收口顺序

1. 核验对象存储备份或精确原始证据来源；不能补造历史成绩。需要持久化存储方案与重启恢复验证。
2. 检查SSH容器内sshd运行/监听及批准指纹，不用容器Up替代SSH可用。
3. 按运行与工作流事实恢复Worker/派发和占用，避免盲目重提或重跑模型。
4. 环境稳定后先定向复测下列失败，再执行最终全量并更新开发计划状态。

## 本轮失败用例

- `tests.test_backup_live::test_backup_verifies_tampering_then_restores_only_scratch`
- `tests.test_browser_case_history::test_browser_failed_attempt_is_still_available_after_success`
- `tests.test_browser_diagnostics::test_browser_diagnostic_execute_reload_and_cancel`
- `tests.test_browser_reports::test_browser_real_report_evidence_and_direct_reload`
- `tests.test_browser_run_navigation::test_report_to_direct_run_snapshot_case_history_and_reload`
- `tests.test_browser_ssh_resources::test_browser_register_private_key_and_preflight_local_ssh`
- `tests.test_diagnostic_pipeline_live::test_diagnostic_submit_deduplicate_execute_and_cancel`
- `tests.test_evaluation_pipeline_live::test_submit_deduplicate_partial_report_and_cancel`
- `tests.test_gpqa_import_live::test_reimport_preserves_ids_and_bundle_matches_manifest`
- `tests.test_native_smoke_integration::test_native_smoke_records_valid_transitions_without_secret_snapshot`
- `tests.test_reconciliation_live::test_recover_closed_workflow_without_restarting_job`
- `tests.test_reconciliation_live::test_browser_unknown_recovery_and_audit_are_consistent`
- `tests.test_reports_live::test_report_catalog_excludes_unverified_and_simulated_artifacts`
- `tests.test_ssh_evaluation_live::test_ssh_submission_snapshot_report_and_cancel`
- `tests.test_ssh_evaluation_live::test_ssh_started_attempt_recovers_remote_evidence_without_model_request`
- `tests.test_ssh_executor_live::test_ssh_submission_deduplicates_and_collects_result_via_sftp`
- `tests.test_ssh_executor_live::test_ssh_cancel_and_cleanup_preserve_unowned_files`
- `tests.test_ssh_executor_live::test_ssh_fingerprint_mismatch_rejects_before_authentication`
- `tests.test_ssh_native_live::test_lost_ssh_ack_is_recovered_without_reexecuting_or_persisting_secret`
- `tests.test_ssh_native_live::test_native_key_conflict_and_result_corruption_do_not_trigger_new_execution`
- `tests.test_ssh_native_live::test_unapproved_bridge_never_executes_and_leaves_unknown_outcome[bridge_sha256]`
- `tests.test_ssh_native_live::test_unapproved_bridge_never_executes_and_leaves_unknown_outcome[lock_sha256]`
- `tests.test_ssh_native_live::test_killed_remote_process_is_unknown_and_never_automatically_restarted`
- `tests.test_ssh_native_live::test_native_cleanup_is_idempotent_owned_and_prevents_reexecution`
- `tests.test_ssh_native_live::test_native_cleanup_rejects_unverified_result_and_unknown_job`
- `tests.test_ssh_native_live::test_native_cleanup_recovers_interrupted_deletion_from_terminal_receipt`

## 修复后收口结果

- 2026-11-11最终全量：228 passed、0 failed、0 skipped，342.75秒；保留1项既有Starlette/httpx弃用警告。原始结果为backend/validation-durable-final.xml。
- 首轮183通过/26失败；恢复SSH/Worker及可复现数据后210通过/3失败；剩余3项关联不可恢复历史证据，现已补齐明确缺失状态、成绩隐藏、基准不计算及榜单排除的行为验证，没有补造模型输出。
- 相对首轮209项新增19项测试：精确恢复4、存储复制4、报告完整性/榜单/跨数据集防误标11；既有浏览器与接口用例增加缺失分支断言，不重复计数。
- Ruff、TypeScript与Vite构建通过。数据库迁移仍为0021；运行环境已切换为持久化MinIO，当前ready=200。
- 84个现存对象复制后逐个校验并通过重启读回。旧Moto端点保留，当前主机应用使用9002；两份旧报告的证据状态仍为missing，榜单分组为0、排除2份，不改历史数据库成绩。
- 实际SSH容器重启后11项专项通过，保留原批准指纹与回退容器。恢复派发和Worker之前确认没有非终态真实测评排队；修复与测试未主动发起真实生成请求。
- 基线包含4a09869及修复工作区；原有MMMU桥接代码与测试仍为单独未提交改动，本次全量执行包含它们，但本批提交不包含它们。
- 成功读取原始真实历史报告的分支本轮无法重新验证；报告页面本轮验证的是缺失保护与隔离。模拟模型的真实SQL/S3/Temporal/SSH流水线通过，不替代新真实测评或正式Release/生产验收。

运行、迁移、备份和镜像来源见[开发恢复指南](./development-recovery.md)。历史对象没有备份且不可重建，不将功能回归通过等同历史恢复成功。

## 标准仓输入对接复核

本轮修复当前未提交的标准仓输入改动，不替代上方既有全量联调记录，也未开启测评执行。

### 修复与回归证据

- 浏览器专项与后端专项原同名 `test_standard_inputs.py`，在非 package 测试目录中发生模块导入冲突。连续3次最小收集均为1个收集错误；浏览器文件改名为 `test_browser_standard_inputs.py` 后，联合收集25项成功，最终默认全量测试通过。
- 六组固定来源均缺 `benchmark_config.json`。原编译器只设置 `startup_missing`，未阻断空平台模板，导致仅有用户调整时也返回 `input_ready=true`。新增六组回归在修复前连续3次均为6项失败；修复后要求缺启动配置时提供含非空 `params` 的版本化平台模板，否则返回 `startup_template_required`，用户调整不能替代。
- 反向临时移除编译阻断后，新增回归再次为6项失败；恢复后两项相关参数化测试合计12项通过。六组完整输入与适配器 prepare 均显式使用平台模板。
- 浏览器专项增加非空模板提示、模板请求内容和锁定字段检查；为启动模板文本框补充明确的可访问标签。该专项使用模拟 API；真实 GitHub、真实 API 与浏览器端到端结果另见下节。

### 实际执行结果

| 检查 | 结果与边界 |
| --- | --- |
| `uv run pytest -q tests/test_standard_inputs.py`，同时设置 `AE_INFRA_TEST=1` 和固定 `AE_ADAPTOR_SOURCE` | **30 passed，0 skipped**；包含六组契约、合并/绕过、真实 PostgreSQL 和固定适配器 prepare |
| 真实 PostgreSQL 并发、冲突及不可变专项 | 单独执行为 **1 passed**；覆盖并发重复导入、内容冲突、编译版本冲突和历史表禁止 UPDATE；保留不可变 TEST 来源记录 |
| 固定适配器 prepare 专项 | 单独执行为 **1 passed**；本地 HEAD 固定为 `1fb877ace4a087cb805e9a3d5878dcce6349bc35`，六组均生成配置内容和命令，未写正式配置、未启动子进程 |
| 默认 `uv run pytest -q` | **195 passed，78 skipped**；同名收集冲突已消除，保留1项既有 Starlette/httpx 弃用警告；跳过项不冒充联调通过 |
| `uv run ruff check .` | 通过 |
| TypeScript `tsc --noEmit` | 通过；使用本机缓存 Node，因为 npm 未加入 PATH |
| Vite 生产构建 | 通过，1709个模块；构建前再次执行 TypeScript 检查 |
| `test_browser_standard_inputs.py` | **1 passed**；本机 Vite 与 Chrome，覆盖预览、差异、登记、模型/服务映射、非空模板、抽屉、锁定字段、脱敏及禁止运行入口；路由守卫确认无真实写请求 |

适配器源码仅克隆到临时目录并检出固定提交，未修改上游仓库。本轮没有调用模型、没有创建测评 Docker、没有启动真实测评，也没有接入输出解析、结果收集、报告、批准基线或门禁。数据库迁移为 `0023_standard_inputs`；其他环境仍需先执行迁移并重启 API。

### 真实 GitHub 同步端到端联调

- 真实链路为 Chrome → Vite `127.0.0.1:5173` → FastAPI `127.0.0.1:8000` → GitHub `SJTUyh/AutoEval-Standard` → PostgreSQL。重启既有 API 加载新路由后，`sync-preview` 固定到 `2f8f167a68f86ee022afcc7f2ae2b62bc2e6d2a2`，返回6组配置及来源附件，预览 SHA-256 为 `57d1f1dd5d9b35183fe97963950ce7927d1d12f5ceff2a51758587309bd5ce3e`。
- 企业代理链包含自签名 CA；API 使用从 Windows 受信根存储导出的临时 CA 包并通过 `SSL_CERT_FILE` 保持证书校验。本机脚本仅对 loopback 客户端设置 `trust_env=False`，避免本机代理拦截本地 POST；未关闭 TLS 校验，未绕过 GitHub 内容检查。
- 真实 API 导入返回201；相同 bundle 再次导入的6个 ID 完全一致。数据库复核该提交恰有6条记录、6个唯一 ID，全部 `execution_enabled=false`、`approval_status=not_approved`。详情 `e1e1e8ca-5fe9-428a-81d3-9300270336c8` 可直接读取，版本与固定提交一致。
- 浏览器未使用 Mock：预览展示完整提交和6条记录，两个来源模型均覆盖 DeepSWE、GPQA_Diamond、MMMU-Pro；点击“确认登记输入版本”显示“标准输入已登记，配置未批准，执行未开放”。请求证据为 `sync-preview` 200、`import` 201、版本列表200。
- 页面打开 `DeepSeek-V4.1-Flash-1017 / DeepSWE` 详情，显示映射 `DeepSWE / v1.1`、17个锁定字段、内容 SHA-256 `fda94f7d43d30f1272a23bc27097c753f18174b49819d7b7aa87709a489e9b55` 及“未批准 · 未开放执行”。首次详情读取被浏览器中止，随后相同 URL 重试返回200；未观察到 HTTP 4xx/5xx。

至此，原方案的真实 GitHub 同步、真实后端和浏览器端到端联调边界已补齐。该结论仍仅限输入侧，不开启执行、结果、报告或门禁验收。
