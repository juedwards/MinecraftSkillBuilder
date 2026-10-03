"""`!challenge` (formerly `!assess`): rubric-based building challenges, assessed formatively.

Flow: `!challenge` -> the player picks a rubric -> the AI designs a partially completed scene
from the rubric and builds it nearby -> the player is teleported there and given a task ->
their block activity and chat are recorded -> they type "finished" -> the task area is
inspected block by block -> the AI gives formative feedback against the rubric -> the
player can try again on a fresh copy of the scene.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .builder import (
    _NOTHING_CHANGED, BuildError, BuildOp, Placement, PlayerPosition, build_prompt, extract_code,
    facing_from_yaw, run_design_script, to_commands,
)
from .minecraft import GameEvent, MinecraftConnection, quote_target
from .progress import Progress
from .usage import CHALLENGE_FEEDBACK, CHALLENGE_SETUP, usage_context
from .realworld import design_map, parse_map_args
from .village import PLOT, design_village, summon_villagers

if TYPE_CHECKING:
    from .bridge import ChatBridge

RUBRIC_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_RUBRIC_CHARS = 20_000
SITE_DISTANCE = 10  # how far ahead of the player the scene is anchored
CLEAR_HEADROOM = 4  # extra air cleared above the scene
MAX_SCAN = 5000  # max blocks inspected when the player finishes
ASSESSMENT_VILLAGE_BUILDINGS = 12  # smaller than !village so setup is quicker
MAP_TASK_SIZE = 22  # task area for map scenes: the middle 22 x 22 blocks, 10 tall (within MAX_SCAN)
FINISH_WORDS = {"finished", "finish", "done", "i'm finished", "im finished", "i am finished", "!finished", "!done", "i'm done", "im done"}
YES_WORDS = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "yes please"}
NO_WORDS = {"no", "n", "nope", "no thanks", "not now"}

STARTER_RULES = """

ASSESSMENT MODE: you are not building a finished creation. You are building the STARTING \
SCENE for a student assessment described by the rubric the user gives you.
- Build the scene described in the rubric's "Starter build" section (or, if it has none, a \
fitting partially completed scene) and leave the part the student must build unfinished.
- Build a solid ground platform under the whole scene at startY so it works on any terrain.
- Call markPlayerStart(x, y, z) once: where the student stands to begin, on top of the \
ground (y = startY + 1), near the front (low Z) of the scene.
- Call markTaskArea(x1, y1, z1, x2, y2, z2) once: the region the student is expected to \
build in. It is inspected to assess their work, so include all the space they might use, \
but keep its volume within 5,000 blocks: for example 20 x 12 x 20 for a wide build, or \
10 x 45 x 10 for a tall one.
- Before the <code> tag, also output a <task> tag with the instructions for the student: \
2-3 short plain-text sentences addressed to them, saying what to build and what good work \
looks like. Refer to yourself as "me" (e.g. "tell me in chat"), never as an assessor or \
teacher. No Markdown.
"""

ASSESS_SYSTEM = """\
You are a supportive teacher giving formative assessment to a student in Minecraft Education. \
Judge the student's build against the rubric using only the evidence provided: an inspection \
of the task area after they finished (compared with the starting scene) and a log of their \
activity. Be encouraging, specific and honest. If the evidence is unclear, say so rather than guess.

Reply with only a JSON object, no other text:
{"summary": "2 sentences to the student about their build overall",
 "criteria": [{"name": "criterion name from the rubric", "level": "a level name from the rubric", "evidence": "one short sentence"}],
 "strengths": ["one or two things they did well"],
 "next_steps": ["one to three specific, achievable actions that would move them up a level"]}

