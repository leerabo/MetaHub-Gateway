"""Bounded HTTP transport shared by fixture creation and fixed probes."""

from __future__ import annotations

import asyncio

import httpx

MAX_RESPONSE_BYTES = 262_144
REQUEST_TIMEOUT_SECONDS = 10


async def guarded_request(check_lease, client, method, path, **kwargs):
    headers = {**kwargs.pop("headers", {}), "Accept-Encoding": "identity"}
    async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
        await check_lease()
        async with client.stream(
            method,
            path,
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers=headers,
            **kwargs,
        ) as response:
            await check_lease()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("unsupported probe response encoding")
            length = response.headers.get("content-length")
            if length is not None and not 0 <= int(length) <= MAX_RESPONSE_BYTES:
                raise ValueError("probe response too large")
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=16_384):
                await check_lease()
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("probe response too large")
                content.extend(chunk)
            await check_lease()
            return httpx.Response(
                response.status_code, headers=response.headers, content=bytes(content)
            )
