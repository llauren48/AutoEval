# 平台人工产物归档流程验证

## 结论

无模型归档路径已实测通过：开发工作台选择标准配置 → HTTP提交 → PostgreSQL运行及不可变快照 → Outbox → Temporal独立工作流 → 已有容器产物复制 → SFTP收集 → S3上传与回读 → case入库 → 测试报告 → 原报告详情展示 → 临时目录清理。

这是已有人工产物的归档闭环，**不是远程模型测评执行闭环**。没有运行 benchmark、没有调用模型，不代表真实GPQA、MMMU-Pro或DeepSWE验收通过。

## 可核对证据

| 项目 | 实测标识 |
| --- | --- |
| 运行ID | `250a8316-cb9e-40c7-9700-76950f514080` |
| 工作流ID | `demo/run/250a8316-cb9e-40c7-9700-76950f514080` |
| 报告ID | `0bb645cf-0d1c-4158-a593-f8e2ba196824` |
| 快照SHA-256 | `f66a5ed8cd242ba09a6603b2fb7655b847f8f735cc4f276b55bd9482157b2a3c` |
| 产物SHA-256 | `0cc66888ddf18526acf6173567f516bd44d4114916e13e86fc6d93a20c56d5fe` |
| 报告SHA-256 | `9bfd2427cc54c466ecb73479e517e8483e3393fef2b2fc8f6e117e10474dba4b` |
| case数量 | 2 |
| Outbox状态 | delivered |
| 清理状态 | cleaned |
| 来源 | simulation，accuracy=null |
| 回读 | 原始产物、报告、两份case证据均校验通过 |

对象存储使用当前后台配置的开发S3兼容服务，不是新配置的云OSS。对象Key：

```text
unified-staging/250a8316-cb9e-40c7-9700-76950f514080/0cc66888ddf18526acf6173567f516bd44d4114916e13e86fc6d93a20c56d5fe.json
reports/250a8316-cb9e-40c7-9700-76950f514080/9bfd2427cc54c466ecb73479e517e8483e3393fef2b2fc8f6e117e10474dba4b.json
```

运行仅处理已预置的验证产物，未删除原文件或容器。临时目录在固定SSH所有者根目录的 jobs/<run_id> 下，SFTP读取后删除；保留所有者标记及共享运行时。主机指纹来自此前用户授权的直接连接观测，后续连接固定该值；这不替代管理员批准的正式资源身份验收。

## 入口与部署

- 数据工作台 → 环境诊断 → 平台归档流程验证。仅开发环境提供，普通控制台没有人工产物提交入口。
- `POST /api/autoeval/v1/diagnostics/unified-fixture`，请求仅含现有标准配置ID，必须携带Idempotency-Key。远程地址、凭据引用、来源文件及预期样本均从后台受保护配置冻结，不接收前端任意路径或命令。
- 后台本地 `.env` 配置 `AE_UNIFIED_FIXTURE_CONFIG=.secrets/unified-fixture.json`。配置和凭据不提交Git，不写快照明文密钥。
- 运行 `uv run alembic upgrade head`，当前为0025；重启API和Outbox投递器，并运行 `uv run python -m autoeval.worker fixture`。独立队列 `demo.fixture.workflow.v1` 不改变既有在途工作流。
- 新增不可变 `unified_output_artifact`，运行后对象地址和哈希追加存储，不回填不可变输入快照。旧收集活动改为读取该产物记录。
- 测试报告仅在开发环境按具体ID访问，普通真实报告目录、有效榜单及正式门禁不接纳它；页面显式标记人工测试产物，正式精度为空，分析未启用。

## 验证结果与限制

- 后端专项43通过、1跳过：标准输入、统一输出、真实PostgreSQL/S3、重复提交、取消、上传失败后恢复及重复归档。跳过项为需额外本地适配器源码的专项，不计为通过。
- 前端真实提交至报告的浏览器专项1通过；原控制台预检、报告图表和统一输出展示3项回归通过。真实提交测试必须显式设置AE_FIXTURE_PIPELINE_TEST=1，默认不创建任务。
- TypeScript、Vite生产构建和本次改动Ruff检查通过。截图及机器可读证据保留在忽略目录 `.screenshots/browser-checks/`，不包含凭据。
- 取消在阶段边界确认；本路径没有模型进程，不证明长时推理取消、未知提交查证、服务中断恢复及清理失败补偿已验收。
- 尚未提供通用远程测评作业执行器、正式资源登记与容量占用、AISBench真实评分复现或完整原生产物打包。这些仍属于后续真实测评闭环，不能用本次两个case测试替代。