Write directly to the student ("you"), in plain text, school-appropriate, short enough for game chat."""


class RubricError(ValueError):
    pass


@dataclass(frozen=True)
class Rubric:
    id: str
    title: str
    text: str


def rubric_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def rubric_section(text: str, heading: str) -> str:
    """Body of a `## heading` section, or ""."""
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


class RubricStore:
    """Rubrics are Markdown files in a directory; the file stem is the rubric's id."""

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, rubric_id: str) -> Path:
        if not RUBRIC_ID.match(rubric_id):
            raise RubricError("Rubric names may only use lowercase letters, numbers, - and _.")
        return self.directory / f"{rubric_id}.md"

    def list(self) -> list[Rubric]:
        if not self.directory.is_dir():
            return []
        rubrics = [self.get(path.stem) for path in self.directory.glob("*.md") if RUBRIC_ID.match(path.stem)]
        return sorted((r for r in rubrics if r), key=lambda r: r.title.lower())

    def get(self, rubric_id: str) -> Rubric | None:
        try:
            path = self._path(rubric_id)
        except RubricError:
            return None
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        return Rubric(rubric_id, rubric_title(text, rubric_id.replace("_", " ").title()), text)

    def save(self, rubric_id: str, text: str) -> Rubric:
        path = self._path(rubric_id)
        text = text.replace("\r\n", "\n").strip() + "\n"
        if len(text) > MAX_RUBRIC_CHARS:
            raise RubricError(f"Rubrics can be at most {MAX_RUBRIC_CHARS} characters.")
        if not text.strip():
            raise RubricError("The rubric is empty.")
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self.get(rubric_id)  # type: ignore[return-value]

    def delete(self, rubric_id: str) -> bool:
        path = self._path(rubric_id)
        if not path.is_file():
            return False
        path.unlink()
        return True


# --- Scenes and inspection ------------------------------------------------------------

Pos = tuple[int, int, int]


@dataclass(frozen=True)
class Scene:
    rubric: Rubric
    task: str
    description: str
    clear_commands: list[str]  # empty the site first (world coordinates)
    commands: list[str]  # then build the scene
    area: BuildOp  # the task area in world coordinates
    start: Pos
    expected: dict[Pos, str]  # starting-scene block per task-area cell ("?" = untouched terrain)
    villagers: list[Pos] = field(default_factory=list)  # summoned on the first attempt only
    teleport_early: bool = False  # move the player before building (the scene is built around them)


def extract_tag(text: str, tag: str) -> str:
    match = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def clip_area(area: BuildOp, limit: int = MAX_SCAN) -> BuildOp:
    """Shrink a task area to at most `limit` blocks: trim the top first, then the sides evenly."""
    x1, y1, z1, x2, y2, z2 = area.x1, area.y1, area.z1, area.x2, area.y2, area.z2
    while (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) > limit:
        if y2 - y1 >= 4:
            y2 -= 1
        elif x2 - x1 >= z2 - z1:
            x1, x2 = x1 + 1, x2 - 1 if x2 - x1 > 1 else x2
        else:
            z1, z2 = z1 + 1, z2 - 1 if z2 - z1 > 1 else z2
    return BuildOp(x1, y1, z1, x2, y2, z2, "air")


def cells(area: BuildOp) -> list[Pos]:
    return [
        (x, y, z)
        for y in range(area.y1, area.y2 + 1)
        for z in range(area.z1, area.z2 + 1)
        for x in range(area.x1, area.x2 + 1)
    ]


def expected_blocks(ops: list[BuildOp], area: BuildOp) -> dict[Pos, str]:
    """What each task-area cell holds after the starting scene is built ("?" = never touched)."""
    grid = {pos: "?" for pos in cells(area)}
    for op in ops:
        xs = range(max(op.x1, area.x1), min(op.x2, area.x2) + 1)
        ys = range(max(op.y1, area.y1), min(op.y2, area.y2) + 1)
        zs = range(max(op.z1, area.z1), min(op.z2, area.z2) + 1)
        for x in xs:
            for y in ys:
                for z in zs:
                    border = x in (op.x1, op.x2) or y in (op.y1, op.y2) or z in (op.z1, op.z2)
                    value = op.block
                    if op.mode == "hollow" and not border:
                        value = "air"
                    elif op.mode == "outline" and not border:
                        continue
                    elif op.mode == "keep" and grid[(x, y, z)] != "air":
                        continue  # "?" stays unknown: the terrain there may not have been air
                    elif op.mode == "replace" and op.replace_filter and grid[(x, y, z)] != op.replace_filter:
                        continue
                    grid[(x, y, z)] = value
    return grid


