# 首次安装的管理员初始化

全新 Ubuntu 部署先执行 `metahubctl.sh install STATE`，迁移数据库并启动服务，再在服务器的交互式终端执行：

```bash
sudo scripts/deploy/metahubctl.sh first-owner /opt/metahub-state
```

命令依次询问邮箱、显示名称、新密码及确认密码。密码至少 14 个字符，没有默认值。将终端显示的随机密钥添加到自己的动态验证码应用，输入当前真实验证码；密码、MFA 密钥、角色和初始化审计记录在同一 PostgreSQL 事务中写入。成功后等待下一枚验证码，再从 HTTPS 登录页登录。命令使用 `none` 日志驱动，密码通过隐藏输入读取，MFA 注册材料只显示在交互终端。不要录制该终端，也不要把输出重定向到文件。

这只用于全新、空的生产数据库。已有账号、平台操作员、租户或旧用户时会拒绝执行，不能用于迁移后的提权、重设已有密码或替换 MFA。若写入过程中失败，整个事务回滚；成功后关闭初始化状态，后续调用拒绝执行。生产 HTTP 初始化接口保持关闭，也不会启用 `000000` 或开发绕过。

初始化凭服务器运维权限完成，并不证明邮箱已收到验证邮件，系统不会伪造邮箱验证记录。试用模板默认关闭邮件发送。已加入经过证书校验、TLS 加密和账号认证的 SMTP 适配；自助注册、邮件找回密码和邮件邀请仍需针对实际部署验收。首位管理员命令不能替代这些功能，也不会自动授予创建组织所需的邮箱验证记录。

## 配置验证码邮件

在服务器 root 私有的 `STATE/backend/runtime.env` 中配置自己的邮件中继，不把凭据写进部署包、GitHub 或网页：

```dotenv
METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE=smtp
METAHUB_SMTP_HOST=smtp.example.com
METAHUB_SMTP_PORT=587
METAHUB_SMTP_SECURITY=starttls
METAHUB_SMTP_USERNAME=YOUR_RELAY_ACCOUNT
METAHUB_SMTP_PASSWORD=YOUR_RELAY_PASSWORD
METAHUB_SMTP_SENDER=security@example.com
METAHUB_SMTP_TIMEOUT_SECONDS=10
```

以上是占位示例，不能直接作为有效配置。中继使用隐式 TLS 时，将 security 改为 `tls`，并填入中继指定的端口（通常为 465）。仅支持这两种加密模式，禁止明文降级；证书不可信、认证失败或收件人被拒绝时发送失败，不改用测试捕获或固定验证码。自建中继需要正确安装可信 CA，不能关闭证书校验。发送地址还需满足中继授权和域名发信配置。

修改后运行生产 preflight，再重启控制服务并实际验证收信。没有邮件服务时保持 `disabled`，不要将“服务能启动”当作注册、找回密码已经可用。SMTP 用户名、密码从配置对象的输出中排除，配置摘要中的用户名也隐藏；邮件正文不包含内部 intent/challenge 标识。

2026-10-15 的 30 项投递及生产配置测试通过。另用真实本机 SMTP 服务器验证了 STARTTLS 和隐式 TLS：不可信证书在发送凭据前拒绝，受信任证书下完成认证、发送并核对实际邮件正文，验证码随机生成。结果见 `output/deployment-ubuntu-20261014/smtp-tls-validation.json`。邮件只保存在独立测试进程内存中，没有外发；该结果不证明客户邮件中继或 Ubuntu 注册流程已经通过。

同日另在独立原生 PostgreSQL 数据库完成 191 项迁移后，以 `prod` 身份规则通过 10 项实际 HTTP 请求检查：图片验证码由视觉读取，邮件通过真实本机 STARTTLS 和认证投递，错误及重复邮箱验证码拒绝，验证后成功创建组织并切换空间，数据库中存在对应邮箱验证记录。结果见 `output/deployment-ubuntu-20261014/registration-native-validation.json`。HTTP 使用 ASGI 传输，数据库连接为本机测试连接；没有注入验证记录、应用开发种子、使用固定验证码或修改原有业务库。该补充检查仍不能替代 Ubuntu HTTPS 和逐页浏览器验收。

当前验证：2026-10-15 在真实 Windows 原生 PostgreSQL 的 191 项迁移后，已验证首次初始化、Argon2 密码验证、加密 MFA、真实验证码消耗、重复执行拒绝和末尾写入故障的完整回滚；MFA 服务的数据库能力检查也已通过 PostgreSQL 实测。这是补充验证；Ubuntu 上的交互初始化和随后浏览器登录仍需完成，不能将本页当作已完成 Ubuntu 部署的证明。结果保存在 `output/deployment-ubuntu-20261014/first-owner-native-validation.json`。

另一次独立 PostgreSQL 数据库中的 23 项正常 HTTP 路由检查已通过：错误密码拒绝、正确密码登录、Secure/HttpOnly Cookie、CSRF 拒绝、真实 TOTP 平台登录、平台目录和审计读取、重复验证码拒绝、五次失败后的设备锁定、退出后会话失效。检查修复了 MFA 失败计数使用 SQLite 标量 `MIN(a,b)` 导致 PostgreSQL 返回 503 的问题；相关计数现使用两种数据库均支持的条件表达式。结果见 `output/deployment-ubuntu-20261014/identity-native-validation.json`。该检查使用 ASGI 传输和 `test` 环境，未应用开发种子、未使用固定验证码；它不证明 Ubuntu HTTPS 网络或浏览器视觉验收通过。
