# PRD-048 效果-成本路由基础测试记录

测试日期：2026-07-19

当前状态：已完成。设计、两轮设计自审、实现后安全复审、代码实现、0037 正反迁移、专项与根级全量回归、真实三服务/API/Webhook、真实 Chrome 三角色三视口、资源幂等清理，以及停止后数据库和日志复核均已通过。最终成功批次仅认定 `D:/MetaHub/artifacts/prd048/20260719T200349`；中间失败截图和被主动中止的旧 pytest 进度不能替代本记录中的最终证据。

运行环境：Windows PowerShell；Python 使用 `C:/Users/rabol/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`；pytest 的 `TEMP/TMP` 固定为 `D:/MetaHub/.tmp`，`PYTHONPATH=shared-sdk/python;control-plane-api/src;gateway-data-plane/src;examples/python/src`。迁移和真实运行只操作开发库一致性副本，未升级或改写仍处于 0034 的 `D:/MetaHub/.data/metahub-dev-prd031.db`。

## 1. 验收口径

- Excel `TC-0095`：完整证据和依赖齐备时，高质量但更贵的模型必须击败低质量便宜模型；门槛、冷启动、缺价、混币、缺延迟、零权重和同分规则确定且可解释。
- Excel `TC-0096`：模型质量统计和人工评分按租户追踪；admin/developer/viewer RBAC、统计/反馈独立版本、CAS、幂等、审计、Webhook 和 RequestLog 冻结事实成立。
- 所有 API 的 tenant_id 只来自认证上下文；错误使用统一 code、request_id 和 trace_id；前端隐藏入口不替代后端权限校验。
- Provider Key、Virtual Key、Token、Prompt、Response、密码和原始上游错误不得进入日志、事件、审计、DOM 或验收 JSON。
- 自动化、迁移副本、真实运行、真实浏览器和停止后终态是相互独立的证据，缺一项不得标记完成。

完整需求、数据/API/页面/错误码/安全约束、测试矩阵和三轮复审见 [PRD-048 设计文档](../prd/PRD-048-quality-cost-routing-foundation.md)；公开 wire 与 NULL 合同见 [RoutingPolicy 与模型质量 API](../api/routing-policies.md)。

## 2. 自动化与静态门禁

| 范围 | 最终命令或证据 | 结果 |
| --- | --- | --- |
| Python 根级全量 | `python -m pytest -q --basetemp D:/MetaHub/.tmp/prd048-final-pytest-20260719t2135` | 退出码 0；`680/680` 通过，0 个 F/E/x/X；`python-full-final.stderr.log` 为 0 bytes |
| 0037 专项 | `pytest -q control-plane-api/tests/integration/test_db_migrations.py -k quality_cost` | `22 passed, 33 deselected`，约 211.90s |
| 全迁移文件 | `pytest -q control-plane-api/tests/integration/test_db_migrations.py` | `55/55` 通过 |
| Shared 全组 | `pytest -q shared-sdk/python/tests` | `74/74` 通过 |
| Python 示例 | `pytest -q examples/python/tests` | `38/38` 通过 |
| Runtime 验收器单测 | `pytest -q scripts/acceptance/tests/test_prd048_runtime_acceptance.py` | `9/9` 通过 |
| Node 示例 | `npm test --prefix examples/js` | `33/33` 通过 |
| Console 全量 | `npm test --prefix console-web` | `261/261` 通过；仅 Node SQLite experimental 提示，无失败 |
| Console lint | `npm run lint --prefix console-web` | `110 files passed` |
| Browser 验收器专项 | `node --test console-web/tests/unit/prd048BrowserAcceptance.test.mjs` | `15/15` 通过 |
| Dashboard 900px 专项 | `node --test console-web/tests/unit/dashboard.test.mjs` | `6/6` 通过 |
| Python compile | `python -m compileall -q shared-sdk/python control-plane-api/src gateway-data-plane/src examples/python/src scripts/acceptance` | 退出码 0 |
| Ruff | `ruff 0.15.21 check --select F shared-sdk/python control-plane-api gateway-data-plane examples/python scripts/acceptance` | `All checks passed!` |
| 中文注释 | `python scripts/ci/check-comments.py` | 退出码 0，`comment coverage ok` |

根级 680 个测试已包含 Shared、Gateway、Control、Python 示例和验收脚本测试。此前单独执行的 Gateway 全组、模型评分、RoutingPolicy quality_cost、Dry-Run、RequestLog、错误/Webhook 等专项均退出 0；最终完成结论只采用最后冻结代码上的根级全量与上表独立门禁，不重复累计测试数。

## 3. 0037 迁移证据

最终迁移批次目录为 `artifacts/prd048/20260719T200349`：

- 源开发库 SHA-256 为 `06632c12ab305507213c34756c815618b58092373f386bd103245ad6d943fabd`；SQLite Backup API 生成的初始副本 SHA-256 为 `023bb873a952f72f12b93957df75326dcb7ac4830a354b837e57399990615289`。
- 首次升级依次应用 0035、0036、0037；第二次 apply 返回空集合。四份验收库均为 37 条 applied migration、最高 0037、integrity `ok`、foreign_key_check 0。
- 0037 checksum 为 `536175d8fc8c0a67994dcaaa9815a67e0b58dea5cc6697134a543d510228d0d1`。
- 空业务 `0037 -> 0036 -> 0037` 往返中，17 个 Score 业务字段除随机 ID 外逐字段一致；前后 manifest SHA-256 均为 `490236d715af87fa3bc555b783f158b4928cb8b4902fcee01ed134c0da937409`。
- 自动统计不可完整重建时固定报 `prd048_down_automatic_statistics_unrebuildable`；存在不可降级质量业务事实时固定报 `prd048_down_business_facts_present`。两类失败前后的 schema/data 逻辑 SHA、文件 SHA 和迁移状态均保持不变。
- 22 个专项覆盖精确 Attempt/Stream 质量白名单、UTC/微秒延迟、64-bit 极值、坏 owner/坏时间/矛盾资格、7 类业务事实 guard、4 类自动统计不可重建 guard、空业务往返和硬约束。

