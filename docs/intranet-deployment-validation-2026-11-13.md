# 内网试用部署验收（2026-11-13）

## 结论

服务器内部部署及无模型验收通过，外部访问未开放、正式切换未完成、真实测评未验收。未在本轮执行git提交或推送。

## 发布与基础设施

独立项目autoeval-platform位于/home/chenluwen/autoeval-platform/releases/20261113。后台镜像sha256:6d3f094cf4b3f4b934cafd861505b1468bd124ddafeca95d99f0497d8b25f059，Python3.12.12；固定基础镜像及离线wheel校验通过。数据库、Temporal和MinIO均持久化。Docker18.09、Compose1.29.2不升级共享宿主引擎。

旧Docker桥接互连失败已通过专属network-anchor网络命名空间解决，无宿主Docker Socket挂载。唯一发布端口为127.0.0.1:18443。API、分发器、workflow、monitored、planning、planning-execution及reconciler运行；队列poller验证通过。

## 迁移

有效业务导出按外键及快照引用补齐依赖，在空目标导入后逐项检查外键闭包。保留7个真实运行及报告、27条case尝试、16份监控快照、30个标准输入版本、60个编译历史、132个非测试配置版本、4个Profile、5个清单、186条已批准榜单记录。旧Outbox、执行租约和规划容量租约均未迁移。

源数据库版本0029_analysis_manual_start，预迁移备份包含665个对象；目标迁移选择60个对象并回读SHA256全部通过。原报告5份verified、2份missing与源端一致，历史缺失未补造。模型服务凭据通过受保护文件迁移，不进入本记录。

## 验证证据

- HTTPS显式信任私有CA：未认证网页、模型与报告API返回401；认证后网页、模型、标准输入、报告及任务接口返回200。
- 无模型任务：99914ec0-8913-410e-b5b1-328ea43d93c6；工作流demo/run/99914ec0-8913-410e-b5b1-328ea43d93c6。
- 测试报告：38a0f4ef-b1bc-45cd-8834-e86e58278569；报告SHA256为2110dbc1a0137416fc1df01e7d404b0287b47cebfa6fcee2d3fd1c442b85f4d3；simulation、accuracy=null、2条case、清理cleaned。
- 重复请求返回同一任务与报告；整套服务重启后数据、HTTPS和证据状态保持一致。
- PostgreSQL单独重启后API自动恢复；待分发意图0、执行租约0、总报告8（7真实+1测试）。
- 远程AISBench无模型预检返回environment_ready=true、evaluation_ready=true；不代表真实评分验收。
- 数据库备份恢复至独立autoeval_restore_check_20261113：8报告、29case；SQL备份SHA256 ebcf1fd24a7b8caa0beaf099f86dd5d1b331c8465668863a514c8d1934260735。
- 65个对象恢复至独立检查桶并回读哈希通过。完整备份（含Temporal两个数据库和凭据）已生成于shared/backups/20261113T065314Z。
- Ruff通过；相关本地回归11项通过。默认依赖外部基础设施的跳过测试不计入远程验收。

## 尚未完成

SSH端口转发被管理员策略拒绝；浏览器跨电脑端到端验证和正式访问切换等待放行。crontab被拒绝，定时备份与健康任务未安装，手动脚本已验证。外部告警通知和异机备份位置未配置；源平台未停写，切换前须最终增量核对。真实GPQA小样本测试须另行授权，不在本轮调用模型。

详细操作、启动、恢复和回滚见deploy/intranet/README.md。保护目录保存原始验收输出与迁移清单，普通文档不含密码或密钥。


## 最终发布核验补充

- 当前后台镜像为R2，工具锁文件兼容路径修复后，远程AISBench环境预检通过。
- API增加Docker健康检查，调度队列poller均有消费者；临时fixture入口关闭、fixture Worker停止，业务服务不再挂载导入目录。
- 最新完整备份为shared/backups/20261113T070022Z，包含业务库、Temporal两个数据库、65个对象及受保护配置。服务器之外的本机保护副本完成后，其校验记录位于backend/.secrets/backups/server-trial-20261113T070022Z/download-verification.json。
- 定时任务未安装：chenluwen被服务器crontab策略拒绝。新增schedule.system-cron.example供管理员审核并安装到/etc/cron.d，按北京时间03:15备份、每5分钟健康检查；该模板未在服务器启用。
- 最终API及报告接口验证通过，7份真实报告、1份测试报告、0待派发意图、0测试报告入榜。保留外部访问、浏览器端到端、正式源端停写及最终增量核对为切换前置条件。

最终收尾：本机异机副本11个文件校验通过。最后同步发布文档时SSH握手/会话被远端关闭，远端最终文档同步及临时验收登录文件清理尚未确认；本机交付文件完整。未因此修改端口或SSH配置，待连接稳定后补同步。
