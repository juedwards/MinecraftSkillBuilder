import asyncio
import json
import re
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from mcchat.assessment import (
    RubricError, RubricStore, clip_area, expected_blocks, feedback_chat, parse_feedback, render_inspection,
    rubric_section,
)
from mcchat.bridge import BridgeConfig, ChatBridge
from mcchat.builder import BuildOp, run_design_script
from mcchat.minecraft import MinecraftServer, parse_testforblock

REPO_RUBRICS = Path(__file__).resolve().parent.parent / "rubrics"

TEST_RUBRIC = """# Test Bridge

## Task

Build a bridge across the river.

## Assessment criteria

| Criterion | Beginning | Secure |
|---|---|---|
| Spanning the gap | Does not reach | Reaches the far bank |
"""


# --- Rubrics ----------------------------------------------------------------------------

def test_rubric_store_crud(tmp_path):
    store = RubricStore(tmp_path / "rubrics")
    assert store.list() == []
    saved = store.save("test_bridge", TEST_RUBRIC)
    assert (saved.id, saved.title) == ("test_bridge", "Test Bridge")
    assert [r.id for r in store.list()] == ["test_bridge"]
    assert store.get("test_bridge").text.startswith("# Test Bridge")
    assert store.delete("test_bridge") and store.get("test_bridge") is None


@pytest.mark.parametrize("bad_id", ["../secrets", "Has Space", "UPPER", "", "a/b", "x" * 80])
def test_rubric_ids_are_validated(tmp_path, bad_id):
    store = RubricStore(tmp_path)
    with pytest.raises(RubricError):
        store.save(bad_id, TEST_RUBRIC)
    assert store.get(bad_id) is None


def test_demo_rubric_is_complete():
    rubric = RubricStore(REPO_RUBRICS).get("build_a_bridge")
    assert rubric is not None and rubric.title == "Build a Bridge"
    for section in ("Learning aims", "Learning objectives", "Task", "Starter build", "Assessment criteria"):
        assert rubric_section(rubric.text, section), section


def test_new_york_rubric_is_complete():
    rubric = RubricStore(REPO_RUBRICS).get("new_york_skyscraper")
    assert rubric is not None and rubric.title == "New York Skyline: Design an Art Deco Skyscraper"
    for section in ("Learning aims", "Learning objectives", "Task", "Starter build", "Assessment criteria"):
        assert rubric_section(rubric.text, section), section
    assert "10 x 10" in rubric_section(rubric.text, "Starter build")


def test_rubric_section():
    assert rubric_section(TEST_RUBRIC, "task") == "Build a bridge across the river."
    assert rubric_section(TEST_RUBRIC, "Missing") == ""


# --- Inspection helpers -----------------------------------------------------------------

def test_parse_testforblock():
    assert parse_testforblock({"statusCode": 0, "statusMessage": "Successfully found the block"}) == "air"
    assert parse_testforblock({"statusCode": -2147483648, "statusMessage": "The block at 1,2,3 is Oak Planks (expected: Air)."}) == "oak_planks"
    assert parse_testforblock({"statusCode": -1, "statusMessage": "The block at 1,2,3 is minecraft:stone."}) == "stone"
    assert parse_testforblock({"statusCode": -1, "statusMessage": "Cannot test for block outside of the world"}) == "unknown"


def test_design_script_marks():
    design = run_design_script(
        "function buildCreation(x,y,z){ safeFill(0,0,0,4,0,4,'stone'); markPlayerStart(2,1,0); markTaskArea(4,3,4,1,0,1); }"
    )
    assert design.start == (2, 1, 0)
    assert design.area == BuildOp(1, 0, 1, 4, 3, 4, "air")


