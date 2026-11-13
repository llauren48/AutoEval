# GPQA SSH 预检修复（2026-11-13）

SSH 资源 `51278e81-b71b-4425-b89f-fd90ce287402`，服务器 179.214.2.3，用户 chenluwen。

新建隔离容器 `autoeval-gpqa-525ed17-repair`（c90eba26872a），未覆盖原有测评容器。仅在保护配置 planning-containers.json 中更新该资源的完整容器及镜像身份，保留切换前备份；凭据及配置不提交。容器设为 unless-stopped；当前变更保存在容器和其专属卷，删除重建容器需要重新部署，不宣称镜像已固化这些修改。

- AISBench：525ed1779f50a8ac068b8638771f633a151e769f，Git 原始提交通过 bundle 传输；所有锁定源码文件哈希及 Python 文件集合检查通过。
- Adaptor：955089ab32ef696c231d628f5ad0d9da28f430c4，从固定 Git 提交构建并安装到隔离依赖目录；direct_url.json 的 vcs_info 校验通过，未伪造版本元数据。
- Planner：0ee9abf49a57c5accc62df8900693cdcea88ea9a，由既有内容哈希运行副本提供。
- 数据：原生加载完整 GPQA Diamond CSV，198 个样本 ID 及哈希逐项与既有冻结全量清单一致。
- 评分：人工答案 A/B 与参考 A/C 的原生评分为 50%，逐项 true/false；仅验证原生评分行为，不作为模型成绩。未更改评分文件或规则。
- 环境仅声明 GPQA 可用；不声明 MMMU-Pro 或 DeepSWE 已验证。

最终真实 SSH 容器预检作业 `14b21cd1-fa19-41b0-a84a-883c30803866`：completed，execution_ready=true，blocking_reasons=[]，GPQA 检查项为空，Planner/Adaptor/AISBench 均与所选固定版本一致。本次没有模型推理、吞吐探测或真实测评提交；预检通过不代表该新容器完整真实推理链路已验收。

机器可读结果保存在开发目录 `.screenshots/preflight-repair/final-preflight.json`。现有 UI 若展示旧结果，需对原配置重新执行预检。
