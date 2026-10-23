# Ubuntu 上的 GitHub 版本更新

状态：更新代码和本地边界测试已加入，完整 Ubuntu 安装、升级、恢复验收仍在进行。当前没有上传或发布本次试用版本，不能把本文当作已经完成部署的证明。

## 流程

发布者在独立环境完成构建和验收，生成固定版本的部署包，使用离线 Ed25519 私钥签名。GitHub Releases 保存部署包、`metahub-release.json` 和 `metahub-release.sig`。Ubuntu 宿主机每小时检查配置的仓库，验证预先安装的公钥、版本、文件大小、SHA-256 和包内每个文件，成功后才在平台工作台列出可升级版本。

平台负责人确认后提交升级请求。Ubuntu 的 root 工作进程串行处理请求：先构建新镜像并检查配置，再暂停入口和写入服务、加密备份、将备份恢复到独立数据库并逐表核对，通过后才向前迁移业务数据库、启动新版本并检查健康状态。备份或恢复核对失败时不迁移业务库，恢复原服务运行。镜像构建在暂停服务之前进行，实际暂停时间需要以最终验收记录为准。容器不挂载 Docker socket，网页不接收 shell 命令、下载地址或服务器路径。

版本不能重复安装或降级；同名版本的部署包不可替换。PostgreSQL 基础镜像变化也拒绝走这个升级入口，需要另行安排数据库升级。失败或中断的任务阻止后续升级，直到运维完成恢复。

单次升级的宿主机命令最长运行 7,200 秒。超时或收到终止信号时，工作进程先终止整个命令进程组，给退出处理 30 秒，再强制终止残留子进程。Docker 守护进程中的容器不属于这些子进程，因此还会执行 `metahubctl.sh quiesce-upgrade STATE`，停止同一 Compose 项目的一次性容器及入口、前端和后端写入服务；保留数据库、文件卷及容器供排查。清理最长 120 秒，清理失败会在任务状态中单独说明并继续阻断后续升级。同一批队列中前一个升级失败，也不会执行后续请求。systemd 给上述退出处理预留额外时间。此处的真实 Linux 进程及容器边界验收记录需与最终部署包一并核对，不能据此推断完整升级已经通过。

2026-10-15 已在独立 Ubuntu VM 上实际验证忽略 SIGTERM 的孙进程被停止、非零退出码被保留，以及 Docker 中两个写入服务和一个一次性容器停止后，另一项目的一次性容器仍保持运行。记录为 `output/deployment-ubuntu-20261014/update-interruption-linux-validation.json`。另有 14 项真实 root 文件权限及锁的队列检查通过，包含同批失败和超时清理顺序；该项替换了部署命令，记录为 `update-queue-interruption-linux-validation.json`。两项均未操作客户数据库，也不等同于整套升级和恢复演练。

宿主机工作进程使用已验证候选版本内的升级控制脚本，确保安装旧版本时也能执行新的备份和恢复核对保护。控制脚本通过 `current-release` 操作旧版本的服务与数据库，但备份、恢复检查仍使用同一候选版本的控制逻辑；缺少候选控制脚本时直接拒绝，不退回旧脚本。已有宿主机工作进程如果早于此保护，需要先由运维更新工作进程；仅上传新部署包不能修改正在运行的旧工作进程。

数据库摘要核对代码也由已验证的控制器提供，在匹配旧版本的镜像中使用原依赖和只读密钥挂载执行；不依赖旧镜像自带的核对工具是否完整。核对脚本从自身在镜像中的位置查找服务模块，不能依赖操作员当前所在目录。备份和隔离恢复使用同一份核对实现，完整冷恢复也使用当前可信控制器的实现，不为此修改旧镜像或业务行。

升级请求只接受有大小上限的普通文件，不跟随符号链接或读取管道。无效、已完成或已处理的请求会保留到服务器的 `updates/retained-requests/`（root 私有目录），退出待处理队列；损坏请求不会永久挡住后面的正常请求。已有 `completed`、`rejected` 或 `recovered` 记录不会被遗留请求覆盖。`running` 和 `failed` 状态仍会阻止后续升级，不能仅为清空队列而改成成功。状态文件采用独立临时文件、原子替换和持久化同步，旧的 `.pending` 文件不会阻止新的状态写入。

