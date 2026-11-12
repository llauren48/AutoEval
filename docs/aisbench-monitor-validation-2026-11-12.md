# AISBench 运行监控验证记录（2026-11-12）

本记录只描述本轮修改后的实际执行结果，不把历史录屏或模拟响应计为验收通过。未启用真实模型调用，未提交、未推送代码。

## 1. 代码与环境

- 固定工具：AISBench `698ff2d1e027c07df37bdd158b62aace2a41ae3b`、适配器 `955089ab32ef696c231d628f5ad0d9da28f430c4`。
- 固定约束：`backend/tool-locks/aisbench-monitor.json`，含补丁文件与运行时 SHA-256、`monitor_port=18480`、`retry=0`、最多 5 样本、每样本最多 64 输出 tokens。
- 补丁：`backend/tool-patches/` 三个补丁均已从固定提交在临时克隆中重放验证，`--check --reverse` 通过；`local.py` 与适配器 `accuracy.py` 的新校验值由重放后的文件实际计算。
- 本轮未新增数据库迁移，未改写 `0026_run_monitor`。
- 前端无法验证：本机未安装 Node.js/npm，`npm run typecheck`、`npm run build` 与需要 Vite 的浏览器检查均未执行。

## 2. 默认离线回归

在 `backend/` 执行 `uv run pytest -q`：

```
240 passed, 101 skipped, 1 warning in 12.08s
```

专项结果：

| 命令 | 结果 |
| --- | --- |
| `uv run pytest -q tests/test_task_monitor.py` | 6 passed, 1 skipped |
| `uv run pytest -q tests/test_monitored_execution.py` | 5 passed |
| `uv run pytest -q tests/test_monitored_runtime_live.py tests/browser_checks/test_browser_monitor.py`（未启用开关） | 7 skipped |
| `uv run ruff check`（本轮修改的 Python 文件） | 通过 |

新增离线用例覆盖：未知工具状态与未来时间的显式诊断、Harbor 平铺与 cases 摘要、嵌套 raw/cases/extensions 脱敏、缺失值不补零、`final` 不被覆盖、预算拒绝先于读取模型密钥、丢失提交确认只查询不重发、成功与取消的终态归档、`last_observed` 回退、超预算结果无效化、终态重放幂等。

## 3. 真实 PostgreSQL / 对象存储一致性

| 端点 | 结果 |
| --- | --- |
| `AE_INFRA_TEST=1`，默认 `http://127.0.0.1:9000`（Moto 5.1.22，仅协议模拟） | 1 passed |
| `AE_INFRA_TEST=1`、`AE_S3_ENDPOINT=http://127.0.0.1:9002`（持久卷 MinIO） | 1 passed |

该用例实际执行：重复相同观测不产生重复快照、绑定 `(run_id, attempt)` 唯一冲突、快照 UPDATE 与 DELETE 被不可变触发器拒绝、跨运行快照与日志读取 404、无监控运行返回 `not_collected`、分页 `next_offset` 正确、最新投影返回受控日志引用与哈希。

Moto 结果只证明协议行为，持久化结论以 9002 端点为准；两者都不代表生产存储耐久性验收。

## 4. 固定容器无模型专项

准备方式：以当前受保护监控配置为基线，在固定容器内创建独立目录 `/tmp/autoeval-monitor-698ff2d-54a07dec-152f17b80b7f`（`cp -a`，不修改共享部署），写入更新后的补丁文件与运行时，并用 `git init` + 精确 `fetch` 补建 Git 元数据。实际核对：

- 适配器根 `HEAD = 955089ab32ef696c231d628f5ad0d9da28f430c4`。
- `benchmark` 根 `HEAD = 698ff2d1e027c07df37bdd158b62aace2a41ae3b`。
- 所有 pins 的 SHA-256 与锁文件一致。

执行（`AE_MONITOR_CONTAINER_TEST=1`，`AE_MONITOR_TEST_DEPLOYMENT` 指向上述独立根与 `runtime-1fae6fdc86ed.py`）：

```
4 passed in 97.95s
```

| 用例 | 覆盖 |
| --- | --- |
| `test_success_ssh_poll_and_duplicate_operations_execute_once` | 提交、重复提交不产生第二次执行、固定指纹 SSH 读取 health/tasks、作业进程树内 PID 校验、终态归档、bundle、清理、夹具调用计数为 1 |
| `test_port_conflict_and_changed_pin_block_before_fixture_execution` | 端口被监听时提交失败且未执行夹具；修改 pin 后提交被拒 |
| `test_cancellation_confirms_process_then_archives_and_cleans` | 取消确认后写出 `cancelled` 终态快照、打包并清理、作业转为 unknown |
| `test_patched_runner_loopback_ready_and_final_without_model` | 直接运行 patched `LocalRunner`：ready 端口写入、仅绑定 `127.0.0.1`、`AE_MONITOR_FINAL` 原子写出 |

