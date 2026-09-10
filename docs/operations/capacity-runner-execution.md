# 隔离容量执行器部署

当前为待整批验收的实现，尚未接入 Control 持久轮询。不要对正在使用的试用服务启用。
部署入口是基础 Compose 加 `infra/compose/docker-compose.capacity-execution.yml`，不是独立 Compose。

## 配置责任

管理员显式设置 `METAHUB_CAPACITY_CONFIGURATION_DIRECTORY`，该目录以只读方式挂载到
`/run/metahub-capacity`。目录必须已经存在，不自动创建。内部 Bearer 和签名密钥沿用基础 Compose
的两个独立环境配置；合成用户凭据和资源探针凭据只放在受限文件，不填写到 API 请求里。

`settings.json` 为严格 JSON，顶层包含：

- `state_directory`: 必须设为 `/var/lib/metahub-capacity`，使用独立命名卷。
- `max_queued_runs`: 1～100，默认 20。
- `targets`: 每个 service/environment/region 一项，不可重复。

每个 target 包含 `service_code`（gateway/control/status）、`environment_class`、`region_code`、
`origin`、`resource_origin`、`resource_credential_file`、`identities` 和请求上限。
两个 origin 必须是显式私网/环回 IP 与端口，不允许 DNS、路径、查询参数、用户信息或重定向。
Gateway 还必须设置 `model_name`。身份数组元素是 `subject_code` 和 `credential_file`，
文件路径必须是容器内绝对路径。subject_code 以 synthetic- 开头，不同主体不能复用凭据。
公共状态读场景可以不设置主体凭据，但资源探针始终需要独立凭据。

测试目标及其探针需要另行部署到内部 `capacity-test-targets` 网络，分配固定 IP。
不要把该网络连接到正在使用的试用数据库或 Gateway。此叠加配置不开放主机端口，
不加入 Docker socket，不授予宿主机 PID 或特权访问。卷目录由镜像初始化给 UID/GID 65532。

## 资源探针

`metahub_capacity_runner.probe:app` 在目标进程可观测的隔离环境启动，配置
`METAHUB_CAPACITY_PROBE_TOKEN_FILE`、`METAHUB_CAPACITY_PROBE_TARGET_PID` 和
`METAHUB_CAPACITY_PROBE_TARGET_CREATED_AT`。进程代次不一致或无法观测时返回 503。
探针不应为了观测扩展到宿主机全局特权；集群级指标需要后续独立适配。

## 执行语义

`/health` 仅报告 API 存活。`/ready` 只有已配置且工作线程存活才返回 200，
不会为健康检查自动调用目标服务。提交采用固定对象和摘要幂等，202 表示未完成。
状态和取消都要求新鲜的同对象签名，不保存签名到任务日志。

授权过期是最晚启动限制。已经开始的执行按其固定 duration 完成或被取消，
排队到期则持久失败；重签不能复活它。容器重启将遗留执行标为 interrupted，
不会自动再次施压。状态卷不要作为临时缓存清空，否则会丢失停止与幂等事实。
