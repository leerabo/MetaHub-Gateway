"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/rate_limits/store.py
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlparse


class RateLimitStoreUnavailable(RuntimeError):
    """限流计数存储不可用。

    上层会把该异常映射为 `gateway.rate_limit_unavailable`，不会把 Redis URL、密码或
    底层 socket 错误写入 API 响应。
    """


class RateLimitCounterStore(Protocol):
    """限流计数器存储协议。

    Redis 和测试内存实现都遵守同一组原子操作语义，便于数据面逻辑只关注限流决策。
    """

    async def increment_window(self, key: str, amount: int, ttl_seconds: int) -> int:
        """递增固定窗口计数器，并返回递增后的当前值。"""

    async def acquire_concurrency(self, key: str, limit: int, ttl_seconds: int) -> tuple[bool, int]:
        """尝试占用并发槽位，返回是否准入和当前并发值。"""

    async def release_concurrency(self, key: str) -> None:
        """释放一个并发槽位，释放失败不能影响已完成的用户请求。"""

    async def ping(self) -> bool:
        """检查限流计数器存储是否已经连接且可以执行原子计数。"""


class InMemoryRateLimitStore:
    """测试用内存计数器。

    该实现只用于单进程测试和本地注入，不用于生产限流；生产路径必须配置 Redis URL。
    """

    def __init__(self, *, now: Callable[[], float] | None = None, available: bool = True) -> None:
        """创建内存计数器，并允许测试注入时间源和可用性状态。"""

        self._now = now or time.time
        self._available = available
        self._lock = asyncio.Lock()
        self._values: dict[str, tuple[int, float | None]] = {}

    async def increment_window(self, key: str, amount: int, ttl_seconds: int) -> int:
        """递增固定窗口计数器，并按 TTL 清理过期窗口。"""

        self._ensure_available()
        async with self._lock:
            self._expire_locked()
            value, _expires_at = self._values.get(key, (0, None))
            value += amount
            self._values[key] = (value, self._now() + ttl_seconds)
            return value

    async def acquire_concurrency(self, key: str, limit: int, ttl_seconds: int) -> tuple[bool, int]:
        """尝试占用并发槽位，超过限制时立即回滚本次递增。"""

        self._ensure_available()
        async with self._lock:
            self._expire_locked()
            value, _expires_at = self._values.get(key, (0, None))
            value += 1
            if value > limit:
                # 并发限制超限时必须回滚本次占用，否则会把后续请求永久挡住。
                if value <= 1:
                    self._values.pop(key, None)
                else:
                    self._values[key] = (value - 1, self._now() + ttl_seconds)
                return False, value
            self._values[key] = (value, self._now() + ttl_seconds)
            return True, value

    async def release_concurrency(self, key: str) -> None:
        """释放并发槽位，计数降到 0 时删除键。"""

        self._ensure_available()
        async with self._lock:
            self._expire_locked()
            value, expires_at = self._values.get(key, (0, None))
            value -= 1
            if value <= 0:
                self._values.pop(key, None)
                return
            self._values[key] = (value, expires_at)

    async def ping(self) -> bool:
        """返回内存存储是否处于可用状态。"""

        self._ensure_available()
        return True

    def _ensure_available(self) -> None:
        """在内存实现中模拟 Redis 不可用，用于验证数据面失败关闭路径。"""

        if not self._available:
            raise RateLimitStoreUnavailable("rate limit store unavailable")

    def _expire_locked(self) -> None:
        """清理已过期的窗口键。

        调用方必须已经持有锁，避免并发测试中出现计数被交错清理。
        """

        now = self._now()
        expired = [key for key, (_value, expires_at) in self._values.items() if expires_at and expires_at <= now]
        for key in expired:
            self._values.pop(key, None)


@dataclass(frozen=True)
class RedisConnectionConfig:
    """Redis 连接配置。

    该结构仅在内存中保存 Redis 密码，不会进入日志、响应或结构化事件 payload。
    """

    host: str
    port: int
    db: int
    username: str | None
    password: str | None


