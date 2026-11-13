# 内网部署实施记录（2026-11-13）

## 已完成

- 远端建立独立目录 /home/chenluwen/autoeval-platform/releases 与 shared，shared权限700。
- 安装并验证Compose 1.29.2，保留Docker 18.09与既有容器。
- 新增deploy/intranet部署骨架：Compose3.7基础服务、独立Worker叠加文件、全路径认证TLS网关、镜像摘要清单。网关仅发布127.0.0.1:18443，无宿主Docker Socket。
- Nginx1.28.0、PostgreSQL16.6、Temporal1.27.2镜像已拉取且临时容器版本检查通过，未启动长期平台服务。
- 本机预迁移备份完成：backend/.secrets/backups/intranet-preparation-20261113，迁移版本0029_analysis_manual_start，665个对象，备份清单SHA256 a8d10609b0fc779c83e8ff6a731d140b2cc7e19e5798ee56efb788fcd2bdcf6e。源服务仍在线，该备份不是最终切换一致性点。
- 发布源码包139个文件，上传远端releases/source-20261113.tar.gz并校验SHA256 40b87c177b55109136fd8ca02bbbd6de947164e2a05ebed17f3a43d88fce411d。包未包含秘密和源数据库。

## 当前阻断

后台基础镜像docker.m.daocloud.io/library/python:3.12.12-slim-bookworm连续两次拉取出现TLS握手超时（registry及token端点）。未取得镜像摘要，未开始后台构建。需要恢复镜像访问或采用可信离线镜像传输，不绕过TLS验证。

## 未完成

持久存储镜像最终锁定、后台镜像构建、Compose完整配置验证、TLS及认证配置、有效数据关联闭包迁移、凭据迁移、独立Temporal命名空间、无模型闭环、访问控制与恢复验收。部署骨架必须完成上述验证后才能启用，不能直接作为已验收发布包。

源库非终态核查发现1条preparing诊断记录，未发现普通测评非终态；正式切换前需重新核查工作流和资源占用。未停止源提交、未向远端恢复数据库、未启动新分发器或Worker、未调用模型、未提交推送。

结论：部署准备部分完成，平台尚未上线，入口尚未开放。
