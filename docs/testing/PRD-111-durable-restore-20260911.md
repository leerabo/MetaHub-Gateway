# PRD-111 持久恢复执行器测试记录

日期：2026-09-11。范围：独立恢复执行、结果绑定、停止确认；不是完整容灾验收。
设计及两轮编码前自审：`docs/prd/S3-durable-restore-lifecycle-20260910.md`。

## 结果

- `s3-restore-cancel-20260911-r1.xml`：1 通过，原生 PostgreSQL 实际 COPY 中途取消。
- `s3-restore-results-20260911-r1.xml`：41 通过，SQLite 持久执行与结果防错校验。
- `s3-durable-restore-final-20260911-r1.xml`：84 项，83 通过、1 环境依赖失败，无跳过。
  失败发生在完整 PostgreSQL schema 迁移前：`.tools/Python312-x64/python.exe` 未安装 pglast。
  未放宽迁移解析门禁，使用仓库已有 `.venv/Scripts/python.exe`（pglast v7.18）重跑。
- `s3-durable-restore-schema-20260911-r2.xml`：该完整 schema 真实备份恢复测试 1 通过，
  包括当前迁移链、受保护 down/up、真实 pg_dump/pg_restore 和数据库指纹比较。
- 因此本次选定的 84 个用例均有最终通过结果。XML 均位于 `.artifacts/`；保留初次失败证据。
- 对本实施单元 7 个 Python 文件的 Ruff 检查通过。

## 覆盖

SQLite 保留真实目标、互不覆盖、请求绑定不变、损坏结果拒绝、取消优先于成功、崩溃后不重放、
并发文件锁、终态 journal 不可重写/删除、子进程回收、超时不启动、指纹读取可取消。
新增停止标志保证清理报错不会掩盖未确认的子进程停止。

原生 PostgreSQL 覆盖：每执行独立新库、源库不受影响、成功后关闭准入、孤立后台会话清退、
停止不确定保持 pending、服务器身份变化拒绝确认、非所属库不操作、真实 COPY 中途取消。
备份回归覆盖 MVCC 同快照、源库离线恢复、数据损坏、非空目标保护和完整 schema。

## 环境与边界

全部使用 `.tmp/` 下新建测试数据和仅监听 127.0.0.1 的 PostgreSQL 集群；测试集群已停止，
进程检查未发现本轮恢复测试目录对应的 postgres.exe 残留。未读取或迁移试用数据库，
未重启 5173/8001，未访问真实付费模型服务。

恢复 target 是隔离验证产物，不是已提升的生产库；指纹不是账本业务守恒证明。
DR 运行时接入、Gateway 路由 ACK、对象副本与跨区 KMS、防脑裂和 RPO/RTO 门禁仍待完成。