class RedisRateLimitStore:
    """基于 Redis 的限流计数器实现。

    为避免当前仓库额外依赖外部 Redis 包，这里实现最小 RESP 客户端；M0 每个命令打开一次短连接，
    后续高吞吐版本可以替换为连接池而不改变上层限流服务接口。
    """

    def __init__(self, redis_url: str) -> None:
        """解析 Redis URL，密码仅保存在进程内用于 AUTH。"""

        self.redis_url = redis_url
        self._config = _parse_redis_url(redis_url)

    async def increment_window(self, key: str, amount: int, ttl_seconds: int) -> int:
        """使用 INCRBY 递增固定窗口计数器，并在首次创建时设置 TTL。"""

        value = int(await self._command("INCRBY", key, str(amount)))
        if value == amount:
            await self._command("EXPIRE", key, str(ttl_seconds))
        return value

    async def acquire_concurrency(self, key: str, limit: int, ttl_seconds: int) -> tuple[bool, int]:
        """使用 INCR 占用并发槽位，超限时用 DECR 回滚。"""

        value = int(await self._command("INCR", key))
        if value == 1:
            await self._command("EXPIRE", key, str(ttl_seconds))
        if value > limit:
            # 超限请求不能留下并发占用，否则后续正常请求会被错误拒绝。
            await self._command("DECR", key)
            return False, value
        return True, value

    async def release_concurrency(self, key: str) -> None:
        """使用 DECR 释放并发槽位，非正数时删除键。"""

        value = int(await self._command("DECR", key))
        if value <= 0:
            await self._command("DEL", key)

    async def ping(self) -> bool:
        """用 PING 验证 Redis 连接、认证和 DB 选择是否可用。"""

        response = await self._command("PING")
        return str(response).upper() == "PONG"

    async def _command(self, *parts: str) -> object:
        """发送一个 Redis 命令并解析 RESP 响应。

        任何 socket、协议或 Redis 错误都会收敛成 RateLimitStoreUnavailable，避免把 Redis 地址、
        密码或底层异常传播给调用方。
        """

        try:
            reader, writer = await asyncio.open_connection(self._config.host, self._config.port)
            try:
                await self._authenticate(reader, writer)
                writer.write(_encode_command(parts))
                await writer.drain()
                return await _read_response(reader)
            finally:
                writer.close()
                await writer.wait_closed()
        except Exception as exc:
            raise RateLimitStoreUnavailable("redis command failed") from exc

    async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """按需执行 AUTH 和 SELECT。

        Redis 密码只进入 AUTH 命令，不写日志；DB 选择失败会被外层统一映射为存储不可用。
        """

        if self._config.password:
            if self._config.username:
                writer.write(_encode_command(("AUTH", self._config.username, self._config.password)))
            else:
                writer.write(_encode_command(("AUTH", self._config.password)))
            await writer.drain()
            await _read_response(reader)
        if self._config.db:
            writer.write(_encode_command(("SELECT", str(self._config.db))))
            await writer.drain()
            await _read_response(reader)


def _parse_redis_url(redis_url: str) -> RedisConnectionConfig:
    """解析 Redis URL 为连接配置，并拒绝不支持的协议或非法数据库编号。"""

    parsed = urlparse(redis_url)
    if parsed.scheme != "redis":
        raise RateLimitStoreUnavailable("unsupported redis scheme")
    db_text = parsed.path.lstrip("/") or "0"
    try:
        db = int(db_text)
    # 输入解析异常统一映射为业务错误，避免无结构异常穿透 API。
    except ValueError as exc:
        # Redis 数据库编号必须是整数，配置错误时提前失败，避免请求期才出现隐性绕过。
        raise RateLimitStoreUnavailable("invalid redis db") from exc
    return RedisConnectionConfig(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        db=db,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
    )


def _encode_command(parts: tuple[str, ...] | list[str]) -> bytes:
    """把 Redis 命令编码成 RESP 数组，保持自研轻量客户端只处理必要协议子集。"""

    encoded_parts = [part.encode("utf-8") for part in parts]
    payload = [f"*{len(encoded_parts)}\r\n".encode("ascii")]
    for part in encoded_parts:
        payload.append(f"${len(part)}\r\n".encode("ascii"))
        payload.append(part)
        payload.append(b"\r\n")
    return b"".join(payload)


async def _read_response(reader: asyncio.StreamReader) -> object:
    """读取一个 RESP 响应。

    PRD-022 只需要简单字符串、整数、Bulk String 和错误响应；数组留给后续扩展。
    """

    prefix = await reader.readexactly(1)
    line = await reader.readline()
    if prefix == b"+":
        return line.rstrip(b"\r\n").decode("utf-8")
    if prefix == b":":
        return int(line.rstrip(b"\r\n"))
    if prefix == b"$":
        length = int(line.rstrip(b"\r\n"))
        if length < 0:
            return None
        data = await reader.readexactly(length)
        await reader.readexactly(2)
        return data.decode("utf-8")
    if prefix == b"-":
        # Redis 返回错误响应时把底层细节折叠为限流存储不可用，避免泄露服务端信息。
        raise RateLimitStoreUnavailable("redis returned error")
    raise RateLimitStoreUnavailable("unsupported redis response")
