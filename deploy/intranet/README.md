# AutoEval 内网试用发布

## 当前状态

已部署到179.214.2.3，项目名autoeval-platform。入口仅绑定127.0.0.1:18443。服务端部署、认证、有效数据迁移、无模型闭环、重启及恢复验证通过；外部访问和浏览器端到端验收等待管理员允许SSH端口转发。未开放公网。开发主机已完成本轮真实模型测评（见[规划到入榜真机验收](../docs/planning-live-acceptance-2026-11-13.md)），服务器本身未发起模型调用。

发布目录：`/home/chenluwen/autoeval-platform/releases/20261113-r3`，`/home/chenluwen/autoeval-platform/current` 指向该版本；上一版本 `releases/20261113` 保留用于回滚。Compose1.29.2，Docker18.09。后台使用离线wheel镜像构建，依赖文件SHA256SUMS已在构建时验证，镜像摘要见image-lock.json。安装为wheel后通过镜像内兼容链接提供工具锁文件。本次升级前备份位于 `releases/20261113/shared/backups/20261113T113720Z`，数据库迁移已由 `0029_analysis_manual_start` 升至 `0030_analysis_reanalysis`。

## 服务与网络

Compose3.7配置将各服务放在独立network-anchor容器的私有网络命名空间，以绕开本机旧Docker的容器间桥接限制，不修改宿主防火墙。仅anchor发布127.0.0.1:18443，禁止绑定所有网卡。PostgreSQL5432、Temporal数据库5433、API8000、Temporal7233、MinIO9000均不单独发布。

API、调度器、workflow/monitored/planning/planning-execution和reconciler已启动；付费分析默认未配置。临时fixture Worker已停止，配置已移除，普通业务容器不再挂载迁移导入目录。历史simulation报告不进入真实报告榜单。

## 操作

在服务器当前用户下执行：

```sh
cd ~/autoeval-platform/current
./trialctl.sh status
./trialctl.sh check
./trialctl.sh backup
./trialctl.sh stop
./trialctl.sh start
```

禁止`down -v`，禁止复用其他项目名操作既有测评容器。重建network-anchor时必须先停止整套平台，再重新启动；普通API/Worker和数据库可单独重启。启动后检查API健康、队列poller与证据完整性。

`shared`及本机`.secrets/intranet-20261113`包含凭据，不能提交或分享。账号密码在本机受保护deployment.json中，CA证书为ca.crt；不要跳过TLS校验。当前TLS证书90天有效，需提前更新；CA私钥仅保存在本机保护目录。

## 备份和恢复

trialctl backup生成业务库、Temporal业务及visibility数据库、对象包、对象哈希清单、受保护配置及镜像/Compose版本。备份目录权限700，文件默认600。证据备份只读源对象，旧证据缺失不修补。

每日备份与健康检查模板见schedule.cron.example。该账号被禁止使用crontab，**未安装任何定时任务**；目前需要手动备份，或管理员安装调度。备份包含密钥，须安全复制到独立介质。不得将服务器本机副本视为异机灾备。

恢复时停止调度和写入，使用空目标数据库与桶；先验证SHA256SUMS，再恢复业务和Temporal数据、对象及凭据引用。不要对正在使用的业务库执行检查库恢复命令。已验证的独立检查库和桶保留在服务器，不作为业务目录使用。

## 升级、回滚与切换

升级先备份并冻结新提交，在新release准备镜像和迁移，验收通过后更新current及受保护配置引用。回滚先停新分发器/Worker，核查新写入，再恢复上一版本；涉及不兼容数据库迁移时必须恢复配套数据库和对象，不能只回退镜像。

本机源平台尚未停写，当前服务器数据为已校验的迁移快照。管理员放行外部访问后，正式切换前需再核对源端增量、非终态任务和资源占用，避免用户两端同时提交。不得复制源Outbox和执行租约到新命名空间。


## 管理员交接

1. SSH放行仅本地转发至127.0.0.1:18443，不开放公网端口。放行后用自有客户端信任ca.crt并验证网页和API登录。
2. 审核schedule.system-cron.example后，可由管理员安装至/etc/cron.d/autoeval-platform，文件root:root、0644；任务以chenluwen身份运行。当前没有自动备份计划生效。
3. 当前试用使用共享团队账号，不提供个人审计。登录信息只在本机受保护deployment.json中交付，不写入文档或聊天。
4. 外部告警接收人、备份异机保留周期和正式切换维护窗口未配置；试用值守通过trialctl check/status及shared下日志进行。