2026-10-15 的实际 Ubuntu 队列检查通过 12 项，使用真实 root 文件权限、符号链接、FIFO 和 `flock`；结果见 `output/deployment-ubuntu-20261014/update-queue-linux-validation.json`。这项检查替换了部署命令，未更新真实业务数据库，不能证明完整镜像升级、迁移或备份恢复已通过。

## 发布者准备

先完成该版本的验收。以下命令只准备发布文件，不会上传 GitHub：

```bash
python3 scripts/deploy/package-trial.py --version 2.0.0-trial.45 \
  --output release/metahub-2.0.0-trial.45-source.tar.gz
python3 scripts/deploy/sign-release.py --version 2.0.0-trial.45 \
  --bundle release/metahub-2.0.0-trial.45-source.tar.gz \
  --summary '已验收的试用版本' --key /offline/release-private.pem
```

签名私钥只由发布者保管，不放进 Git 仓库、服务器或部署包。首次生成：

```bash
umask 077
openssl genpkey -algorithm Ed25519 -out /offline/release-private.pem
openssl pkey -in /offline/release-private.pem -pubout -out release-public.pem
```

公钥通过独立可信渠道核对并安装，不能让更新程序从同一个待验证发布包获取信任根。经批准发布时，本轮 GitHub Release 标签为 `v2.0.0-trial.45`，附上上面三个文件。默认不执行仓库中的 Git hooks，也不在服务器上运行 `git pull`。本文未创建 GitHub Actions 或自动发布任务。

## 宿主机配置

完成首次正式安装后，`current-release` 指向当前源码包目录；该目录及其父目录必须 root 所有且不能被其他用户写入。生产 state 与源码目录分开。`prepare-production.py` 创建独立配置、密钥、升级队列和备份密钥。既有实例不得重新运行它覆盖 state。

首次准备须在已核对签名和文件摘要的解包目录中执行。配置的版本号读取该包的 `release-manifest.json`，构建标识为该清单的 SHA-256，与后续升级使用同一格式；不会固定为早期 trial 版本。缺少有效清单时在创建 state 或生成密钥之前拒绝执行。准备脚本读取清单不等于验证发布签名，签名和包内文件仍须先由可信发布校验流程核对。

安装信任公钥和配置示例，按实际 state 路径修改 `/etc/metahub/updater.json`：

```bash
sudo install -d -m 0755 /etc/metahub /opt/metahub-releases
sudo install -m 0444 release-public.pem /etc/metahub/release-public.pem
sudo install -m 0600 infra/production/updater.example.json /etc/metahub/updater.json
sudo install -m 0644 infra/production/metahub-update.{service,timer} /etc/systemd/system/
sudo install -m 0644 infra/production/metahub-release-sync.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now metahub-update.timer metahub-release-sync.timer
sudo systemctl start metahub-release-sync.service
```

systemd 示例假设初始工具目录为 `/opt/metahub`，实际不同则修改 ExecStart。仓库目前为 `leerabo/MetaHub`。试用预发布版本需显式 `include_prereleases: true`；正式环境可设为 false。本版下载器支持公开仓库，没有接收或保存 GitHub token；私有仓库支持尚未完成。网络需允许 GitHub API/Release 资源，以及镜像构建使用的官方镜像、Python 和 Debian 固定快照源。

## 配置和数据

GitHub 包不包含真实 `.env`、账号密码、KEY、数据库、文件卷或备份密钥。服务器 state 保留原有密钥，PostgreSQL 和文件卷独立于源码包。升级不会运行开发种子，也不会重置密码或启用固定验证码。已保存的镜像和源码版本不要在验收恢复前清理。

生产 PostgreSQL 数据库须使用 UTF-8 编码及 C/POSIX 排序和字符分类规则。新卷初始化已显式指定 `--encoding=UTF8 --locale=C`；它不会修改已有卷。Ubuntu 实测发现默认 `en_US.utf8` 会改变触发器清单的排序摘要，导致 0152 迁移拒绝执行。迁移入口因此在任何结构或迁移记录写入之前检查数据库规则，不兼容时返回 `db.collation_unsupported`。已有非二进制规则的数据库必须另做保留源库的迁移验证，不能改历史摘要或关闭漂移检查强行通过。

