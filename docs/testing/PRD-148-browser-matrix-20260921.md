# PRD-148 当前 Web 矩阵复验（2026-09-21）

本批绑定源码 revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`、源码 SHA256 `08026d4e0fc53163dcf67bd7369c2faa7834f2b6c40ca390e37966dfa0abb0bb`，生产资源摘要为 `5b4a07dc44b74c97a1efddc0f60c585b18d44f7923aa69141208f7691b19d148`。

静态验证通过：`npm run build`（294 assets、276 modules）、`npm run lint`（838 files）和 `npm test`（2211/2211）。

完整 Web 矩阵目录为 `.artifacts/product-qa/20260921-021856`。合同测试与 lint、Chromium 1x/1.5x、Edge 1x/1.25x/1.5x、Firefox 1x/1.25x/1.5x 通过。Chromium 1.25x 这一次矩阵任务只有 `product_qa_browser_journey_failed`，没有 JSON 旅程产物，因此按真实运行失败/证据不足保留，矩阵结论是 10/11；`evidence-manifest.json` 证明批次起止源码未变。

随后在同一源码下独立重跑 Chromium 1.25x，产物 `.artifacts/product-qa/browser-real-chromium-scale-1_25.json` 显示 admin/developer/viewer 三条旅程通过、请求失败数为 0、控制台错误数为 0。该重跑说明失败具有运行波动特征，但不能覆盖原矩阵失败，也不能把矩阵批次改写为 11/11。

此前 Chromium 1x/1.5x 的 Workspace 503 锁竞争由延迟且串行的 page-view 遥测提交修复；本批 Chromium 1x 单档通过，未再复现该 503。此结果只证明当前浏览器回归改善，不构成真实商业、GA 或稳定性证据。

PRD-148 仍未完成。真实注册到付费、企业治理/私有 Gateway、Partner/Device 数据路径、六方会签、发布后复核、回滚、财务/隐私/安全/迁移对账、scheduler-enabled 259200 秒稳定性观察和 HEAD/origin 一致性仍未闭合。

补充边界：`outputs/prd148-negative-current-final-20260916/status.json` 中 Platform 38/38、Workspace 44/44、Partner/Support 17（1 skipped）、Device 14/14 均为测试通过，但该批 `source_unchanged=false`，只能算历史源码漂移证据，不能替代当前源码矩阵。

当前源码的 Viewer 交互复验见 `.artifacts/product-qa/20260921-interaction-ux-classified/prd148-human-interaction-chromium-scale-1.json`，4/4 目标页通过，硬请求失败、响应错误、控制台错误和布局问题均为 0。3 个 page-view `net::ERR_ABORTED` 作为预期的陈旧访问取消单独记录；它们由路由转换取消旧遥测写入，服务端对应请求返回 200，不作为网络失败。详细说明见 `docs/testing/PRD-148-human-interaction-20260921.md`。
