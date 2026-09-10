# 商业请求执行批次验收

日期：2026-09-10。范围为 PRD-098/099/102/103/104/105 的请求响应和支付回调并发修复。
原 Excel 需求与全部验收门禁保持；本记录不宣布商业化全域、PRD-148 或长稳已完成。

## 设计和归属

两轮编码前及支付补充自审见 `../prd/S2-commercial-worker-boundary-20260910.md`。
六条 PRD 各有 `PRD-编号-commercial-execution.md` 设计及同号独立测试/敏感记录。
同步服务整体交给工作线程，事务、授权、数据库连接关闭和原 HTTP DTO 不拆分。

## 实际结果

- 修复前六个领域的代表读取全部复现事件循环阻塞：
  `.artifacts/s2-commercial-worker-before-20260910.xml`，6 failed。
- 修复后 41 分支的成功/重放/异常及 ContextVar、心跳和单次调用共 123 passed。
  加测试副本保护 5 项，最终 `.artifacts/s2-commercial-worker-fixture-final-20260910.xml`
  共 128 passed。辅助夹具首次受系统 pytest 临时目录 ACL 影响，改用独立 basetemp；
  随后修正仅测试目录 mkdir 的 exist_ok，早期失败 XML 均保留。
- 第一轮商业 API：`.artifacts/s2-api-66unkpf8/results.xml`，7 passed/1 failed。
  支付进一步诊断 `.artifacts/s2-api-059pqq1v/results.xml`：94 重放成功、6 SQLITE_BUSY/503。
- 支付修复后 `.artifacts/s2-api-urihxwpb/results.xml` 通过首次 8 并发唯一收据、写锁下
  只读重放、100 重放、乱序、催缴、退款、正文冲突及完整性/敏感数据断言。
- 最终联合 `.artifacts/s2-api-80iz7dsi/results.xml` 的其他 7 个流程全部通过，支付的并发
  阶段通过后，旧测试因固定相同时间却依赖随机订单排序而失败。按订单 ID 校验后，
  `.artifacts/s2-api-vn4m3klt/results.xml` 支付复合流程通过；不重跑未改变且已通过的 7 项。
- 相关 11 个 Python 文件 Ruff 通过，`git diff --check` 通过。数据库始终是独立副本，
  初始空白基线哈希 `062ae8e3a8024030efb712a5120793483203b86d552fcfd86a46e9bf804f4c2a`。
  每个副本仍经真实迁移校验，未删除迁移历史、放宽校验或减少测试并发。

## 证据边界

API 使用 ASGITransport；部分旧测试使用受控 Platform/Workspace 身份夹具，不能证明
真实浏览器/MFA 全链。数据库完整性与业务事务通过不是新一轮 SQLite/PostgreSQL 迁移
往返证明。本批无 schema 改动，复用原迁移记录，不为单纯线程边界重复重建全部 schema。

支付为模拟渠道签名回调，不是真实收付款；发票为测试产物，通知没有真实 SMTP/SMS。
未做付费模型推理、用户账号操作或历史计量分类。未改 UI，所以不重复既有布局截图，
商业完整业务浏览器、多 DPR、多浏览器、Linux、规模性能、真实外部服务和 72 小时门禁仍待完成。

## 下一步

完成本增量独立提交与后端发布后，继续商业真实依赖和操作闭环，并沿原 Excel 推进
运行时、企业身份、私有 Gateway 与迁移治理。不能将本批 8 个复合 API 的增量回归解释
为整个商业化批次已完成。
