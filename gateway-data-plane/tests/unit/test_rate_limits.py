"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/unit/test_rate_limits.py
"""

from __future__ import annotations

import pytest

from metahub_gateway.rate_limits.service import estimate_request_tokens
from metahub_gateway.rate_limits.store import InMemoryRateLimitStore, RateLimitStoreUnavailable


@pytest.mark.anyio
async def test_in_memory_rate_limit_window_resets_after_ttl() -> None:
    """窗口边界：固定窗口计数器过期后重新从当前请求计数。"""

    now = 1000.0
    store = InMemoryRateLimitStore(now=lambda: now)
    assert await store.increment_window("metahub:rl:test:rpm:960", 1, 90) == 1
    assert await store.increment_window("metahub:rl:test:rpm:960", 1, 90) == 2
    now = 1091.0
    assert await store.increment_window("metahub:rl:test:rpm:960", 1, 90) == 1


@pytest.mark.anyio
async def test_in_memory_concurrency_rolls_back_when_denied() -> None:
    """并发边界：超限请求会回滚本次占用，释放后下一次请求可继续进入。"""

    store = InMemoryRateLimitStore()
    allowed, current = await store.acquire_concurrency("metahub:rl:test:concurrency", 1, 120)
    denied, denied_current = await store.acquire_concurrency("metahub:rl:test:concurrency", 1, 120)
    await store.release_concurrency("metahub:rl:test:concurrency")
    allowed_after_release, _current_after_release = await store.acquire_concurrency(
        "metahub:rl:test:concurrency",
        1,
        120,
    )
    assert (allowed, current) == (True, 1)
    assert (denied, denied_current) == (False, 2)
    assert allowed_after_release is True


@pytest.mark.anyio
async def test_in_memory_store_can_simulate_unavailable_redis() -> None:
    """异常：测试 store 可以模拟 Redis 不可用，供数据面 fail closed 流程覆盖。"""

    store = InMemoryRateLimitStore(available=False)
    with pytest.raises(RateLimitStoreUnavailable):
        await store.ping()


def test_estimate_request_tokens_counts_chat_and_embedding_inputs() -> None:
    """TPM 估算：只按长度估算，不返回或记录 Prompt 明文。"""

    chat_tokens = estimate_request_tokens(
        "chat",
        {"messages": [{"role": "user", "content": "12345678"}], "max_tokens": 3},
    )
    embedding_tokens = estimate_request_tokens("embeddings", {"input": ["1234", "5678"]})
    assert chat_tokens == 5
    assert embedding_tokens == 2
