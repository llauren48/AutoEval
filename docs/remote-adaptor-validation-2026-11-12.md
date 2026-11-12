# 独立适配器部署与收集验证

日期：2026-11-12。用户明确授权直接连接服务器、绕过平台接入验证。该验证未建立平台批准指纹记录，不能作为正式 SSH 资源验收。

## 部署

- 服务器：179.214.2.3:22。
- 复用容器：autoeval-ais-bench-llm-accuracy-sjtuyh-autoeval。
- 新版源码提交：955089ab32ef696c231d628f5ad0d9da28f430c4。
- 独立目录：`/tmp/autoeval-validation-955089ab-de1c0857ff`。
- 原 AISBench 提交：698ff2d1e027c07df37bdd158b62aace2a41ae3b。
- 通过独立 PYTHONPATH 调用新版模块，无 pip 安装、无替换原适配器、无容器重建或重启。目录位于容器临时文件系统，容器重建后需重新部署。

## 收集验证

现有挂载目录 `/home/yanhe/runs/gpqa` 为空。因此在独立目录 `fixture-only/raw` 创建明确标记的两条测试产物，使用新版 `--collect-results --result-dir <独立测试产物目录> --output <独立输出路径>` 验证，不运行 benchmark、不预检模型、不产生模型调用。

结果：收集退出成功，schema_version=1，case_count=2。测试分数分别1和0，汇总0.5；输入 token 分别100、101，输出分别10、11；每条耗时2秒，开始结束时间均可提取。

输出：`/tmp/autoeval-validation-955089ab-de1c0857ff/fixture-only/unified_result.json`。

SHA-256：`0cc66888ddf18526acf6173567f516bd44d4114916e13e86fc6d93a20c56d5fe`。

这些数值均为人工测试夹具，未写入平台报告、基线或榜单。此次验证证明新版适配器可在现有 LLM 容器内收集指定格式的产物，不证明真实模型、AISBench 新增字段或 Harbor 测评闭环已通过。真实历史产物重取及 Harbor 独立部署尚未验证。
