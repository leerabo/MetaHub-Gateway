# PRD-146 导航工作线程测试

设计及两轮自审：`../prd/PRD-146-navigation-worker-boundary.md`。

修订前 `.artifacts/s2-navigation-worker-before-20260910.xml` 在导航路径复现同步服务阻塞事件循环。修订后 `.artifacts/s2-navigation-worker-after-20260910.xml` 共 35 项通过，其中六条路由各覆盖成功和异常传播，共 12 项；另含浏览器 runner 与用量夹具的 23 项。

此测试直接验证事件循环在同步服务执行期间能推进，不是数据库权限或最终浏览器无故障的证明。数据库连接和事务由原服务持有，后续复用真实数据库导航测试，并随 S2 候选生产包联合验收。旧源码报告中的导航/页面统计 503 尚不能仅凭该单元测试宣称消除。

候选生产包 `.artifacts/s1-browser-36cbe1xa/acceptance.json` 三视口 27 阶段通过，0 API 5xx、0 导航数据库异常。本次未再出现两个 34 秒页面统计等待，但不推断所有可能根因均已消除。

`.artifacts/s2-finance-index-navigation-20260910.xml` 为 55 通过、1 失败：旧偏好测试依赖此前 scheduler 测试已产生一条事件，和原设计的纯 UI 偏好不发导航变更事件合同不一致。改为操作前后事件数相等，保留精确 CAS/重放/冲突和 command 断言，修订后 `.artifacts/s2-navigation-replay-independent-20260910.xml` 四项通过，包括断连正文、导航、偏好和高级工具。其生产导航源码使用已验收候选目录，修订测试文件来自工作区；不改生产事件逻辑、不重复跑无改动 scheduler。
