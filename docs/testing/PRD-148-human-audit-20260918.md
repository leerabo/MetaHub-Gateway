# PRD-148 人类式业务走查（2026-09-18）

本轮先集中检查再按业务域修改。范围包括管理员、开发者、Viewer 的页面入口和移动布局，以及 Provider 状态、订阅/争议、员工 KEY、Webhook、请求解释、反馈和内部 FinOps 的空态、状态语义和可操作性。浏览器走查使用仓库 Playwright 隔离运行；不把它称为原生 computer-use，原生入口错误 `unsupported Codex auth method: apikey` 保留。

## 修改前发现

- Provider 状态页同时显示“健康”、探测成功率 97.15%、P95 5503 ms 和 0 个真实调用资格样本，页面没有说明健康结论的证据范围。
- 无订阅 Workspace 只显示“当前 Workspace 暂无订阅”，没有说明付款、发票和争议不可用的原因，也没有开通下一步。
- 请求解释已具备输入旁校验、英文 i18n、刷新/清空图标和安全错误态；本轮确认不再重复修改。
- Webhook Secret 列表只显示前缀/指纹且创建后一次性展示；本轮未发现需要放宽安全边界的问题。
- 69 个域页面自动化审计（59 桌面、10 移动）没有页面/API/布局错误，但这只是读取和布局审计，不等于每个写入动作或真实商业链路通过。

## 批量修改

- `renderProviderStatusPanel.js` 和 `renderProviderHealthPanel.js` 增加“证据范围”提示：无真实调用样本时明确说明主要来自探测；探测成功率低于 100% 时显示失败/超时提示。后端 `effective_health_status` 仍是唯一健康判定，前端没有自行改写状态。
- `renderWorkspaceCommercialAccounting.js` 的无订阅态说明没有套餐、付款、发票和争议记录，并提示联系平台运营开通后刷新；不会在无发票时渲染争议提交表单。
- `main.css` 为证据提示和订阅空态加入稳定的窄屏样式，保持现有布局轨道和可读性。
- `production-surface-manifest.json` 同步当前 294 个生产资源摘要。

## 修改后证据

- `node --test tests/unit/providerStatus.test.mjs tests/unit/providerHealth.test.mjs tests/unit/prd144CommercialAccounting.test.mjs`：24/24 通过。
- `npm run lint`：838 files 通过，生产面校验通过。
- `npm run build`：294 assets 通过，资源摘要为 `df518fe132342130280c753f957a42ed8e90a43a21284b8d34936b08170de022`。
- 当前源码证据：revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`，QA 源码闭集 SHA256 `199b63e279eee9af271755ed5b5715d58b94fdbe59e146ea1eb1b226d4d5b8ac`，2688 files。

本轮角色全量脚本在管理员 4 张截图后退出且没有生成 report；该运行没有被计为通过，作为运行未终态保留。历史 Chromium 1× 登出 15 秒失败、scheduler-enabled 争用/503、计量历史数据缺口和所有真实 GA 门禁继续保留。

PRD-148 继续保持未完成。
