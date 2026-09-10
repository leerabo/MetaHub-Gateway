# PRD-147 GET 导出来源验证测试

设计与两轮自审：`../prd/PRD-147-resource-export-browser-origin.md`。

- 后端来源负矩阵与验收夹具合计 56 项通过，证据 `.artifacts/s2-origin-fixture-unit-20260910.xml`。包含缺少/重复来源、相似域名、跨站、错误方法、来源不能代替 CSRF，以及夹具事务回滚与并发去重。
- 前端共享认证和 S2 资源回归 473 项通过，证据 `.artifacts/s2-finance-frontend-origin-20260910.xml`。只有 Workspace + GET + csrfRequired 使用 origin-only referrer，其它请求不改变来源策略，客户端不得手工设置 Origin/Referer。
- 真实 Cookie API 定向 1 项通过，证据 `.artifacts/s2-api-8wu9yy9g/results.xml`：同组织员工归属、原币用量、CSV、幂等、无 Origin 的可信同源 GET 导出、显式不可信 Origin 拒绝及 KEY 轮换后历史保持。不是模型推理调用。
- 工作区源码浏览器联合流程通过 27 个业务阶段、21 个布局检查，见 `.artifacts/s1-browser-zvjj5lze/acceptance.json`。三个视口实际下载 CSV，验证筛选后的两条记录及 KEY 归属。该报告 `production_build=false`，且仍记录两次页面访问统计接口 503；不代表全部网络请求或全部导出页面通过。
- 独立暂存树 `2fc47fdfd46fa5c081043e4af7b1c2f570624a16` 已导出至 `.tmp/s2-origin-index-20260910`。仅含本次来源修复及其依赖，生产检查覆盖 271 个模块、289 个源码资产；构建产出 289 个资产，摘要 `612407cddff53af76d08eeb3e866d7333c1e86ea09af11a249981181b75e0d13`。从导出源码执行后端 33 项、前端 95 项，分别见 `.artifacts/s2-origin-index-backend-20260910.xml` 与 `.artifacts/s2-origin-index-frontend-20260910.xml`。
- 以上暂存树是测试时版本；随后仅更新本文及敏感审计文档，不改变已测生产代码。本地 5173 试用实例未变更，完整 S2 生产包浏览器验收与发布仍待完成，不把本结果当作完整 Excel 验收。

## 已保留失败

`.artifacts/s1-browser-580gfa72/acceptance.json` 首次发现 GET 缺 Origin 导致 CSV 403；`.artifacts/s1-browser-rt0_5hkj/acceptance.json` 证实只改后端仍不够，因为控制台 no-referrer 不发送 Referer。保留两次失败，未更改既有证据。

API runner 在隔离工作目录中运行后首次缺少相对种子文件，记录 `.artifacts/s2-api-i2ww51lw/`；改为显式指向所选源码的种子文件，未复制用户 .env。早期 `.artifacts/s2-api-tmhutp0x/` 在仓库 cwd 加载过本地配置，不作为不读取配置的隔离性证明。

导航偶发 503 位于 `.artifacts/s1-browser-fsuie9br/acceptance.json`，未证明由来源修复解决，继续保留调查。

重新核验最终源码报告还发现 `/workspace/navigation/page-views` 两次返回 `workspace_session.unavailable` 503。其业务步骤通过不能抵消网络错误；`navigation_database_failures=[]` 也仅代表已安装的导航诊断没有捕获数据库异常，不能证明页面访问统计接口无故障。
