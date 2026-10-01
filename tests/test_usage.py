import asyncio
import json
from datetime import datetime

from aiohttp.test_utils import TestClient, TestServer

from mcchat.bridge import BridgeConfig, ChatBridge
from mcchat.config import Settings
from mcchat.runtime import Runtime
from mcchat.usage import BUILD, CHAT, VILLAGE, Prices, UsageLedger, UsageRecord, count_operations, usage_context
from mcchat.webapp import create_app
from test_village import run_with_minecraft

PRICES = Prices(1.0, 10.0, "£")


def test_prices():
    assert PRICES.cost(1_000_000, 0) == 1.0 and PRICES.cost(0, 100_000) == 1.0


def test_usage_context_tags_calls_and_tasks(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.jsonl")

    async def scenario():
        with usage_context("Steve", VILLAGE):
            # Parallel builder agents inherit the context.
            await asyncio.gather(*(asyncio.sleep(0, result=ledger.record("m", 100, 10)) for _ in range(3)))
            await asyncio.gather(*(asyncio.create_task(asyncio.to_thread(ledger.record, "m", 1, 1)) for _ in range(2)))
        ledger.record("m", 5, 5)

    asyncio.run(scenario())
    assert [(r.player, r.task) for r in ledger.records] == [("Steve", VILLAGE)] * 5 + [("(server)", "Other")]
    reloaded = UsageLedger(tmp_path / "usage.jsonl")
    assert len(reloaded.records) == 6, "usage survives a restart"
    reloaded.clear()
    assert reloaded.records == [] and not (tmp_path / "usage.jsonl").exists()


def test_count_operations():
    def rec(t, player, task):
        return UsageRecord(t, player, task, "m", 1, 1)

    records = [
        rec(0, "Steve", BUILD), rec(5, "Steve", BUILD),        # routing + design = one build
        rec(100, "Steve", BUILD),                              # a second build later
        rec(0, "Alex", VILLAGE), *[rec(20 * i, "Alex", VILLAGE) for i in range(1, 9)],  # one long village
        rec(1, "Steve", CHAT), rec(2, "Steve", CHAT),          # every chat message counts
    ]
    assert count_operations(records) == {BUILD: 2, VILLAGE: 1, CHAT: 2}


def test_summary(tmp_path):
    ledger = UsageLedger(None)
    now = datetime(2026, 10, 7, 15, 0).timestamp()
    old = datetime(2026, 9, 1, 12, 0).timestamp()
    ledger.records = [
        UsageRecord(now - 60, "Steve", CHAT, "gpt", 1000, 100),
        UsageRecord(now - 30, "Alex", BUILD, "gpt", 2000, 1000),
        UsageRecord(old, "Steve", CHAT, "gpt", 1000, 100),
    ]
    week = ledger.summary(PRICES, days=7, now=now)
    assert week["currency"] == "£" and week["totals"]["requests"] == 2 and week["totals"]["players"] == 2
    assert round(week["totals"]["cost"], 6) == round((3000 * 1 + 1100 * 10) / 1e6, 6)
    assert [d["name"] for d in week["by_day"]] == [f"2026-10-0{d}" for d in range(1, 8)]
    assert week["by_day"][-1]["requests"] == 2 and week["by_day"][0]["requests"] == 0
    assert [r["name"] for r in week["by_task"]] == [BUILD, CHAT], "most expensive first"
    assert week["by_task"][0]["operations"] == 1 and week["by_task"][0]["cost_per_operation"] == week["by_task"][0]["cost"]
    everything = ledger.summary(PRICES, now=now)
    assert everything["totals"]["requests"] == 3 and everything["by_day"][0]["name"] == "2026-09-01"


class MeteredLLM:
    """Behaves like AzureFoundryLLM: reports token usage for each call."""

    def __init__(self, ledger: UsageLedger):
        self.ledger = ledger

    async def complete(self, messages):
        self.ledger.record("gpt-test", 1200, 30)
        return "Hello!"


def test_chat_usage_is_billed_to_the_player():
    ledger = UsageLedger(None)
    bridge = ChatBridge(MeteredLLM(ledger), BridgeConfig())

    async def script(mc):
        await mc.chat("Steve", "hi")
        await mc.wait_for("Hello!")
        await mc.chat("Steve", "!clear")
        await mc.wait_for("Conversation cleared.")
        await mc.chat("Steve", "!dance")
        await mc.wait_for("I don't know !dance. Type !help")

    run_with_minecraft(bridge, script)
    assert [(r.player, r.task) for r in ledger.records] == [("Steve", CHAT)], "unknown commands never reach the AI"


def test_usage_api_and_price_settings(tmp_path):
    settings = Settings(azure_endpoint="https://x.openai.azure.com/", azure_api_key="k", azure_model="gpt-test")
    ledger = UsageLedger(tmp_path / "usage.jsonl")
    runtime = Runtime(settings, MeteredLLM(ledger), env_path=tmp_path / ".env", rubrics_dir=tmp_path / "r", usage=ledger)
    with usage_context("Steve", CHAT):
        ledger.record("gpt-test", 1_000_000, 100_000)

    async def scenario():
        async with TestClient(TestServer(create_app(runtime))) as client:
            before = await (await client.get("/api/usage?days=7")).json()
            saved = await client.post("/api/settings", json={"price_input_per_million": 2, "price_output_per_million": 20, "currency": "£"})
            after = await (await client.get("/api/usage")).json()
            bad = await client.post("/api/settings", json={"price_input_per_million": -1})
            bad_days = await client.get("/api/usage?days=x")
            cleared = await client.delete("/api/usage")
            empty = await (await client.get("/api/usage")).json()
            return before, saved.status, after, bad.status, bad_days.status, cleared.status, empty

    before, saved, after, bad, bad_days, cleared, empty = asyncio.run(scenario())
    assert before["totals"]["cost"] == 1.25 + 1.0 and before["model"] == "gpt-test"
    assert saved == 200 and after["currency"] == "£" and after["totals"]["cost"] == 2 + 2
    assert after["by_player"][0]["name"] == "Steve" and after["by_task"][0]["name"] == CHAT
    assert bad == 400 and bad_days == 400 and cleared == 200 and empty["totals"]["requests"] == 0
    env = (tmp_path / ".env").read_text()
    assert "PRICE_INPUT_PER_M=2" in env and "PRICE_OUTPUT_PER_M=20" in env


def test_cost_page_present(tmp_path):
    runtime = Runtime(Settings(), None, env_path=tmp_path / ".env", rubrics_dir=tmp_path / "r")

    async def scenario():
        async with TestClient(TestServer(create_app(runtime))) as client:
            return await (await client.get("/")).text()

    html = asyncio.run(scenario())
    assert 'data-tab="costs"' in html and "Class cost planner" in html