def test_expected_blocks_modes():
    area = BuildOp(0, 0, 0, 2, 2, 0, "air")
    ops = [
        BuildOp(0, 0, 0, 2, 0, 0, "stone"),
        BuildOp(0, 1, 0, 2, 2, 0, "glass", "outline"),  # 3x2 slab: every cell is on the border
        BuildOp(1, 1, 0, 1, 1, 0, "dirt", "keep"),  # glass already there: kept
    ]
    grid = expected_blocks(ops, area)
    assert grid[(1, 0, 0)] == "stone" and grid[(1, 1, 0)] == "glass" and grid[(0, 2, 0)] == "glass"
    hollow = expected_blocks([BuildOp(0, 0, 0, 2, 2, 2, "stone", "hollow")], BuildOp(0, 0, 0, 2, 2, 2, "air"))
    assert hollow[(1, 1, 1)] == "air" and hollow[(0, 1, 1)] == "stone"
    assert expected_blocks([], area)[(0, 0, 0)] == "?"


def test_clip_area_limits_volume():
    clipped = clip_area(BuildOp(0, 0, 0, 39, 19, 39, "air"), limit=5000)
    volume = (clipped.x2 - clipped.x1 + 1) * (clipped.y2 - clipped.y1 + 1) * (clipped.z2 - clipped.z1 + 1)
    assert volume <= 5000 and clipped.y1 == 0


def test_render_inspection_marks_changes():
    area = BuildOp(0, 0, 0, 2, 1, 0, "air")
    expected = {(0, 0, 0): "stone", (1, 0, 0): "stone", (2, 0, 0): "air", (0, 1, 0): "air", (1, 1, 0): "air", (2, 1, 0): "air"}
    found = dict(expected) | {(2, 0, 0): "oak_planks", (1, 0, 0): "air"}
    text = render_inspection(area, found, expected, (0, 1, -1))
    assert "added: oak_planks x1" in text and "removed: stone x1" in text
    assert "Layer 1 (y=1): all air" in text
    assert "z 0 A.B   |  -+" in text  # x0 unchanged, x1 removed, x2 added


def test_render_inspection_separates_game_physics():
    area = BuildOp(0, 0, 0, 5, 0, 0, "air")
    expected = {(0, 0, 0): "sand", (1, 0, 0): "air", (2, 0, 0): "grass_block", (3, 0, 0): "water", (4, 0, 0): "water", (5, 0, 0): "air"}
    found = {(0, 0, 0): "water", (1, 0, 0): "water", (2, 0, 0): "dirt", (3, 0, 0): "stone", (4, 0, 0): "air", (5, 0, 0): "oak_planks"}
    text = render_inspection(area, found, expected, (0, 1, -1))
    assert "added: oak_planks x1" in text
    assert "replaced: water -> stone x1" in text, "a pillar placed in water is the student's work"
    assert "sand -> water x1" in text and "air -> water x1" in text and "grass_block -> dirt x1" in text
    assert "water -> air x1" in text.split("game physics, not the student")[1]
    assert "z 0 ~~AB.C   | %%%*%+" in text


def test_parse_feedback_and_chat():
    reply = 'Here you go: {"summary": "Nice start.", "criteria": [{"name": "Span", "level": "Secure", "evidence": "Reaches."}], "strengths": ["Neat"], "next_steps": ["Add railings", "Add pillars"]}'
    feedback = parse_feedback(reply)
    assert feedback.summary == "Nice start." and feedback.criteria[0]["level"] == "Secure"
    chat = feedback_chat(feedback)
    assert "- Span: Secure. Reaches." in chat and "To do better: 1) Add railings 2) Add pillars" in chat
    assert parse_feedback("not json at all").summary == "not json at all"


# --- End to end -------------------------------------------------------------------------

STARTER_REPLY = """<description>A small river scene.</description>
<task>Build a bridge across the river to the far bank.</task>
<code>
function buildCreation(x, y, z) {
  safeFill(x, y, z, x + 8, y, z + 8, "grass_block");
  safeFill(x, y - 2, z + 3, x + 8, y, z + 5, "water");
  markPlayerStart(x + 4, y + 1, z + 1);
  markTaskArea(x + 2, y, z + 2, x + 6, y + 3, z + 6);
}
</code>"""

