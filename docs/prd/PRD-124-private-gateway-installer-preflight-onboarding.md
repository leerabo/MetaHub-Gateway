# PRD-124 云管私有 Gateway 安装包、预检与接入向导

状态：设计冻结，尚未编码；计划迁移 0116。依赖 PRD-120/121/123 和 PRD-028，范围以 Excel PRD-124 为准。

## Excel 权威合同

- CLI：`metahub preflight`、`metahub install`、`metahub connect`、`metahub uninstall`；API：`GET /edge/installers`。
- 对象：`InstallerRelease`、`InstallProfile`、`PreflightReport`、`Installation`、`InstallAudit`。
- 事件：`edge.install_started`、`edge.install_completed`、`edge.install_failed`。
- SLO：标准环境安装 < 20 分钟；预检 < 2 分钟；失败清理可验证。

## 1. 产品闭环

release_manager 经双人审批发布不可变签名 Compose/容器/CLI manifest；tenant owner 选择 Deployment、系统 profile 和 release，下载不含客户 Secret 的引导。CLI 先离线验签，再检查 Linux/arch、CPU、内存、磁盘、端口、DNS、时间、代理、CA、文件系统和容器 runtime；任一必需项失败不写系统状态。安装使用 staging 目录和持久 step journal，完成服务、PRD-120 注册、PRD-121 初始快照和健康探测后才原子标记 active。卸载明确选择保留配置/审计/本地 Secret，默认保留客户数据。

## 2. 0116 数据与制品

- InstallerRelease/Artifact 不可变，包含 content hash、SBOM hash、签名用途、支持 OS/arch、schema min/max、N/N-1 兼容和发布审批。
- InstallProfile 仅包含非敏感资源/网络要求；PreflightReport 上传固定 check/outcome 摘要，不上传主机名、IP、路径、环境变量或证书正文。
- Installation/InstallAudit 保存 step、generation、receipt hash、补偿状态和时间；CLI 重启按 journal 幂等继续或回退。
- 在线和离线介质从同一 canonical manifest 生成，构建可复现；签名不匹配、未知 artifact、版本不兼容立即失败关闭。

## 3. 权限与体验

tenant owner 只下载本 Workspace 引导；release_manager 发布平台制品但看不到注册 token/Secret；本地 root 操作必须交互确认并写本地不可变审计。Console 是紧凑步骤流：环境选择、预检结果、安装状态、注册/健康结果和恢复动作；不在页面展示 shell 命令中的凭据，注册 token 通过安全 stdin/文件通道交给 CLI。

## 4. 第一轮自审：供应链与半安装

- P0：只校验容器 digest、不校验完整 manifest/Compose/CLI 会被替换。修正为根 manifest 覆盖所有 artifact、SBOM、profile 和签名用途。
- P0：边检查边写系统会留下半安装。修正为 preflight 纯读，install staging + journal，commit 前可完整补偿。
- P0：安装包预置租户 token/Secret 会形成共享凭据。修正为制品完全 tenant-neutral，引导信息另行短时获取。
- P1：代理/CA 诊断上传原值会泄漏内网。修正为只上传固定 check code/outcome。

## 5. 第二轮自审：恢复与卸载

- P0：崩溃重试重复注册第二设备。修正为 installation generation、PRD-120 intent/CSR 幂等键和 step receipt。
- P0：卸载默认删除 Secret/审计会造成不可恢复损失。修正为默认保留，显式 step-up 和逐类确认后才能清除。
- P1：离线介质与在线 release 不可复现会难以验收。修正为相同 manifest/hash/SBOM，构建时间等非确定字段移出签名内容。
- P1：版本不兼容靠启动后发现会扩大故障。修正为 preflight 校验 schema、Agent、Control 和 N/N-1 矩阵。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0116 迁移；签名/SBOM/digest/版本/代理/CA/DNS/时间故障；preflight 零写入；各 step 崩溃恢复与补偿；重复 connect 不克隆身份；卸载保留/清除矩阵；全新 Linux 容器验收和离线介质；Console 三浏览器；20 分钟/2 分钟 SLO；制品、命令行、日志、报告敏感扫描及静止终态。
