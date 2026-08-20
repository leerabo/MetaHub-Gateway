# MetaHub 产品级 QA 详细计划

本计划是 PRD 专项测试之外的产品验收层。所有项目按组并行设计和执行；只有实际执行器
返回成功并产生脱敏证据时才记为 PASS。静态定义、未安装浏览器和未达到规定时长均不得
替代真实执行。`all_108_prds_complete` 始终保持 `false`。

执行器：`B` 后端隔离 pytest；`P` 性能/压力 runner；`W` Node 产品合同；`R` 隔离真实浏览器；
`L` 四小时长稳；`C` 生产 CSV 净化白盒与下载合同。

## WB 白盒测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| WB-01 | 服务边界与路由装配 | Control/Gateway 职责和路由无越界依赖 | B |
| WB-02 | 租户字段覆盖 | 核心业务表均有 `tenant_id` | B |
| WB-03 | 租户查询约束 | 资源读写由认证上下文限定租户 | B |
| WB-04 | 后端 RBAC | 无权限角色直接请求仍被拒绝 | B |
| WB-05 | 统一错误中间件 | code/request_id/trace_id 稳定且无堆栈 | B |
| WB-06 | SQLite 请求连接状态 | 单请求复用；请求结束物理关闭；下一请求恢复外键、query_only、busy_timeout | B/P |
| WB-07 | 日志脱敏路径 | 密码、Key、Token、Prompt、Response 不进入日志 | B |
| WB-08 | 导出净化 | UTF-8、BOM、CRLF、公式单元格防护 | B/C |

## API 接口测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| API-01 | 未认证请求 | 401 与统一错误结构、保留客户端 request_id | B |
| API-02 | 无权限请求 | 403 由后端产生，不依赖前端按钮 | B |
| API-03 | 非法 JSON/字段 | 422/400 使用稳定业务错误码 | B |
| API-04 | 不存在资源 | 同租户不存在与越权探测不泄漏差异 | B |
| API-05 | 跨租户 payload | `tenant_id` 注入或篡改被拒绝 | B |
| API-06 | 分页筛选边界 | limit/offset/未知筛选失败关闭 | B/W |
| API-07 | 503 与恢复 | 错误含 request_id，恢复后请求成功 | R |
| API-08 | CSV 下载合同 | content-type、nosniff、文件名和编码安全 | B/C/W |

## BB 黑盒功能测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| BB-01 | 正确登录 | 进入有权限的产品首页 | R |
| BB-02 | 错误登录 | 友好错误、无凭据回显 | B/R |
| BB-03 | 租户隔离 | 跨租户读写无法成功 | B |
| BB-04 | 管理员导航 | Provider、Usage 等主要入口可达 | R |
| BB-05 | 开发者导航 | Project、Virtual Key 入口可达 | R |
| BB-06 | Viewer 只读 | 可读页面可用，写动作不可执行 | B/R |
| BB-07 | 侧栏折叠 | 展开/折叠状态真实变化且布局稳定 | W/R |
| BB-08 | 筛选与清空 | 表单提交、重置和结果状态一致 | W/R |
| BB-09 | 空数据 | 空态给出产品化下一步 | W/R |
| BB-10 | 加载状态 | 请求期间不出现空白或布局跳变 | W/R |
| BB-11 | 服务异常 | 503 显示安全信息和 request_id | R |
| BB-12 | 服务恢复 | 用户重试/刷新后恢复可用 | R |
| BB-13 | 退出登录 | 会话销毁并返回登录页 | R |
| BB-14 | 下载导出 | CSV 可下载且不含敏感字段 | C/W |

## GR 灰盒测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| GR-01 | UI 与 API 权限对照 | UI 隐藏不替代 API 拒绝 | B/W |
| GR-02 | request_id 贯通 | 页面错误与 API 响应可关联 | B/R |
| GR-03 | 运行时角色变更 | 后端立即按新权限拒绝 | B |
| GR-04 | 取消未决读取 | 导航/退出后旧响应不覆盖新页面 | W/R |
| GR-05 | 请求内连接复用 | 鉴权与业务读取复用同一句柄，连接本地 PRAGMA 不污染下一请求 | B/P |
| GR-06 | 数据库迁移后运行 | 空库迁移后 API 和浏览器流程可用 | B/P/R |
| GR-07 | 故障注入 | 单接口失败局部降级而非整页失效 | R |
| GR-08 | 失败日志观察 | 私密输入不出现在响应或捕获日志 | B |
| GR-09 | 导出链路观察 | 只记录摘要，不记录 CSV 正文 | B/C |

## PERF 性能与压力测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| PERF-01 | 隔离 ASGI 并发健康 | 24 并发全部成功 | P |
| PERF-02 | 登录和首屏观察 | 无 6-9 秒本地阻塞回归 | R |
| PERF-03 | 回环基础采样 | 30 请求无错误并记录分位数 | P |
| PERF-04 | Control API 延迟 | 健康接口 p95 在本机基线内 | P |
| PERF-05 | Gateway 延迟 | 健康接口 p95 在本机基线内 | P |
| PERF-06 | 有界压力 | 120 请求/并发 8 全部 2xx | P |
| PERF-07 | 压力后恢复 | 压力结束后健康检查仍成功 | P |
| PERF-08 | 长稳 | 连续 14,400 秒无 5xx/传输错误 | L |
| PERF-09 | SQLite 健康 | integrity ok、外键违规 0、请求句柄复用和关闭正常 | P |
| PERF-10 | 证据内存与脱敏 | 仅聚合事实，无正文、URL、凭据 | P/L |