## 4. 真实三服务验收

Runtime 状态见 `artifacts/prd048/20260719T200349/runtime-state.json`，运行结果 `status=passed`：

- 使用两个本地 OpenAI-compatible 上游构造“高质量较贵”和“低质量便宜”，最终选择 `prd048-quality-a`；冻结 `statistics_version=0`，当前统计推进到 1，证明历史解释不被当前 Score 回算。
- 两个候选均低于质量门槛时，Provider 调用数和 Attempt 数均为 0，失败请求仍有统一错误码和 request_id 可追踪。
- admin 读取/写入成功；developer 读取成功、写入 403；viewer 读取 403。第二真实租户对 GET/PATCH 均得到防枚举 404，目标 Score 未变化；客户端请求未携带 tenant_id。
- Score CAS、版本冲突、同 request_id 同意图恢复和不同意图冲突均通过。`model.feedback_recorded` 共 6 条，Webhook HMAC 全部有效。
- RequestLog/Decision/Candidate 冻结公式、统计版本、证据、三分量和最终分；数据库 Prompt 哨兵命中 0，Runtime stderr 为 0。
- Gateway、Control API、Console 在真实验收期间健康检查均为 HTTP 200。

## 5. 真实 Chrome 验收

最终结果为 `artifacts/prd048/20260719T200349/browser/browser-result.json`，与 `browser.stdout.json` 9292 bytes 字节完全一致，SHA-256 为 `203b9ff009a19976c916f523200aab0c29cd90155fedeeedd16c67643fe54d49`，browser stderr 为 0。

- admin 完成 PAGE-068、PAGE-069、RequestLog 三页三视口共 9 张图，两条无副作用 PATCH 均进入领域校验并返回 422；developer 同样 9 张图，DOM 只读且两条 PATCH 均由后端返回 403；viewer 四个受限 hash 全部回退 overview，只生成三视口 3 张图。
- 21 张 PNG 宽度按 1280、900、390 各 7 张，高度分别不小于 720、800、844；全部 `nonBlank=true`、每张截图前后两次隐私扫描通过，21 个 SHA 均不同。
- admin/developer 迟到 PAGE-069 200 响应均被 owner/generation 门禁拒绝重建旧 DOM。viewer 对 ModelAlias、RoutingPolicy、Score、RequestLog 的 request/response/prefetch/write 全部为 0。
- 三角色隐私检查点分别为 27、27、15；blocked request、external request、browser error、DOM/storage/network 敏感命中均为 0。三角色均真实 POST logout 一次得到 200，并确认认证 Storage 清空。
- 900px viewer Dashboard 曾暴露四列 `dl` 最小内容撑宽问题；修复后 root/body 均为 900，Console main client/scroll width 均为 602，最终截图无横向溢出或遮挡。

## 6. 清理、静止库与日志终验

- 首次 cleanup 变更：Alias 1、Environment 1、Model 3、Provider 3、Session 3、Tenant 1、User 2、Virtual Key 1、Webhook 0，`cleanup_verified=true`。
- 第二次 cleanup 上述九类变更计数全部为 0，仍为 `cleanup_verified=true`，证明幂等且没有扩大清理范围。
- 三服务停止后执行 `wal_checkpoint(TRUNCATE)`，最终库 SHA-256 为 `39eaf358f537aa4c24fa2b061a821e47fb1238b5a6dd651174e182ee3be66ce3`；37 条迁移、最高 0037、checksum 匹配、integrity `ok`、FK 0，WAL/SHM 均不存在。
- 8000、8001、5173 监听均为 0。Runtime、Browser、两轮 cleanup 和 Console stderr 均为 0 bytes。
- Control 2639 条、Gateway 322 条 access line 全部符合 Uvicorn 访问格式；Control/Gateway 结构化日志逐行 JSON 解析，仅各有 4 条标准 Uvicorn 生命周期行。结构化级别无 error，warning 仅为受控异常/RBAC 场景的固定 `request.failed`。
- 日志和验收 JSON 对三角色密码、认证/加密/Internal Secret、Bearer/JWT、Provider Key 形态、Prompt/Response 哨兵、Traceback、NativeCommandError、未知 console 错误和 5xx 浏览器错误的命中均为 0。数据库 Webhook Secret 只保存 `aesgcm-v1` 密文，公开 `whsec_` 前缀不构成明文 Secret。

## 7. 结论

PRD-048 的设计、实现、租户/RBAC/统一错误、质量与预算事务、迁移、专项/全量、真实三服务、Webhook、浏览器、隐私、幂等清理和静止终态全部通过，可以标记完成。

按 Excel `01_PRD总表` 重新识别出的下一条未完成项是 `PRD-049 - 调用反馈与采纳率记录`（M1-企业POC / P1 / 用户反馈）。总体 108 条 PRD 目标仍在进行中，不因 PRD-048 完成而宣称全部完成。
