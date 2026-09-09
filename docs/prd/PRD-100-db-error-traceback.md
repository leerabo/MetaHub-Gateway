# PRD-100 数据库异常兼容修复

在计量迁移回滚验收中发现 DbError 使用 frozen dataclass，contextlib 重抛时写入 __traceback__ 会触发 FrozenInstanceError，遮蔽原本的迁移拒绝原因。

修复只取消异常对象的 frozen，保留 code/message/suggestion 和字符串输出。异常对象需要兼容 Python 的 traceback/context/cause 状态，不据此改变迁移、连接或权限规则。

复核一：业务返回字段和迁移错误码不变，禁止把错误转换为成功。复核二：contextmanager 中捕获的仍为同一 DbError，原 traceback 保留；计量迁移有事实拒绝不再被次级异常掩盖。这是开发中发现的兼容补丁，不冒称新增了事前完整业务域设计。