历史迁移 `0190_remove_personal_workspaces` 包含个人空间数据清理，不能对有客户个人空间数据的旧库直接执行。生产迁移入口现在会先只读检查：该迁移尚未应用且仍有个人空间或个人资料记录时，返回 `db.destructive_migration_blocked`，在写迁移记录或修改结构之前拒绝继续。必须另行制定保留这些数据的迁移方案；有备份不代表可以自动清理客户数据。已经应用的迁移文件及其校验和保持原样，此拦截也不能恢复过去已删除的数据。

## 失败恢复边界

迁移前的失败可以恢复原服务。迁移一旦开始，失败处理会停止新旧版本的入口和写入服务，不能假定旧代码兼容新数据库。

PostgreSQL 的 0194 逻辑身份修复属于只向前的修复。其 down 脚本会在同一事务中明确拒绝回退，保留修复后的触发器和 `applied` 迁移记录；不能只把记录标成已回退却保留修复函数，否则下一次正向迁移将因原函数摘要不匹配而失败。需要恢复旧版本时，使用与备份匹配的源码、配置、密钥及新数据库完成恢复。

用 `metahubctl.sh restore-check STATE BACKUP_PREFIX` 将加密备份恢复到新数据库并核实，保留故障数据库供排查。正式恢复还需核对对应版本、加密配置及文件卷，切换到恢复的新数据库后再启动兼容版本；禁止直接执行数据库 down migration 覆盖业务库。恢复和健康检查完成后，由运维将对应任务状态标记为 `recovered`，才重新放行后续任务。此恢复步骤尚待完整 Ubuntu 演练，不是自动数据库降级功能。

备份命令另生成加密的 `.verification.gpg` 数据摘要，纳入备份校验和清单。`restore-check` 必须逐表比对行数和全部行的 SHA-256 摘要，并核对列、约束、索引、函数、触发器、视图定义及序列状态；不再仅凭迁移记录数量宣布恢复通过。旧备份缺少该摘要时，此自动检查拒绝执行，需要单独进行人工恢复验收。2026-10-16 已在独立 Ubuntu Compose 环境实际执行 GPG 加密备份和隔离数据库恢复，核对通过 1,101 张表、4,408 行、结构和序列；升级流程中的重复备份恢复也通过。证据见 `output/deployment-ubuntu-20261014/backup-tool-entrypoint-20261016/`。配置、文件卷和数据库一起切换的完整冷恢复仍未完成，不能把隔离数据库核对当作完整部署恢复。

新备份的 `.sha256` 清单使用相对文件名，搬到其他服务器后仍可核对。`python3 scripts/deploy/verify-backup.py --prefix /恢复目录/备份前缀` 只读取该目录内同一前缀的四个指定加密文件；也支持旧清单中的绝对 POSIX 路径，但不会访问那些原机路径。缺失、重复、多余条目、内容变化及符号链接均拒绝通过，Linux FIFO 会立即拒绝。`restore-check` 使用同一校验器。校验和通过仅证明这四个文件与清单一致，完整恢复仍须解密、数据库逐表核对，并恢复匹配的配置及文件卷。备份加密密钥需另行安全保管；不要重新生成生产根密钥尝试恢复旧数据。

### 完整冷恢复命令（尚待实际 Ubuntu 验收）

工作区新加入 `recover` 命令，恢复四类加密备份及配置。下例是待验收操作流程，不能作为目前客户恢复已经可用的承诺：

```bash
sudo bash /可信工具目录/scripts/deploy/metahubctl.sh recover \
  /原部署state /备份目录/备份前缀 /全新恢复state /与备份版本完全一致的源码目录
```

原 state 必须仍可读取，且保有原备份加密密钥。新 state 必须不存在。此命令先校验四类制品、GPG 解密完整性和 tar 路径、条目类型及权限，再核对备份版本与源码版本；构建匹配镜像后才停止原服务。它创建新的 PostgreSQL、文件及 socket 卷，不覆盖原卷；数据库逐表、结构和序列摘要核对后，再核对恢复文件内容，保留原有加密根、数据库密码及 TLS 配置。

