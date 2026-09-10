"""Cooperative restore cancellation without logging subprocess output or credentials."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class RestoreStopped(Exception):
    def __init__(self, code: str = "backup.restore_cancelled"):
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class RestoreControl:
    stopping: Callable[[], bool] = field(repr=False)
    deadline: float = field(default_factory=lambda: time.monotonic() + 7200)
    child_stop_confirmed: bool = field(default=True, init=False)

    def check(self) -> None:
        if self.stopping():
            raise RestoreStopped()
        if time.monotonic() >= self.deadline:
            raise RestoreStopped("backup.restore_timeout")

    def run(self, arguments: list[str], *, env: dict[str, str]) -> None:
        self.check()
        process = subprocess.Popen(
            arguments,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.child_stop_confirmed = False
        try:
            while True:
                self.check()
                try:
                    code = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            self.check()
            if code:
                raise RestoreStopped("backup.restore_failed")
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    raise RestoreStopped("backup.restore_stop_unconfirmed") from None
            self.child_stop_confirmed = True


__all__ = ["RestoreControl", "RestoreStopped"]
