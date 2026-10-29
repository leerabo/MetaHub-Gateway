"""Linux image entry point. Stdio is a private protocol, never a console or shell."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from .guest_runtime import run_guest
from .lease_channel import StreamLeaseTransport


async def _main(input_pipe, output_pipe):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=4100)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), input_pipe
    )
    try:
        write_transport, protocol = await loop.connect_write_pipe(
            lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()),
            output_pipe,
        )
        writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
        transport = StreamLeaseTransport(reader, writer)
        try:
            return await run_guest(
                transport,
                Path("/app/assurance/base.sqlite3"),
                Path("/tmp"),
                Path("/app/infra/migrations"),
            )
        finally:
            transport.close()
    finally:
        read_transport.close()


def main() -> int:
    if sys.platform != "linux" or len(sys.argv) != 1:
        return 1
    # Keep private descriptors before suppressing any application/library output.
    with (
        os.fdopen(os.dup(0), "rb", buffering=0) as input_pipe,
        os.fdopen(os.dup(1), "wb", buffering=0) as output_pipe,
    ):
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            try:
                return asyncio.run(_main(input_pipe, output_pipe))
            except BaseException:
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