FEEDBACK_REPLY = json.dumps({
    "summary": "You reached the far bank.",
    "criteria": [{"name": "Spanning the gap", "level": "Secure", "evidence": "A plank walkway crosses the river."}],
    "strengths": ["You worked quickly."],
    "next_steps": ["Add railings on both sides."],
})


class TeacherLLM:
    """Answers the starter-scene prompt and the assessment prompt; records what it was asked."""

    def __init__(self):
        self.assessment_requests: list[str] = []

    async def complete(self, messages):
        system = messages[0]["content"]
        if "ASSESSMENT MODE" in system:
            return STARTER_REPLY
        if "formative assessment" in system:
            self.assessment_requests.append(messages[-1]["content"])
            return FEEDBACK_REPLY
        return "chat answer"


class FakeMinecraft:
    """A tiny Minecraft: applies fill/setblock to a block dict and answers testforblock from it."""

    PLAYER = {"dimension": 0, "position": {"x": 10.5, "y": -58.38, "z": 20.5}, "uniqueId": "-1", "yRot": 0.0}

    def __init__(self):
        self.world: dict[tuple[int, int, int], str] = {}
        self.commands: list[str] = []
        self.replies: list[str] = []
        self.ws = None

    def apply(self, command: str) -> dict:
        parts = command.split()
        if parts[0] == "fill":
            x1, y1, z1, x2, y2, z2 = map(int, parts[1:7])
            for x in range(min(x1, x2), max(x1, x2) + 1):
                for y in range(min(y1, y2), max(y1, y2) + 1):
                    for z in range(min(z1, z2), max(z1, z2) + 1):
                        self.world[(x, y, z)] = parts[7]
        elif parts[0] == "setblock":
            self.world[tuple(map(int, parts[1:4]))] = parts[4]
        elif parts[0] == "testforblock":
            block = self.world.get(tuple(map(int, parts[1:4])), "air")
            if block == "air":
                return {"statusCode": 0}
            pretty = block.replace("_", " ").title()
            return {"statusCode": -2147483648, "statusMessage": f"The block at {','.join(parts[1:4])} is {pretty} (expected: Air)."}
        elif parts[0] == "querytarget":
            return {"statusCode": 0, "details": json.dumps([self.PLAYER])}
        return {"statusCode": 0}

    async def serve(self):
        async for raw in self.ws:
            data = json.loads(raw)
            if data["header"]["messagePurpose"] == "subscribe":
                continue
            command = data["body"]["commandLine"]
            if command.startswith("tellraw "):
                payload = command[command.index(' {"rawtext"') + 1:]
                self.replies.append(json.loads(payload)["rawtext"][0]["text"])
                body = {"statusCode": 0}
            else:
                self.commands.append(command)
                body = self.apply(command)
            await self.ws.send(json.dumps({
                "header": {"requestId": data["header"]["requestId"], "messagePurpose": "commandResponse"},
                "body": body,
            }))

    async def send_event(self, name: str, body: dict):
        await self.ws.send(json.dumps({"header": {"messagePurpose": "event", "eventName": name, "version": 1}, "body": body}))

    async def chat(self, sender: str, message: str):
        await self.send_event("PlayerMessage", {"message": message, "sender": sender, "receiver": "", "type": "chat"})

    async def wait_for(self, text: str, after: int = 0, timeout: float = 10) -> int:
        """Wait for a reply containing `text` at index >= after; returns its index."""
        async def poll():
            while True:
                for i in range(after, len(self.replies)):
                    if text in self.replies[i]:
                        return i
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(poll(), timeout)


