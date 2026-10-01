import asyncio
import json

from websockets.asyncio.client import connect

from mcchat.assessment import RubricStore
from mcchat.bridge import BridgeConfig, ChatBridge
from mcchat.builder import BuildOp
from mcchat.minecraft import MinecraftServer
from mcchat.village import (
    PITCH, PLOT, BuildingPlan, VillagePlan, choose_plots, lay_out, parse_plan, parse_village_args,
)
from test_assessment import FakeMinecraft

PLAN = VillagePlan("Testville", "test", "grass_block", "cobblestone", [BuildingPlan(f"B{n}", "a hut") for n in range(9)])


def box(w: int, d: int, h: int = 4, block: str = "oak_planks") -> list[BuildOp]:
    return [BuildOp(0, 0, 0, w - 1, h - 1, d - 1, block, "hollow")]


def overlaps(a: BuildOp, b: BuildOp) -> bool:
    return a.x1 <= b.x2 and b.x1 <= a.x2 and a.z1 <= b.z2 and b.z1 <= a.z2


def test_parse_plan_validates_blocks_and_count():
    reply = 'Sure! {"name": "Fjord", "style": "viking", "ground_block": "sand", "path_block": "minecraft:spruce_planks",' \
            ' "buildings": [{"name": "Hall", "description": "big"}, {"name": "Hut", "description": "small"}, {"oops": 1}, "x"]}'
    plan = parse_plan(reply, 2)
    assert (plan.name, plan.style) == ("Fjord", "viking")
    assert plan.ground_block == "grass_block", "sand would fall, so the fallback is used"
    assert plan.path_block == "spruce_planks"
    assert [b.name for b in plan.buildings] == ["Hall", "Hut"]


def test_parse_village_args():
    assert parse_village_args("viking") == ("viking", 20)
    assert parse_village_args("japanese 12") == ("japanese", 12)
    assert parse_village_args("") == ("", 20)
    assert parse_village_args("tiny 2") == ("tiny", 4)
    assert parse_village_args("huge 99") == ("huge", 24)


def test_choose_plots_rings_and_reserve():
    plots = choose_plots(20, (0, 0))
    cells = [(p.i, p.j) for p in plots]
    assert len(set(cells)) == 20 and (0, 0) not in cells
    assert sum(max(abs(i), abs(j)) == 1 for i, j in cells) == 8, "the inner ring is filled first"
    reserved = choose_plots(5, (100, 200), reserve=(0, 1))
    assert (reserved[0].i, reserved[0].j) == (0, 1) and len(reserved) == 6
    assert reserved[0].centre == (100, 200 + PITCH) and reserved[0].facing == (0, 1)


def test_lay_out_geometry():
    designs = [(PLAN.buildings[0], box(5, 5), "")] + [(b, box(PLOT, 9), "") for b in PLAN.buildings[1:8]] + [(PLAN.buildings[8], None, "boom")]
    village = lay_out(PLAN, designs, feet=(0, 64, 0), facing=(0, 1), reserve_plot=True)

    assert village.ground == 63 and len(village.built) == 8 and village.failed == ["B8 (boom)"]
    assert len(village.building_commands) == 8 and len(village.villagers) == 7
    footprints = [op for op in village.world_ops[1:] if op.block == "oak_planks"]
    assert len(footprints) == 8
    for n, a in enumerate(footprints):
        assert all(not overlaps(a, b) for b in footprints[n + 1:]), "buildings never overlap"
        assert village.area.x1 <= a.x1 and a.x2 <= village.area.x2 and village.area.z1 <= a.z1 and a.z2 <= village.area.z2

    free = village.plot_area(village.free_plot)
    assert village.free_plot.centre == (0, PITCH), "the empty plot is right in front of the player"
    assert not any(overlaps(free, op) for op in footprints), "the reserved plot stays empty"
    sx, sy, sz = village.spawn
    assert sy == 64 and not any(op.x1 <= sx <= op.x2 and op.z1 <= sz <= op.z2 for op in footprints)
    centrepiece = footprints[0]
    assert (centrepiece.x1, centrepiece.z1, centrepiece.x2, centrepiece.z2) == (-2, -2, 2, 2)
    for x, y, z in village.villagers:
        assert y == 64 and not any(op.x1 <= x <= op.x2 and op.z1 <= z <= op.z2 for op in footprints)


HUT = """<code>
function buildCreation(x, y, z) {
  safeFill(x, y, z, x + 4, y + 3, z + 4, "oak_planks", { mode: "hollow" });
  safeSetBlock(x + 2, y + 1, z, "air");
}
</code>"""


