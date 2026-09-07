"""Windows-capable test launcher for the fixed guest workload, not a Docker image."""

import asyncio
import sys
import traceback
from pathlib import Path

from metahub_assurance_runner import guest_runtime
from prd137_lease_peer import TestPipeTransport

_execute = guest_runtime.execute_workload


async def diagnosed(*args):
    try:
        return await _execute(*args)
    except Exception as error:
        # Test diagnostics contain code locations, never exception values or locals.
        print(type(error).__name__, file=sys.stderr)
        for frame in traceback.extract_tb(error.__traceback__):
            print(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}", file=sys.stderr)
        raise


guest_runtime.execute_workload = diagnosed

if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(
            guest_runtime.run_guest(
                TestPipeTransport(),
                Path(sys.argv[1]),
                Path(sys.argv[2]),
                Path(sys.argv[3]),
            )
        )
    )