## WEB Web 自动化测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| WEB-01 | 登录控件语义 | label/autocomplete/type 正确 | W/R |
| WEB-02 | 键盘操作 | Tab、Enter、焦点状态可用 | W/R |
| WEB-03 | 鼠标真实点击 | 坐标移动、down/up 完成命令 | R |
| WEB-04 | 导航切换 | 主标题和内容随入口稳定变化 | R |
| WEB-05 | 响应式布局 | 桌面与移动无横向溢出 | W/R |
| WEB-06 | 浏览器控制台 | 非预期 console/page error 为 0 | R |
| WEB-07 | 网络请求 | 非预期 request failure 为 0 | R |
| WEB-08 | 会话清理 | 退出后敏感状态和未决请求被清理 | W/R |

## UI 界面元素测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| UI-01 | 侧栏控件 | aria-controls/expanded 与视觉状态一致 | W/R |
| UI-02 | 产品语言 | 普通 UI 不出现 PRD/迁移/表名/源码路径 | W/R |
| UI-03 | 敏感输入 | 密码遮罩，Key/Token 不二次渲染 | W/R |
| UI-04 | 状态组件 | loading/empty/error/denied/retry 明确 | W/R |
| UI-05 | 按钮与表单 | 文案不截断、禁用/忙碌状态稳定 | W/R |
| UI-06 | 固定布局 | 折叠宽度、主区 min-width、移动布局稳定 | W/R |
| UI-07 | 安全错误展示 | 仅 code/message/request_id，无原始正文 | B/W/R |

## UCD 用户中心设计测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| UCD-01 | 首次管理员 | 无研发知识也可登录并找到 Provider | R |
| UCD-02 | 开发者任务 | 可找到 Project 和 Virtual Key | R |
| UCD-03 | Viewer 任务 | 只读能力清楚、无误导写入口 | B/R |
| UCD-04 | 错误恢复 | 文案说明下一步并能恢复 | R |
| UCD-05 | 术语可理解性 | 不以内部实现词作为主要说明 | W/R |
| UCD-06 | 导航信息密度 | 长侧栏可折叠且分组可扫描 | W/R |
| UCD-07 | 移动任务 | 390x844 下关键控件可达 | R |
| UCD-08 | 中英文内容 | 中英文长文本不重叠、不溢出 | W/R |
| UCD-09 | 支持定位 | 错误 request_id 可见但不泄密 | B/R |

## COMP 兼容性测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| COMP-01 | Chromium | 三视口真实流程通过 | R |
| COMP-02 | Edge | 三视口真实流程通过 | R |
| COMP-03 | Firefox | 三视口真实流程通过，不允许 skipped 代替 | R |
| COMP-04 | 显示缩放 | 100%/125%/150% 控件可达且无溢出 | R |
| COMP-05 | 语言与长文本 | 中文/英文容器不重叠 | W/R |
| COMP-06 | 网络故障 | 503、恢复、重试跨浏览器合同一致 | R |
| COMP-07 | Excel CSV | BOM/UTF-8/CRLF/公式防护正确 | B/C/W |

## SEC 安全测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| SEC-01 | 租户越权 | payload/query/path 均不能改目标租户 | B |
| SEC-02 | 权限绕过 | 直接 API 调用不能绕过 RBAC | B |
| SEC-03 | 认证边界 | 缺失、伪造、失效会话统一拒绝 | B |
| SEC-04 | 输入输出注入 | HTML 与 CSV 公式注入被转义/净化 | B/C/W |
| SEC-05 | 错误信息泄漏 | 无堆栈、SQL、路径、响应正文 | B/W/R |
| SEC-06 | 日志与证据脱敏 | Key/Token/密码/Prompt/Response 不落证据 | B/P/R |
| SEC-07 | 下载安全 | no-store、nosniff、安全文件名和内容类型 | B/C/W |

## FLOW 全流程测试

| ID | 场景 | 验收条件 | 执行器 |
| --- | --- | --- | --- |
| FLOW-01 | 管理员登录到 Provider | 真人鼠标完成登录、折叠、导航 | R |
| FLOW-02 | 管理员 Provider 到 Usage | 两页均加载且无研发术语 | R |
| FLOW-03 | 开发者登录到 Project | 权限页面可达 | R |
| FLOW-04 | 开发者 Project 到 Virtual Key | 页面切换稳定 | R |
| FLOW-05 | Viewer 移动登录 | 390x844 无横向溢出 | R |
| FLOW-06 | Viewer 权限边界 | 敏感操作不可执行，API 后端拒绝 | B/R |
| FLOW-07 | 侧栏折叠持久流程 | 重建 DOM 后状态仍正确 | R |
| FLOW-08 | Control 503 | 显示安全错误和 request_id | R |
| FLOW-09 | Control 恢复 | 解除故障后恢复正常内容 | R |
| FLOW-10 | 三角色退出 | 返回登录页、请求取消、无会话残留 | R |

## 统一执行与判定

```powershell
.\scripts\quality\run-product-qa.ps1 -Group All -RunBrowserMatrix `
  -RunLivePerformance -RunPressure -PressureRequests 120 -PressureConcurrency 8
```

四小时稳定性单独使用 `product_qa_stability_gate.py --profile long_stability --run-long`。
最终报告必须逐项引用本轮 artifact；任何 `not_run`、`skipped`、超时、测试运行器错误或证据缺失
均不得计为 PASS。
