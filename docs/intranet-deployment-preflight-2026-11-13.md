# 内网试用部署预检记录（2026-11-13）

## 目标与执行边界

目标为179.214.2.3，使用chenluwen账号，复用现有服务器，独立部署平台。通过现有受保护凭据及固定主机身份建立SSH连接。本轮只读检查，不运行模型、不改共享容器、不提交推送。

## 实测结果

| 检查 | 结果 |
| --- | --- |
| 系统架构 | Linux x86_64 |
| 账号权限 | chenluwen属于docker组，HOME可写 |
| CPU | 72逻辑CPU |
| 内存 | 总计约754GiB，检查时可用约727GiB |
| HOME所在文件系统 | 可用约3.2TiB，使用率27% |
| Docker Engine | 18.09.0 |
| Docker Compose插件 | 未安装，docker compose返回未知命令 |
| docker-compose独立命令 | 未找到 |
| 固定Compose下载 | GitHub v2.29.7请求12秒内未成功，HTTP状态000；不能据此推断所有网络均不可用 |
| 已有容器 | 既有AISBench、规划及其他执行环境持续运行，未停止或修改 |
| 目录 | 未创建部署目录，尚未上传密钥或数据 |

## 阻断与后续操作

预检阶段未通过。需要可获得且与目标Docker兼容的固定版本Compose工具，以及经过验证的镜像传输路径。若升级Docker，必须由服务器管理员评估共享容器影响并安排维护窗口；当前任务不擅自升级宿主机引擎。也可由本机准备离线Compose和镜像后传输，但必须先核实与18.09引擎的兼容性，不能直接把新版Compose可下载视为可部署。

未开始：生产式Temporal及隔离数据库、Nginx认证与TLS、发布镜像、有效数据迁移、无模型闭环、备份恢复和访问验收。未暂停源平台提交，未迁移Outbox或Temporal状态，未重放历史任务。

服务器有多个网络地址，未绑定任何新服务入口；后续先仅loopback监听，经受控SSH隧道验证，确认内网访问策略后才调整绑定。不得直接绑定所有网卡。

结论：仅服务器只读预检完成，部署未完成、迁移未完成、真实测评未验收。


## Compose兼容工具安装结果

经用户授权，已在用户目录安装docker-compose 1.29.2（独立Python 3.9虚拟环境）。命令路径 `/home/chenluwen/.local/bin/docker-compose`，环境路径 `/home/chenluwen/.local/share/autoeval-compose-1.29.2`。未升级Docker，未修改系统Python。安装依赖清单保存于远端环境的requirements.installed.txt。

验证：Compose版本1.29.2、Docker SDK 5.0.3；成功连接Docker Engine 18.09.0；pip check通过；Compose 3.7最小配置解析通过。未拉起测试容器，8个既有容器保持运行。使用docker-compose命令，而非docker compose插件命令。

该版本仅为现有旧引擎的兼容安装，不代表平台Compose发布包或生产基础设施验收通过。后续仍需生成兼容3.7的独立部署配置、准备固定镜像，并完成迁移与访问验收。此前Compose缺失阻断已解除；平台部署仍未完成。
