# 试用入口回归

范围：首页重新排版、登录立即显示、组织仅填名称。
设计见 `../prd/MetaHub-trial-entry-revision-20260910.md`。

## 已验证

- 前端专项：`.artifacts/trial-entry-focused-final-20260910.xml`，54 项通过。
- 后端：`.tmp/trial-entry-tests-hbknqrr7/results.xml`，11 项通过。
  包含中文名称、自动标识、默认区域调整后的重放、非法显式字段拒绝、
  显式及自动标识各 100 次并发重试、CSRF、Owner、平台越权拒绝和零假模型资源。
- 源码浏览器：`.artifacts/s1-browser-ooy1i2gr/acceptance.json`，通过。
  1920/1440/1280/390/320 屏宽无横向溢出，正文不与场景重叠，首屏露出能力区。
  Canvas 618-1604 种颜色、1044-8118 个变化像素；暂停及减少动态效果通过。
  故意挂起品牌和会话接口后，登录表单桌面 561ms、手机 1086ms 出现，迟到响应不清空输入。
  1280/390 注册、中文名称组织、Owner、使用者、独立 KEY、轮换与停用、配置引导均通过。
  无外部模型调用，未使用在线用户账号，不产生在线测试组织。
- Python Ruff、前端 lint、生产模块边界、git whitespace 检查通过。

## 完整性边界

全量前端测试运行 `.artifacts/trial-entry-unit-20260910.xml`：1986 项，14 项失败。
其中品牌加载顺序的旧断言已按本次非阻塞设计更新，专项回归通过。
其余 13 项集中于账号安全测试未进入账号安全视图，以及控制台 VM 夹具缺少
`workspaceProfileMutation` 等依赖，未在本批顺带修改这些测试或控制台业务。
不能将专项通过表述为全仓库测试全绿。

首次后端测试把标准 422 错误误写为 400，已纠正并重跑。
旧迁移往返用例依赖当前目录定位 down 文件，不适合本次从独立基线运行的夹具；
本批无迁移，未宣称完成新增迁移往返验证。

首次源码截图检测发现文案与场景相差数像素重叠，已修正间距后完整重跑。
生产候选的 289 个资源均按 SHA-256 验证。两次生产测试在 Canvas 元素截图步骤等待超时，
添加阶段诊断后，相同产品构建在 `.artifacts/s1-browser-vltvtfvc/acceptance.json` 全链通过。
未改变动画或放宽像素断言来隐藏失败；尚不能据此确定两次等待超时的环境原因。

## 在线数据

发布前备份目录：`.tmp/trial-entry-release-20260910/`。
有效备份为 `database-verified.sqlite3`，使用项目数据库连接注册自定义校验函数后
通过 integrity_check 与 foreign_key_check。首次以标准 SQLite 连接检查遇到
缺少 metahub_sha256 函数，保留原证据，未改动在线数据库。
不执行数据库迁移，不重置账号、登录会话、组织、员工、KEY 或账单。

## 发布结果

已发布至 `http://localhost:5173/`，Control 健康检查 200，新进程 10272。
Frontend 仍为原 11248，未关闭用户浏览器或修改用户 Cookie。
首页、登录、服务状态、品牌及注册策略 HTTP 检查均为 200。

最终资产集：`e030216040c5a5dc6067b6c1fb23d913b08aefc0b257ab48992cda922e4bea7a`。
最后一项布局修订将组织名称表单改为单列；最终候选来源
`.tmp/trial-entry-stage-bse9DD/console-web/dist`，旧前端可恢复副本保存在
`.tmp/trial-entry-release-20260910/previous-dist`，发布记录为同目录 `publication.json`。

最终生产全链复验：`.artifacts/s1-browser-bljrby1t/acceptance.json` 通过，
289 个生产资源逐一核验，桌面/手机组织表单截图已人工检查。该轮慢接口实验中
登录表单桌面 487ms、手机 393ms 出现；五种尺寸动画、暂停、减少动态效果及
注册到组织/使用者/KEY 的完整流程均通过，浏览器 pageErrors 为 0。

发布后与本次有效备份逐表逐列只读核对，17 张身份/使用者/KEY/计量/财务相关表
无新增、无删除、无已有字段变动。未将用户在前一轮发布后新注册的账号恢复掉。
本次仍是本地试用增量，不宣称商业化总目标或 72 小时长稳完成。

后续首屏高度及试用导航修订已另行发布，最新记录见
`MetaHub-trial-navigation-20260910.md`；上面的资产摘要保留为本次原发布历史。
