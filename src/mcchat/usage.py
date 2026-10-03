"""AI usage and cost tracking.

Every AI call records its input and output tokens, tagged with the player and task it was for.
The tags come from `usage_context()`, which uses a context variable, so they carry into parallel
work automatically (e.g. a village's builder agents). Records are appended to a JSON-lines file
so totals survive restarts; costs are computed from editable prices per million tokens.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

USAGE_FILE = Path("usage.jsonl")

# Task types shown in the web interface.
CHAT, BUILD, MAP, VILLAGE, CHALLENGE_SETUP, CHALLENGE_FEEDBACK, SETUP, TEACHER, OTHER = (
    "Chat", "Build", "Map", "Village", "Challenge setup", "Challenge feedback", "Setup", "Teacher assistant", "Other",
)

_current: ContextVar[tuple[str, str]] = ContextVar("usage_context", default=("(server)", OTHER))


@contextmanager
def usage_context(player: str, task: str) -> Iterator[None]:
    """AI calls inside this block (and tasks started from it) are billed to `player` and `task`."""
    token = _current.set((player, task))
    try:
        yield
    finally:
        _current.reset(token)


@dataclass(frozen=True)
class UsageRecord:
    time: float
    player: str
    task: str
    model: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Prices:
    input_per_million: float
    output_per_million: float
    currency: str = "$"

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_million + output_tokens * self.output_per_million) / 1_000_000


def _group(records: list[UsageRecord], key, prices: Prices) -> list[dict[str, Any]]:
    groups: dict[str, list[UsageRecord]] = defaultdict(list)
    for record in records:
        groups[key(record)].append(record)
    rows = []
    for name, items in groups.items():
        tokens_in = sum(r.input_tokens for r in items)
        tokens_out = sum(r.output_tokens for r in items)
        rows.append({
            "name": name, "requests": len(items), "input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost": prices.cost(tokens_in, tokens_out),
        })
    return sorted(rows, key=lambda row: -row["cost"])


OPERATION_GAP = 45.0  # seconds: a longer pause between a player's calls for a task starts a new operation


def count_operations(records: list[UsageRecord]) -> dict[str, int]:
    """Operations per task. Every chat message is one; other calls group while they keep coming."""
    counts: dict[str, int] = defaultdict(int)
    last: dict[tuple[str, str], float] = {}
    for r in sorted(records, key=lambda r: r.time):
        key = (r.player, r.task)
        if r.task == CHAT or key not in last or r.time - last[key] > OPERATION_GAP:
            counts[r.task] += 1
        last[key] = r.time
    return dict(counts)


class UsageLedger:
    def __init__(self, path: Path | None = USAGE_FILE):
        self.path = path
        self.records: list[UsageRecord] = []
        if path is not None and path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self.records.append(UsageRecord(**json.loads(line)))
                except (ValueError, TypeError):
                    continue  # skip a damaged line rather than losing the rest

    def record(self, model: str, input_tokens: int, output_tokens: int) -> UsageRecord:
        player, task = _current.get()
        entry = UsageRecord(time.time(), player, task, model, int(input_tokens or 0), int(output_tokens or 0))
        self.records.append(entry)
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(asdict(entry)) + "\n")
            except OSError:
                log.warning("Couldn't save usage to %s", self.path, exc_info=True)
        return entry

    def clear(self) -> None:
        self.records.clear()
        if self.path is not None and self.path.exists():
            self.path.unlink()

    def summary(self, prices: Prices, days: int | None = None, now: float | None = None) -> dict[str, Any]:
        """Totals, breakdowns by task / player / model / day, and the average cost per task (for estimates)."""
        now = now or time.time()
        records = self.records
        if days:
            start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
            records = [r for r in records if r.time >= start.timestamp()]
        tokens_in = sum(r.input_tokens for r in records)
        tokens_out = sum(r.output_tokens for r in records)
        total = prices.cost(tokens_in, tokens_out)

        by_day = {row["name"]: row for row in _group(records, lambda r: datetime.fromtimestamp(r.time).strftime("%Y-%m-%d"), prices)}
        first_day = (
            datetime.fromtimestamp(now) - timedelta(days=days - 1) if days
            else datetime.fromtimestamp(min((r.time for r in records), default=now))
        )
        days_list = []
        day = first_day.replace(hour=0, minute=0, second=0, microsecond=0)
        while day.date() <= datetime.fromtimestamp(now).date():
            key = day.strftime("%Y-%m-%d")
            days_list.append(by_day.get(key, {"name": key, "requests": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}))
            day += timedelta(days=1)

        # Average per *operation*: one !build is a routing call plus a design call, one village is 20+ calls.
        by_task = _group(records, lambda r: r.task, prices)
        operations = count_operations(records)
        for row in by_task:
            count = max(1, operations.get(row["name"], 0))
            row["operations"] = count
            row["cost_per_operation"] = row["cost"] / count

        return {
            "currency": prices.currency,
            "prices": {"input_per_million": prices.input_per_million, "output_per_million": prices.output_per_million},
            "totals": {
                "requests": len(records), "input_tokens": tokens_in, "output_tokens": tokens_out, "cost": total,
                "players": len({r.player for r in records}),
            },
            "by_task": by_task,
            "by_player": _group(records, lambda r: r.player, prices),
            "by_model": _group(records, lambda r: r.model, prices),
            "by_day": days_list,
        }