LIQUIDS = {"water", "flowing_water", "lava", "flowing_lava"}
FALLING = {"sand", "red_sand", "gravel"}
SOIL = {"grass_block", "dirt", "grass", "dirt_path", "grass_path", "farmland"}


def is_physics(before: str, now: str) -> bool:
    """Changes the game makes by itself: water flowing or draining, sand/gravel falling, grass turning to dirt.

    Replacing water with a block (e.g. a pillar in a river) is not physics: the student did that.
    """
    return (
        (now in LIQUIDS and (before == "air" or before in FALLING or before in LIQUIDS))
        or (before in LIQUIDS and now == "air")
        or (before in FALLING and now == "air")
        or (before in SOIL and now in SOIL)
    )


def _words(name: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", name.lower()) if w and w not in ("block", "of", "minecraft")}


def same_block(found: str, expected: str) -> bool:
    return found == expected or _words(found) == _words(expected)


def render_inspection(area: BuildOp, found: dict[Pos, str], expected: dict[Pos, str], start: Pos) -> str:
    """A layer-by-layer text map of the task area for the LLM, plus what changed."""
    counts = Counter(found.values())
    symbols: dict[str, str] = {"air": ".", "water": "~", "unknown": "?"}
    alphabet = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
    for name, _ in counts.most_common():
        if name not in symbols:
            symbols[name] = next(alphabet, "#")

    added: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    replaced: Counter[str] = Counter()
    physics: Counter[str] = Counter()
    layers = []
    for y in range(area.y1, area.y2 + 1):
        rows, changes, layer_changed, layer_solid = [], [], False, False
        for z in range(area.z1, area.z2 + 1):
            row, change_row = [], []
            for x in range(area.x1, area.x2 + 1):
                now, before = found[(x, y, z)], expected[(x, y, z)]
                row.append(symbols[now])
                layer_solid |= now != "air"
                mark = " "
                if before == "?" or now == "unknown":
                    mark = "?" if now != "air" and before == "?" else " "
                elif not same_block(now, before):
                    if is_physics(before, now):
                        mark, physics[f"{before} -> {now}"] = "%", physics[f"{before} -> {now}"] + 1
                    elif before == "air":
                        mark, added[now] = "+", added[now] + 1
                    elif now == "air":
                        mark, removed[before] = "-", removed[before] + 1
                    else:
                        mark, replaced[f"{before} -> {now}"] = "*", replaced[f"{before} -> {now}"] + 1
                    layer_changed = True
                change_row.append(mark)
            rows.append("".join(row))
            changes.append("".join(change_row))
        label = f"Layer {y - area.y1} (y={y})"
        if not layer_solid:
            layers.append(f"{label}: all air")
            continue
        block = [label + ("   |   changes" if layer_changed else "")]
        for z_index, (row, change) in enumerate(zip(rows, changes)):
            block.append(f"  z{z_index:>2} {row}" + (f"   | {change}" if layer_changed else ""))
        layers.append("\n".join(block))

    legend = ", ".join(f"{sym}={name}" for name, sym in symbols.items() if name in counts)
    sx, sy, sz = start
    lines = [
        f"Task area: {area.x2 - area.x1 + 1} wide (x) x {area.y2 - area.y1 + 1} tall (y) x {area.z2 - area.z1 + 1} deep (z).",
        f"Rows are z (z0 nearest the student's start), columns are x (left to right from x={area.x1}).",
        f"The student started at x={sx}, y={sy}, z={sz}.",
        f"Legend: {legend}.",
        "Changes compared with the starting scene: + block added, - block removed, * block replaced, "
        "% game physics (not the student), ? unknown.",
        "",
        "Summary of changes by the student:",
        f"  added: {_fmt_counts(added)}",
        f"  removed: {_fmt_counts(removed)}",
        f"  replaced: {_fmt_counts(replaced)}",
        f"Changes made by game physics, not the student (water flowing, sand falling, grass turning to dirt): "
        f"{_fmt_counts(physics)}",
        "",
        *layers,
    ]
    return "\n".join(lines)


def _fmt_counts(counts: Counter[str]) -> str:
    return ", ".join(f"{name} x{n}" for name, n in counts.most_common()) or "none"


# --- Sessions -------------------------------------------------------------------------


@dataclass
class Activity:
    time: float
    kind: str  # "placed", "broken" or "chat"
    detail: str


@dataclass
class AssessmentSession:
    player: str
    state: str  # "choosing" | "preparing" | "active" | "assessing" | "retry"
    options: list[Rubric] = field(default_factory=list)
    scene: Scene | None = None
    attempt: int = 0
    started: float = 0.0
    activity: list[Activity] = field(default_factory=list)


@dataclass(frozen=True)
class Feedback:
    summary: str
    criteria: list[dict[str, str]] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


def parse_feedback(reply: str) -> Feedback:
    try:
        data = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return Feedback(summary=" ".join(reply.split())[:600])

    def strings(value: Any) -> list[str]:
        return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []

    criteria = [
        {"name": str(c.get("name", "")).strip(), "level": str(c.get("level", "")).strip(), "evidence": str(c.get("evidence", "")).strip()}
        for c in data.get("criteria", []) if isinstance(c, dict)
    ]
    return Feedback(
        summary=str(data.get("summary", "")).strip(),
        criteria=[c for c in criteria if c["name"]],
        strengths=strings(data.get("strengths")),
        next_steps=strings(data.get("next_steps")),
    )


def feedback_chat(feedback: Feedback) -> str:
    lines = [f"How you did: {feedback.summary}"] if feedback.summary else []
    for c in feedback.criteria:
        lines.append(f"- {c['name']}: {c['level']}" + (f". {c['evidence']}" if c["evidence"] else ""))
    if feedback.strengths:
        lines.append("Well done: " + " ".join(feedback.strengths))
    if feedback.next_steps:
        lines.append("To do better: " + " ".join(f"{i}) {step}" for i, step in enumerate(feedback.next_steps, 1)))
    return "\n".join(lines)


def describe_activity(session: AssessmentSession, finished: float) -> str:
    placed = Counter(a.detail for a in session.activity if a.kind == "placed")
    broken = Counter(a.detail for a in session.activity if a.kind == "broken")
    chats = [a for a in session.activity if a.kind == "chat"]
    minutes, seconds = divmod(int(finished - session.started), 60)
    lines = [
        f"Attempt {session.attempt}; time taken: {minutes} min {seconds} s.",
        f"Blocks placed ({sum(placed.values())}): {_fmt_counts(placed)}",
        f"Blocks broken ({sum(broken.values())}): {_fmt_counts(broken)}",
    ]
    if chats:
        lines.append("Student's chat during the task:")
        lines += [f"  [{int(a.time - session.started)}s] {a.detail}" for a in chats[-20:]]
    return "\n".join(lines)


class AssessmentManager:
    def __init__(self, bridge: ChatBridge, rubrics: RubricStore, reports_dir: Path):
        self.bridge = bridge
        self.rubrics = rubrics
        self.reports_dir = reports_dir
        self.sessions: dict[str, AssessmentSession] = {}

    def overview(self) -> list[dict[str, Any]]:
        return [
            {"player": s.player, "state": s.state, "attempt": s.attempt, "rubric": s.scene.rubric.title if s.scene else None}
            for s in self.sessions.values()
        ]

    def emit(self, player: str, status: str) -> None:
        self.bridge.emit("assess", player=player, status=status)

    async def say(self, conn: MinecraftConnection, player: str, text: str, error: bool = False) -> None:
        await self.bridge.reply(conn, player, text, error=error, private=True)

    # --- chat entry point --------------------------------------------------------------

    async def handle_chat(self, conn: MinecraftConnection, player: str, text: str) -> bool:
        """Handle a chat message if it belongs to the assessment flow. Returns True if consumed."""
        command = " ".join(text.lower().split()).rstrip(".!?") or text.lower()
        session = self.sessions.get(player)
        if command in ("!challenge", "!assess"):
            await self.start(conn, player)
            return True
        if session is None:
            return False
        if command in ("!cancel", "!stop"):
            del self.sessions[player]
            self.emit(player, "cancelled")
            await self.say(conn, player, "Challenge stopped. Type !challenge to start another.")
            return True
        if session.state == "choosing":
            await self._choose(conn, session, text.strip())
            return True
        if session.state == "active":
            if command in FINISH_WORDS:
                await self._finish(conn, session)
                return True
            session.activity.append(Activity(time.time(), "chat", text.strip()))
            return False  # still answered by the AI as normal chat
        if session.state == "retry":
            if command in YES_WORDS:
                await self._prepare(conn, session, rebuild_only=True)
                return True
            del self.sessions[player]
            self.emit(player, "finished")
            if command in NO_WORDS:
                await self.say(conn, player, "Great work today! Type !challenge any time to try another challenge.")
                return True
        return False

    def record_game_event(self, event: GameEvent) -> None:
        session = self.sessions.get(event.player)
        if session is None or session.state != "active" or event.name not in ("BlockPlaced", "BlockBroken"):
            return
        block = event.body.get("block")
        name = ""
        if isinstance(block, dict):
            name = str(block.get("id") or block.get("name") or "")
        kind = "placed" if event.name == "BlockPlaced" else "broken"
        session.activity.append(Activity(time.time(), kind, name.removeprefix("minecraft:") or "unknown"))

    # --- steps -------------------------------------------------------------------------

    async def start(self, conn: MinecraftConnection, player: str) -> None:
        current = self.sessions.get(player)
        if current and current.state in ("active", "preparing", "assessing"):
            title = current.scene.rubric.title if current.scene else "a challenge"
            await self.say(conn, player, f"You're already doing {title}. Type finished when you're done, or !cancel to stop.")
            return
        if self.bridge.llm is None:
            await self.say(conn, player, "I'm not connected to an AI yet. Type !setup to connect me.", error=True)
            return
        rubrics = self.rubrics.list()
        if not rubrics:
            await self.say(conn, player, "There are no challenges yet. Ask your teacher to add one in Minecraft Quest Builder.", error=True)
            return
        self.sessions[player] = AssessmentSession(player, "choosing", options=rubrics)
        self.emit(player, "choosing a rubric")
        options = "\n".join(f"{i}. {r.title}" for i, r in enumerate(rubrics, 1))
        await self.say(conn, player, f"Choose a challenge by typing its number:\n{options}\n(Type !cancel to stop.)")

    async def _choose(self, conn: MinecraftConnection, session: AssessmentSession, answer: str) -> None:
        options = session.options
        choice = None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            choice = options[int(answer) - 1]
        else:
            choice = next((r for r in options if answer.lower() in (r.title.lower(), r.id)), None)
        if choice is None:
            await self.say(conn, session.player, f"Type a number from 1 to {len(options)}, or !cancel.", error=True)
            return
        session.scene = None
        session.options = [choice]
        await self._prepare(conn, session)

    async def _prepare(self, conn: MinecraftConnection, session: AssessmentSession, rebuild_only: bool = False) -> None:
        """Design (first attempt only), build, teleport and give the task."""
        player = session.player
        busy = self.bridge.begin_build(conn, player)
        if busy:
            await self.say(conn, player, f"I'm busy building for {busy}. Type !challenge to try again in a moment.")
            self.sessions.pop(player, None)
            return
        session.state = "preparing"
        try:
            async with self.bridge.progress(conn, player, private=True) as progress:
                if not rebuild_only or session.scene is None:
                    rubric = session.options[0]
                    await progress.say(f"Setting up {rubric.title}. I'm designing your starting scene, this can take a minute...")
                    self.emit(player, f"designing scene for {rubric.title}")
                    progress.stage("The AI is designing your starting scene")
                    with usage_context(player, CHALLENGE_SETUP):
                        session.scene = await self.create_scene(conn, player, rubric, progress)
                else:
                    await progress.say("Rebuilding the scene for another try...")
                scene = session.scene
                self.emit(player, f"building scene ({len(scene.commands)} commands)")
                progress.stage("Building your starting scene", total=len(scene.clear_commands) + len(scene.commands))
                # Clear the site completely before building, so nothing is placed into old blocks.
                errors = await conn.run_commands(scene.clear_commands, on_done=progress.tick)
                if scene.teleport_early:
                    await self._teleport(conn, player, scene)
                errors += await conn.run_commands(scene.commands, on_done=progress.tick)
                errors = [e for e in errors if not _NOTHING_CHANGED.search(e)]
                if errors:
                    self.emit(player, f"{len(errors)} scene commands failed: {errors[0]}")
                if session.attempt == 0 and scene.villagers:
                    progress.stage("Summoning villagers")
                    await summon_villagers(conn, scene.villagers)
                await self._teleport(conn, player, scene)
        except Exception as exc:
            self.sessions.pop(player, None)
            self.emit(player, f"failed: {exc}")
            message = str(exc) if isinstance(exc, BuildError) else "something went wrong while setting up."
            await self.say(conn, player, f"Sorry, I couldn't set up the challenge: {message}", error=True)
            return
        finally:
            self.bridge.end_build(conn)

        session.attempt += 1
        session.activity = []
        session.started = time.time()
        session.state = "active"
        self.emit(player, f"attempt {session.attempt} started: {scene.rubric.title}")
        await self.say(conn, player, f"Your task: {scene.task}\nType finished in chat when you're done.")

    async def create_scene(self, conn: MinecraftConnection, player: str, rubric: Rubric, progress: Progress | None = None) -> Scene:
        llm = self.bridge.llm
        if llm is None:
            raise BuildError("the AI isn't connected.")
        map_spec = rubric_section(rubric.text, "Starter map")
        if map_spec:
            return await self._map_scene(conn, player, rubric, " ".join(map_spec.split()), progress)
        village_style = rubric_section(rubric.text, "Starter village")
        if village_style:
            return await self._village_scene(conn, player, rubric, " ".join(village_style.split())[:300], progress)
        reply = await llm.complete([
            {"role": "system", "content": build_prompt() + STARTER_RULES},
            {"role": "user", "content": f"Create the starting scene for this assessment rubric:\n\n{rubric.text}"},
        ])
        design = await asyncio.to_thread(run_design_script, extract_code(reply))
        task = extract_tag(reply, "task") or rubric_section(rubric.text, "Task") or "Complete the build."
        task = " ".join(re.sub(r"(\*\*|__|`)", "", task).split())

        try:
            info = await conn.query_player(player)
        except Exception as exc:
            raise BuildError("I couldn't find where you are. Are cheats turned on in this world?") from exc
        here = PlayerPosition(info["position"]["x"], info["position"]["y"], info["position"]["z"], info.get("yRot", 0.0))
        fx, fz = facing_from_yaw(here.yaw)
        anchor = PlayerPosition(here.x + fx * SITE_DISTANCE, here.y, here.z + fz * SITE_DISTANCE, here.yaw)
        placement = Placement(design.ops, anchor)
        ops = [placement.op(op) for op in design.ops]

        clear = BuildOp(
            min(op.x1 for op in ops) - 1, min(op.y1 for op in ops), min(op.z1 for op in ops) - 1,
            max(op.x2 for op in ops) + 1, max(op.y2 for op in ops) + CLEAR_HEADROOM, max(op.z2 for op in ops) + 1,
            "air",
        )
        world_ops = [clear, *ops]
        area = placement.op(design.area) if design.area else BuildOp(clear.x1, clear.y1, clear.z1, clear.x2, clear.y2, clear.z2, "air")
        area = clip_area(area)
        start = placement.point(*design.start) if design.start else placement.point(placement.centre_x, 1, placement.front_z - 1)
        return Scene(
            rubric=rubric,
            task=task,
            description=extract_tag(reply, "description"),
            clear_commands=to_commands([clear]),
            commands=to_commands(ops),
            area=area,
            start=start,
            expected=expected_blocks(world_ops, area),
        )

    async def _map_scene(self, conn: MinecraftConnection, player: str, rubric: Rubric, spec: str, progress: Progress | None = None) -> Scene:
        """A real place from OpenStreetMap around the player; the task area is the middle of the map."""
        request = parse_map_args(spec)
        scene = await design_map(
            self.bridge.map_source, conn, player, request, llm=self.bridge.llm, progress=progress,
            on_status=lambda status: self.emit(player, f"map: {status}"),
        )
        (cx, cz), ground, half = scene.centre, scene.ground, MAP_TASK_SIZE // 2
        area = BuildOp(cx - half, ground - 2, cz - half, cx + half - 1, ground + 7, cz + half - 1, "air")
        start = scene.world(scene.layout.nearest_open((0, half + 2)))  # just south of the task area
        task = rubric_section(rubric.text, "Task") or "Complete the task in the middle of the map."
        return Scene(
            rubric=rubric,
            task=" ".join(re.sub(r"(\*\*|__|`)", "", task).split()),
            description=(
                f"A real-world map of {scene.place.name} from OpenStreetMap at {request.scale:g} m per block "
                f"({scene.summary()}){', with the bridges left out' if not request.bridges else ''}. North is -z. "
                f"The task area is the middle {MAP_TASK_SIZE} x {MAP_TASK_SIZE} blocks of the map."
            ),
            clear_commands=scene.clear_commands,
            commands=scene.commands,
            area=area,
            start=start,
            expected=expected_blocks(scene.world_ops, area),
            teleport_early=True,
        )

    async def _village_scene(self, conn: MinecraftConnection, player: str, rubric: Rubric, style: str, progress: Progress | None = None) -> Scene:
        """A village built around the player with one empty plot in front of them: the task area."""
        assert self.bridge.llm is not None
        village = await design_village(
            self.bridge.llm, conn, player, style, ASSESSMENT_VILLAGE_BUILDINGS, reserve_plot=True, progress=progress,
            on_status=lambda status: self.emit(player, f"village: {status}"),
        )
        plot = village.free_plot
        assert plot is not None
        area = village.plot_area(plot)
        (fx, fz), (x, z) = plot.facing, plot.front
        task = rubric_section(rubric.text, "Task") or "Build on the empty plot in front of you."
        return Scene(
            rubric=rubric,
            task=" ".join(re.sub(r"(\*\*|__|`)", "", task).split()),
            description=(
                f"{village.plan.name}, a {village.plan.style} village of {len(village.built)} buildings "
                f"({', '.join(village.built)}). The student builds on the empty plot ({PLOT} x {PLOT} blocks) "
                f"facing the village square; the task area is that plot."
            ),
            clear_commands=village.clear_commands,
            commands=village.commands,
            area=area,
            start=(x - fx * 2, village.ground + 1, z - fz * 2),
            expected=expected_blocks(village.world_ops, area),
            villagers=village.villagers,
            teleport_early=True,
        )

    async def _teleport(self, conn: MinecraftConnection, player: str, scene: Scene) -> None:
        x, y, z = scene.start
        a = scene.area
        cx, cy, cz = (a.x1 + a.x2) / 2 + 0.5, a.y1 + 1, (a.z1 + a.z2) / 2 + 0.5
        await conn.run_command(f"tp {quote_target(player)} {x + 0.5} {y} {z + 0.5} facing {cx} {cy} {cz}")

    async def _finish(self, conn: MinecraftConnection, session: AssessmentSession) -> None:
        player, scene = session.player, session.scene
        assert scene is not None
        finished = time.time()
        session.state = "assessing"
        self.emit(player, "finished, inspecting build")
        try:
            async with self.bridge.progress(conn, player, private=True) as progress:
                await progress.say("Great! Let me take a look at your build. This can take a minute...")
                positions = cells(scene.area)
                progress.stage("Inspecting your build block by block", total=len(positions))
                found = dict(zip(positions, await conn.blocks_at(positions, on_done=progress.tick)))
                inspection = render_inspection(scene.area, found, scene.expected, scene.start)
                activity = describe_activity(session, finished)
                llm = self.bridge.llm
                if llm is None:
                    raise RuntimeError("the AI isn't connected")
                self.emit(player, "assessing against the rubric")
                progress.stage("The AI is checking your build against the challenge")
                with usage_context(player, CHALLENGE_FEEDBACK):
                    reply = await llm.complete([
                        {"role": "system", "content": ASSESS_SYSTEM},
                        {"role": "user", "content": (
                            f"# Rubric\n\n{scene.rubric.text}\n\n# Task given to the student\n\n{scene.task}\n\n"
                            f"# Starting scene\n\n{scene.description or '(see the rubric)'}\n\n"
                            f"# Activity log\n\n{activity}\n\n# Inspection of the task area after the student finished\n\n{inspection}"
                        )},
                    ])
                feedback = parse_feedback(reply)
        except Exception as exc:
            session.state = "active"
            self.emit(player, f"assessment failed: {exc}")
            await self.say(conn, player, "Sorry, I couldn't check your build just now. Type finished to try again.", error=True)
            return

        report = self._save_report(session, feedback, activity, inspection, finished)
        session.state = "retry"
        self.bridge.emit(
            "assessment", player=player, rubric=scene.rubric.title, attempt=session.attempt,
            summary=feedback.summary, criteria=feedback.criteria, next_steps=feedback.next_steps,
            report=str(report) if report else "",
        )
        await self.say(conn, player, feedback_chat(feedback) + "\nWould you like to try again? Type yes or no.")

    def _save_report(self, session: AssessmentSession, feedback: Feedback, activity: str, inspection: str, finished: float) -> Path | None:
        scene = session.scene
        assert scene is not None
        stamp = datetime.fromtimestamp(finished)
        safe_player = re.sub(r"[^A-Za-z0-9_-]+", "_", session.player) or "player"
        path = self.reports_dir / f"{stamp:%Y%m%d-%H%M%S}_{safe_player}_{scene.rubric.id}_attempt{session.attempt}.md"
        criteria = "\n".join(f"| {c['name']} | {c['level']} | {c['evidence']} |" for c in feedback.criteria)
        text = f"""# {scene.rubric.title}: {session.player}, attempt {session.attempt}

Finished {stamp:%Y-%m-%d %H:%M}.

## Task

{scene.task}

## Feedback

{feedback.summary}

| Criterion | Level | Evidence |
|---|---|---|
{criteria}

**Strengths:** {" ".join(feedback.strengths) or "-"}

**Next steps:**
{chr(10).join(f"{i}. {s}" for i, s in enumerate(feedback.next_steps, 1)) or "-"}

## Activity

```
{activity}
```

## Inspection of the task area

```
{inspection}
```
"""
        try:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return path
        except OSError as exc:
            self.emit(session.player, f"could not save report: {exc}")
            return None