开始切换后，原 state 留下 `recovery-in-progress` 标记，阻止原升级工作进程和旧命令误操作新容器。恢复失败时保持入口及写入服务停止，保留两套 state 和卷供排查；不能直接把旧代码接回已迁移数据库。恢复成功后，需核实应用与账号，再将宿主机 updater 的 state 和 updates 路径改到新目录；旧升级队列不会自动重放。构建或配置检查失败时保留原服务运行，并清除明文数据库暂存。已经丢失原 state 的整机灾难恢复仍需先重建受保护的恢复种子目录，不适用此命令直接恢复。

新建数据库的就绪探针检查容器内 `127.0.0.1` TCP 端口，避免将官方镜像初始化期间的临时 Unix socket 服务误判为可恢复状态。恢复到匹配的旧版本时，新生成的恢复覆盖配置也使用此探针，不修改旧版本清单。恢复日志和 `recovery.json` 记录固定阶段名称，帮助定位数据库、文件、前置检查或服务启动的失败；不记录密钥或数据库行值。

安装、重启、升级及冷恢复依次等待 control-api、gateway 就绪，再启动 console 和 edge。每阶段保持 300 秒健康等待，不能将命令已提交或容器已经创建当成成功。重启会先停止入口及两个后端；若健康检查失败，入口和写入服务保持停止。此顺序已通过本地命令边界检查，真实 Ubuntu 完整恢复及 SaaS 升级状态以验收记录为准。

服务入口在导入 ASGI 应用之前，使用实际 PostgreSQL TLS 配置建立连接并执行只读 `SELECT 1`，仅该启动操作共享 30 秒预算；随后归还连接供应用启动阶段复用。正常请求的连接池等待配置不因此改变。连接失败时拒绝启动，仅输出固定阶段、异常类型和允许公开的数据库错误码，不输出 DSN、密码或异常详情。该修复源于独立 ARM64 Ubuntu 仿真环境中首次连接超过默认 3 秒的失败，不能据此宣称整体启动时间或原生服务器性能已经验收。

冷恢复收到 SIGINT/SIGTERM 后会停止当前宿主机命令的整个进程组；切换开始后另行停止 Docker 守护进程持有的一次性任务和写入服务，并清除明文数据库暂存。二次可捕获信号不会跳过这段清理。SIGKILL、断电或清理失败仍需运维检查，不承诺自动恢复运行。

目前只有本地归档校验和失败分支测试通过；真实 Ubuntu 全配置、文件卷、数据库恢复及完整流程中断恢复尚未通过，原始故障卷在这些验证完成前不得清理。

独立恢复数据库同样显式使用 `template0`、UTF-8 和 C 规则，避免继承宿主机默认模板的其他排序规则。私有 PostgreSQL 的 TLS 访问规则仅额外允许应用角色连接严格命名的 `restore_日期_时间_随机值` 校验库，不开放其他数据库或网络超级用户访问。

升级前创建的独立恢复数据库暂时保留，便于核查；它占用额外数据库空间，不能将生产库剩余空间全部分配给日常数据。2026-10-15 在 Windows 原生 PostgreSQL 的业务副本上，实际加密转储并恢复了 1,101 张表、72,565 行，结构、全部行摘要和序列状态一致；还验证了行变化、结构变化可以被识别，源副本保持不变。结果见 `output/deployment-ubuntu-20261014/backup-native-validation.json`。这项补充检查使用独立的内存测试密钥，不是可交付备份，也不替代 Ubuntu 的 GPG、Compose 和完整升级恢复演练。

Ubuntu 上另用替代 Docker/GPG 命令验证了升级的六个流程分支：备份失败、恢复核对失败时均未调用迁移，旧版本标记不变且恢复旧服务；迁移失败、健康检查失败时停止入口和写入服务，不启动旧代码；成功时先核对后迁移；旧版本控制脚本不支持新保护时，也由已验证候选版本完成控制流程。记录见 `output/deployment-ubuntu-20261014/upgrade-backup-gate-linux-validation.json`。这是命令调用顺序和失败处理检查，未执行真实数据库升级。

GitHub Release API 文档：<https://docs.github.com/en/rest/releases/releases>。
