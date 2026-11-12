# 新版标准输入与统一输出接入

本轮固定标准仓 `4d7e3620bb9a32011155defda33094777a3cd3c6`、适配器 `955089ab32ef696c231d628f5ad0d9da28f430c4`。测试夹具直接来自这些提交：原有六组标准加 MiniMax 三组标准，另含上游 GPQA 与 DeepSWE 输出样例。来源样例只用于契约验证，不导入为平台真实报告。上游仓库均为 Apache-2.0，来源为 SJTUyh/AutoEval-Standard 与 SJTUyh/AutoEval-Benchmark-Adaptor。

## 已实现

- 新版编译默认不生成 `--no-template`；Docker、容器、挂载等字段移入 `runtime_config`。旧版参数与不可变编译历史保留，新编译可显式选择旧提交。不会将旧配置原地升级。
- `adaptor_bridge.invocation` 生成独立提交键、作业目录及固定参数的调用计划；仅允许显式指定本作业输出目录收集，拒绝其他目录。它不连接服务器、不执行命令；尚未接到任务提交入口。
- 统一输出 v1 解析校验工具、子命令、case 数量、冻结样本身份、attempt、状态与数值类型，拒绝重复尝试和未知格式。Harbor 缺少 dataset 时必须提供冻结的默认数据集。
- `archive_output` 对原文及逐尝试证据归档到对象存储；数据库记录索引、工具时间、token、诊断及原始分数来源。并发重复收集幂等，内容冲突拒绝；成功重试不覆盖旧失败。原文保留字节及 SHA-256。
- 平台 `created_at/finished_at` 是归档观测时间；`tool_metadata` 中的时间是工具报告值。缺失值保持空，时间异常仅作为诊断，不改变评分。token 合计包含当前收集的每次独立尝试，记录已知值与覆盖率；不跨多次收集叠加。
- 迁移 `0024_unified_outputs` 增加 case 元数据与不可变收集表，旧记录字段为 NULL。case 查询增加 `tool_metadata`；`GET /api/autoeval/v1/runs/{run_id}/unified-output` 返回最近一次收集摘要并重新验证原文证据，无收集返回 data=null。
- 任务 case 详情展示工具时间、耗时和 token；报告总览显示最近一次收集的 token 覆盖。原始报告、基线、榜单及门禁均不回写。上游分数保留为 `upstream_total_score`，`metric_conversion=not_verified`，不能当成已批准正式指标。
- Result Worker 注册 `collect_unified_output` 活动。活动只接受 run_id，从不可变快照读取 `unified_collection`，校验快照哈希、适配器版本与暂存对象归属，再归档；没有公开上传或直接运行适配器的前端接口。

冻结快照的内部收集契约：

```json
{
  "unified_collection": {
    "adaptor_revision": "955089ab32ef696c231d628f5ad0d9da28f430c4",
    "collection_key": "attempt-1",
    "artifact": {"object_key": "unified-staging/<run_id>/unified_result.json", "sha256": "<原文字节SHA-256>"},
    "expected_cases": {"GPQA_diamond": ["<冻结清单样本ID>"]},
    "default_dataset": "GPQA_diamond"
  }
}
```

该契约是未来执行桥接写入新运行快照的输入，**不能修改历史快照补入**。已有 GPQA 工作流没有此字段，也没有自动调度该收集活动；旧路径不会因此改变。真实 AISBench/数据集标识到平台 manifest 的映射尚需联调锁定，不能从实际输出反推必测清单。

## 开放边界与待完成项

远程提交、SFTP 原生产物打包、工作流收集活动调度、取消与清理的真实适配器闭环尚未接通，不能称为远程执行已验收。需先提供 `179.214.2.3` 的端口、用户、批准指纹、目录、容器及受保护凭据路径，核实 AISBench 改造提交，再批准模型、样本范围与预算。仅有适配器提交号不能替代运行环境版本校验。当前不新增可执行任务入口，执行保持关闭。

task-plan 本轮不实现，继续显示“暂无可靠估算”。真实 GPQA、MMMU-Pro 和 DeepSWE 分别联调，不能用样例解析通过替代评分正确性或实际资源验收。

## 验证

```powershell
cd backend
uv run alembic upgrade head
$env:AE_INFRA_TEST='1'
uv run pytest tests/test_unified_outputs.py tests/test_standard_inputs.py -q
$env:AE_BROWSER_TEST='1'
$env:AE_CHROME_PATH='<Chrome路径>'
uv run pytest tests/browser_checks/test_unified_output.py -q
```

数据库与存储专项使用真实 PostgreSQL/S3，保留 TEST-unified 运行和不可变归档。未调用模型；浏览器使用拦截夹具。部署后重启 API 和 Result Worker，新增模块才会加载。
