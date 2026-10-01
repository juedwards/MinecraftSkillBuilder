import asyncio
import json

from mcchat.bridge import BridgeConfig, ChatBridge
from mcchat.progress import Progress, format_elapsed
from mcchat.realworld import MapRequest
from mcchat.router import parse_route
from test_realworld import FakeMap
from test_village import run_with_minecraft


# --- Progress ----------------------------------------------------------------------------

def test_format_elapsed():
    assert (format_elapsed(5), format_elapsed(65), format_elapsed(600)) == ("5s", "1m 05s", "10m 00s")


def test_progress_heartbeats_and_percentages():
    said, beats = [], []

    async def scenario():
        async def say(text):
            said.append(text)

        async def heartbeat(text):
            beats.append(text)

        async with Progress(say, interval=0.05, heartbeat=heartbeat) as progress:
            progress.stage("Placing blocks", total=200)
            progress.tick(50)
            await asyncio.sleep(0.08)
            await progress.say("Built 5 of 20 buildings...")  # a real message holds off the next heartbeat
            await asyncio.sleep(0.03)
            beats_before = len(beats)
            await asyncio.sleep(0.06)
        return beats_before

    beats_before = asyncio.run(scenario())
    assert said == ["Built 5 of 20 buildings..."]
    assert beats and beats[0].startswith("Placing blocks: 25% (50/200)... (")
    assert beats_before == 1 and len(beats) == 2


def test_silent_progress_does_nothing():
    async def scenario():
        async with Progress.silent() as progress:
            progress.stage("Anything", total=3)
            progress.tick()
            await progress.say("ignored")
            return progress.message()

    assert asyncio.run(scenario()).startswith("Anything: 33% (1/3)")


class SlowLLM:
    async def complete(self, messages):
        await asyncio.sleep(0.25)
        return "Here's my answer."


def test_slow_answers_get_grey_progress_updates():
    bridge = ChatBridge(SlowLLM(), BridgeConfig(progress_interval=0.1))

    async def script(mc):
        await mc.chat("Steve", "why is the sky blue?")
        await mc.wait_for("Here's my answer.")

    mc = run_with_minecraft(bridge, script)
    thinking = [r for r in mc.replies if "Thinking..." in r]
    assert thinking and all(r.startswith("§7[AI] ") for r in thinking), "progress updates are grey"
    assert mc.replies[-1] == "§b[AI]§r Here's my answer."


# --- Routing -------------------------------------------------------------------------------

def test_parse_route():
    place = parse_route('{"kind": "place", "query": "Tower of London, London, UK", "size": 500, "scale": null}', "the tower of london")
    assert place.kind == "place" and place.map_request == MapRequest("Tower of London, London, UK", 128, 2.0)
    design = parse_route('Sure: {"kind": "design", "request": "a castle inspired by the Tower of London"}', "x")
    assert (design.kind, design.request, design.map_request) == ("design", "a castle inspired by the Tower of London", None)
    assert parse_route("not json", "a rocket").request == "a rocket", "unreadable answers mean: design it"
    assert parse_route('{"kind": "place", "query": ""}', "a rocket").kind == "design"
    no_bridges = parse_route('{"kind": "place", "query": "Bath", "bridges": false, "scale": 3}', "bath")
    assert no_bridges.map_request == MapRequest("Bath", 96, 3.0, bridges=False)


class RouterLLM:
    """Routes, then designs; records what kind of prompts it received."""

    def __init__(self, route: dict):
        self.route = route
        self.calls: list[str] = []

    async def complete(self, messages):
        system = messages[0]["content"]
        if system.startswith("You route build requests"):
            self.calls.append("route")
            return json.dumps(self.route)
        self.calls.append("design")
        return '<code>function buildCreation(x, y, z) { safeFill(x, y, z, x + 2, y + 2, z + 2, "stone"); }</code>'


def test_build_routes_real_places_to_the_map():
    llm = RouterLLM({"kind": "place", "query": "Tower of London, London", "size": 40, "scale": 2})
    source = FakeMap()
    bridge = ChatBridge(llm, BridgeConfig(), map_source=source)

    async def script(mc):
        await mc.chat("Steve", "!build the tower of london")
        await mc.wait_for("Map data © OpenStreetMap contributors", timeout=20)

    run_with_minecraft(bridge, script)
    assert llm.calls == ["route"] and source.queries == ["Tower of London, London"]


def test_build_routes_designs_to_the_designer():
    llm = RouterLLM({"kind": "design", "request": "a stone cube"})
    source = FakeMap()
    bridge = ChatBridge(llm, BridgeConfig(), map_source=source)

    async def script(mc):
        await mc.chat("Steve", "!build a cube like the tower of london")
        await mc.wait_for("Built a stone cube (3x3x3", timeout=20)

    run_with_minecraft(bridge, script)
    assert llm.calls == ["route", "design"] and source.queries == []
