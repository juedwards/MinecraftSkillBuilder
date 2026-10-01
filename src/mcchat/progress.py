"""Keeps players informed during long operations.

A `Progress` runs alongside slow work (AI calls, map downloads, placing thousands of blocks).
If nothing has been said for `interval` seconds, it says what's happening now, with a
percentage when the step can be counted, so players know the server is working for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 10.0  # seconds of silence before a progress update

Say = Callable[[str], Awaitable[None]]


def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


class Progress:
    """Use as `async with Progress(say) as progress:`; call `stage()` as the work moves on.

    `say` sends normal messages and resets the timer; `heartbeat` (defaults to `say`) sends the
    periodic updates. With `say=None` it does nothing, so callees can always accept one.
    """

    def __init__(self, say: Say | None, interval: float = PROGRESS_INTERVAL, heartbeat: Say | None = None):
        self._say = say
        self._heartbeat = heartbeat or say
        self.interval = interval
        self._stage = "Working on it"
        self._total = 0
        self._done = 0
        self._started = self._last_said = time.monotonic()
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def silent(cls) -> Progress:
        return cls(None)

    def stage(self, text: str, total: int = 0) -> None:
        """Start a new step; `total` > 0 makes it countable with `tick()`."""
        self._stage, self._total, self._done = text, total, 0

    def tick(self, n: int = 1) -> None:
        self._done += n

    def message(self) -> str:
        text = self._stage
        if self._total:
            done = min(self._done, self._total)
            text += f": {done * 100 // self._total}% ({done}/{self._total})"
        return f"{text}... ({format_elapsed(time.monotonic() - self._started)})"

    async def say(self, text: str) -> None:
        """Send a normal message (and hold off the next heartbeat)."""
        self._last_said = time.monotonic()
        if self._say is not None:
            await self._say(text)

    async def __aenter__(self) -> Progress:
        self._started = self._last_said = time.monotonic()
        if self._heartbeat is not None and self.interval > 0:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        assert self._heartbeat is not None
        while True:
            wait = self._last_said + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            self._last_said = time.monotonic()
            try:
                await self._heartbeat(self.message())
            except Exception:
                log.debug("Progress update failed", exc_info=True)