class VillageLLM:
    def __init__(self, buildings: int):
        self.buildings = buildings
        self.builder_calls = 0

    async def complete(self, messages):
        system = messages[0]["content"]
        if system.startswith("You plan villages"):
            return json.dumps({
                "name": "Testville", "style": "test", "ground_block": "grass_block", "path_block": "cobblestone",
                "buildings": [{"name": f"House {n}", "description": "a hut"} for n in range(self.buildings)],
            })
        if "formative assessment" in system:
            return '{"summary": "Nice home.", "criteria": [], "strengths": [], "next_steps": []}'
        self.builder_calls += 1
        return HUT


def run_with_minecraft(bridge: ChatBridge, script):
    mc = FakeMinecraft()

    async def scenario():
        server = MinecraftServer("127.0.0.1", 0, bridge.handle_chat, on_game_event=bridge.handle_game_event)
        await server.start()
        try:
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                mc.ws = ws
                serving = asyncio.create_task(mc.serve())
                await script(mc)
                serving.cancel()
        finally:
            await server.close()

    asyncio.run(scenario())
    return mc


def test_village_end_to_end():
    llm = VillageLLM(6)
    events = []
    bridge = ChatBridge(llm, BridgeConfig(), on_event=events.append)

    async def script(mc):
        await mc.chat("Steve", "!village test 6")
        await mc.wait_for("Welcome to Testville! 6 buildings and 5 villagers.", timeout=20)

    mc = run_with_minecraft(bridge, script)
    assert llm.builder_calls == 6
    summons = [c for c in mc.commands if c.startswith("summon ")]
    assert len(summons) == 5 and all(c.startswith("summon villager_v2 ") for c in summons)
    tp = next(c for c in mc.commands if c.startswith("tp "))
    assert mc.commands.index(tp) < mc.commands.index(summons[0]), "the player is moved before buildings go up"
    # Player feet (10, -60, 20), facing +Z: the square (in front of the centrepiece) and streets are cobblestone.
    assert mc.world[(10, -61, 15)] == "cobblestone" and mc.world[(10 + PITCH // 2, -61, 40)] == "cobblestone"
    assert mc.world[(10, -61, 20)] == "oak_planks", "the centrepiece stands in the middle of the square"
    assert 'tp "Steve" 10.5 -60 15.5 facing 10.5 -59 20.5' == tp
    assert sum(1 for b in mc.world.values() if b == "oak_planks") > 0
    assert bridge.building is None
    assert any(e["type"] == "build" and e["status"].startswith("village: done") for e in events)


def test_help_lists_every_command():
    bridge = ChatBridge(VillageLLM(1), BridgeConfig(), rubrics=RubricStore(__import__("pathlib").Path("rubrics")))
    text = bridge.help_text()
    for command in ("!build", "!village", "!challenge", "!reset", "!setup", "!help", "finished"):
        assert command in text


def test_assessment_with_starter_village(tmp_path):
    store = RubricStore(tmp_path / "rubrics")
    store.save("home", "# Village Home\n\n## Task\n\nBuild a home on the empty plot.\n\n## Starter village\n\nA test village.\n")
    llm = VillageLLM(12)
    bridge = ChatBridge(llm, BridgeConfig(), rubrics=store, reports_dir=tmp_path / "reports")

    async def script(mc):
        await mc.chat("Steve", "!challenge")
        await mc.wait_for("1. Village Home")
        await mc.chat("Steve", "1")
        i = await mc.wait_for("Your task: Build a home on the empty plot.", timeout=20)
        # Facing south (+Z) from (10, 20): the empty plot is one cell ahead, centred on (10, 20 + PITCH).
        mc.world[(10, -60, 20 + PITCH)] = "oak_planks"
        await mc.chat("Steve", "finished")
        await mc.wait_for("Would you like to try again?", i)
        await mc.chat("Steve", "yes")
        await mc.wait_for("Your task:", i + 1)
        await mc.chat("Steve", "!cancel")
        await mc.wait_for("Challenge stopped")

    mc = run_with_minecraft(bridge, script)
    summons = [c for c in mc.commands if c.startswith("summon ")]
    assert len(summons) == 11, "villagers are summoned on the first attempt only"
    tps = [c for c in mc.commands if c.startswith("tp ")]
    start_z = 20 + PITCH - PLOT // 2 - 2
    assert f'tp "Steve" 10.5 -60 {start_z}.5 facing 10.5 -60 {20 + PITCH}.5' in tps
    assert llm.builder_calls == 12, "the village is designed once, not again for the retry"