期间修复的实际缺陷：预检与提交前的端口探测缺少 `SO_REUSEADDR`，连续运行会因上一轮连接残留误报端口占用；已修复。另有 `final` 钩子重复取快照、无计数时改写 `.evaluation` 后缀导致终态失真，已改为单次取快照并原子写入。

该专项只证明平台与固定补丁的无模型协议路径；不包含真实模型请求、真实评分或 Harbor 执行。

### 4.1 服务器容器复测（179.214.2.3）

经用户授权，使用服务器 `179.214.2.3`（用户 `chenluwen`）上的固定容器 `autoeval-ais-bench-llm-accuracy-sjtuyh-autoeval` 复测。主机指纹与受保护配置中的固定值一致；容器 ID 与配置一致；测试前端口 18480 空闲、固定根 `jobs` 为空，未发现进行中的监控作业。

在容器内新建独立根 `/tmp/autoeval-monitor-698ff2d-54a07dec-1fae6fdc86ed`（`cp -a` 现有根，未修改生产根），注入修复后的补丁、`runtime-1fae6fdc86ed.py`，并上传通过过滤克隆得到的 Git 元数据。实测：

- 适配器根 `HEAD = 955089ab32ef696c231d628f5ad0d9da28f430c4`。
- `benchmark` 根 `HEAD = 698ff2d1e027c07df37bdd158b62aace2a41ae3b`。
- 全部 pins 的 SHA-256 与锁文件一致。

```
4 passed in 97.54s
```

测试后：端口 18480 已释放，生产根 `local.py` 仍为旧校验值（未被修改），测试目录已清理。容器无外网，Git 元数据由本地上传而非容器内拉取。

服务器的 `AE_AISBENCH_MONITOR_CONFIG` 仍未指向该独立根与新运行时，因此真实测评预检会失败，直到运维显式更新配置。本轮未发起真实模型调用。

## 5. 前端回归状态

- `RunMonitor` 已增加独立加载态，仅在明确 `not_collected` 时显示“未采集”，并区分 `final` 与 `last_observed`。
- 新增 `backend/tests/browser_checks/test_browser_monitor.py` 与 `monitor.js` 模拟响应，覆盖加载态、2 秒轮询、页面隐藏暂停、过期保留、终态停止、快照与日志受控读取、资源标签。
- 受 Node.js 缺失限制，本轮未执行 typecheck、构建与浏览器检查；用例默认跳过，不计为通过。

## 6. 部署注意

- 生产/开发使用的 `AE_AISBENCH_MONITOR_CONFIG` 当前仍指向旧 pins 与旧运行时文件名；部署新版本前必须重新应用补丁、放置 `runtime-1fae6fdc86ed.py`，并更新该受保护配置，否则预检会明确失败（预期行为）。
- compose 新增 `monitored-worker`（`python -m autoeval.worker monitored`，`workers` profile），监控端口仍不发布到宿主。

## 7. 历史真实运行（非本轮结果）

- 运行 `ef00d534-67cd-417e-9940-37084c3be81e`、报告 `f04f4ce6-23e0-4d1d-ae19-06944586eae4`，5 次调用、0 失败，final 监控快照 `099f375fe4d4099178b24e7261f07159af0aa34e12b882946c9316f0d7d18d4a`，榜单记录 `2bf1ea81-655d-4b3c-b775-4a23e805b253`，准确率 0.0 且 `metric_conversion=not_verified`。
- 证据位于 `.screenshots/monitored-evaluation-recording/`，该目录被 `.gitignore` 忽略，未入库；截图中资源标签显示“本机 Docker”，与当前字段映射不同，说明录屏来自旧前端版本。
- 该运行属于修改前的历史证据，不能作为本轮回归或新版本验收结论；本轮未发起任何真实模型调用。

## 8. 仍未验收

- Harbor 真实测评与真实取消回收。
- 新版本下的真实 GPQA Lite 全流程录屏与交付核对。
- 前端 typecheck、生产构建与浏览器回归（等待 Node.js 22+ 环境）。
- 生产对象存储耐久性与正式 Release 门禁。