def test_assessment_end_to_end(tmp_path):
    store = RubricStore(tmp_path / "rubrics")
    store.save("test_bridge", TEST_RUBRIC)
    llm = TeacherLLM()
    events = []
    bridge = ChatBridge(llm, BridgeConfig(), on_event=events.append, rubrics=store, reports_dir=tmp_path / "reports")
    mc = FakeMinecraft()

    async def scenario():
        server = MinecraftServer("127.0.0.1", 0, bridge.handle_chat, on_game_event=bridge.handle_game_event)
        await server.start()
        try:
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                mc.ws = ws
                serving = asyncio.create_task(mc.serve())

                await mc.chat("Steve", "!challenge")
                i = await mc.wait_for("Choose a challenge")
                await mc.wait_for("1. Test Bridge", i)
                await mc.chat("Steve", "1")
                i = await mc.wait_for("Your task: Build a bridge across the river to the far bank.", i)
                attempt1_commands = len(mc.commands)

                # The scene sits 10 blocks ahead (anchor z=30), 2 more for the gap: local z0 -> world z32.
                assert 'tp "Steve" 10.5 -60 33.5 facing 10.5 -60 36.5' in mc.commands
                assert mc.world[(10, -61, 32)] == "grass_block" and mc.world[(10, -61, 36)] == "water"

                # The student lays a plank walkway across the task area.
                for z in range(34, 39):
                    mc.world[(10, -60, z)] = "oak_planks"
                    await mc.send_event("BlockPlaced", {"block": {"id": "oak_planks", "namespace": "minecraft"},
                                                        "player": {"name": "Steve"}, "count": 1})
                await mc.send_event("BlockPlaced", {"block": {"id": "stone"}, "player": {"name": "Alex"}})
                await mc.chat("Steve", "Finished!")
                start = i
                i = await mc.wait_for("Would you like to try again?", i)
                feedback = "\n".join(mc.replies[start:i + 1])
                assert "How you did: You reached the far bank." in feedback
                assert "- Spanning the gap: Secure. A plank walkway crosses the river." in feedback
                assert "To do better: 1) Add railings on both sides." in feedback

                await mc.chat("Steve", "yes")
                await mc.wait_for("Rebuilding the scene", i)
                i = await mc.wait_for("Your task:", i)
                assert len(mc.commands) > attempt1_commands
                assert mc.world[(10, -60, 34)] == "air", "the rebuild clears the student's blocks"
                await mc.chat("Steve", "!cancel")
                await mc.wait_for("Challenge stopped", i)
                serving.cancel()
        finally:
            await server.close()

    asyncio.run(scenario())

    request = llm.assessment_requests[0]
    assert "Blocks placed (5): oak_planks x5" in request, "only Steve's events are recorded"
    assert "added: oak_planks x5" in request
    assert "# Rubric" in request and "Test Bridge" in request
    reports = list((tmp_path / "reports").glob("*_Steve_test_bridge_attempt1.md"))
    assert len(reports) == 1 and "Add railings on both sides." in reports[0].read_text()
    result = next(e for e in events if e["type"] == "assessment")
    assert result["criteria"][0]["level"] == "Secure" and result["attempt"] == 1
    assert bridge.assessments.sessions == {}


def test_assess_rejects_invalid_choice_and_no_rubrics(tmp_path):
    bridge = ChatBridge(TeacherLLM(), BridgeConfig(), rubrics=RubricStore(tmp_path / "none"))
    mc = FakeMinecraft()

    async def scenario():
        server = MinecraftServer("127.0.0.1", 0, bridge.handle_chat)
        await server.start()
        try:
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                mc.ws = ws
                serving = asyncio.create_task(mc.serve())
                await mc.chat("Steve", "!challenge")
                await mc.wait_for("There are no challenges yet")
                RubricStore(tmp_path / "none").save("test_bridge", TEST_RUBRIC)
                await mc.chat("Steve", "!assess")  # the old name still works
                i = await mc.wait_for("Choose a challenge")
                await mc.chat("Steve", "7")
                await mc.wait_for("Type a number from 1 to 1", i)
                serving.cancel()
        finally:
            await server.close()

    asyncio.run(scenario())
