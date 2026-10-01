"""`!build`: AI-designed structures placed live in front of the player.

Adapted from BuilderGPT (https://github.com/CyniaAI/BuilderGPT, Apache-2.0): the LLM writes
a JavaScript `buildCreation()` that calls `safeFill` / `safeSetBlock`, and the script runs in
a QuickJS sandbox to produce block operations. Instead of exporting a Java schematic, the
operations are rotated to face the player and sent to Bedrock / Education as
`fill` / `setblock` commands.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, replace
from typing import Callable

import quickjs

from .llm import ChatModel
from .minecraft import MinecraftConnection
from .progress import Progress

MAX_SIZE = 48  # max blocks along any axis
MAX_OPS = 5000  # max safeFill/safeSetBlock calls
MAX_FILL_VOLUME = 32768  # Bedrock's limit for one /fill
SCRIPT_TIME_LIMIT = 3  # seconds
SCRIPT_MEMORY_LIMIT = 64 * 1024 * 1024
GAP = 2  # blocks between the player and the front of the build
PLAYER_EYE_HEIGHT = 1.62  # Bedrock reports player positions at eye level

# Blocks builds may use (see palette.py for the list, fallbacks and why).
from .palette import BEDROCK_BLOCKS  # noqa: E402  (re-exported for the other modules)

# Minecraft reports these as errors, but they only mean the blocks were already right.
_NOTHING_CHANGED = re.compile(r"no blocks|\b0 blocks|couldn't be placed|could not be placed", re.IGNORECASE)

# Blocks that fall or flow away unless something solid is underneath.
NEEDS_SUPPORT = frozenset({"sand", "gravel", "water"})
SUPPORT_BLOCK = "stone"

FILL_MODES = frozenset({"destroy", "hollow", "keep", "outline", "replace"})
SETBLOCK_MODES = frozenset({"destroy", "keep", "replace"})

BUILD_SYSTEM_PROMPT = """\
You are an expert Minecraft builder and JavaScript coder creating structures in Minecraft \
Bedrock Edition (Minecraft Education). Your goal is to produce a Minecraft structure via code, \
considering accents, block variety, symmetry and asymmetry, overall aesthetics, and most \
importantly, adherence to the platonic ideal of the requested creation. The build will be seen \
by students, so keep it school-appropriate.

You have access to the following helper functions:

```javascript
/**
 * Fills a region with blocks.
 * @param {string} blockType - e.g. "stone", "oak_planks"
 * @param {Object} [options]
 * @param {string} [options.mode] - "destroy", "hollow", "keep", "outline" or "replace"
 * @param {string} [options.replaceFilter] - with mode "replace": only replace this block
 */
function safeFill(x1, y1, z1, x2, y2, z2, blockType, options = {}) {}

/**
 * Places a single block.
 * @param {Object} [options]
 * @param {string} [options.mode="replace"] - "replace", "destroy" or "keep"
 */
function safeSetBlock(x, y, z, blockType, options = {}) {}
```

Your task is to implement:

```javascript
function buildCreation(startX, startY, startZ) {
  // Implement this
}
```

IMPORTANT: You must only use block types from this list:

<block_types_list>
%BLOCKS%
</block_types_list>

Before your final output, plan briefly inside <build_planning> tags: key elements of the \
request, block choices for each part, a rough layout, and efficient use of safeFill for large \
areas and safeSetBlock for details.

Then give your final output in this format:

<description>
One sentence describing the creation.
</description>

<code>
function buildCreation(startX, startY, startZ) {
  // JavaScript using safeFill and safeSetBlock
}
</code>

Rules:
- The code MUST be enclosed in <code></code> tags, be synchronous (no async/await) and work in \
one shot; there is no opportunity for iteration.
- startY is ground level. Blocks above ground go above startY; floors and foundations go at startY.
- The creation must fit within %WIDTH% blocks along X and Z and %HEIGHT% blocks tall.
- Build from startX/startZ towards +X and +Z. The front or entrance must face -Z (the side \
with the lowest Z); that side will face the player.
- The terrain may not be flat or empty: clear the space with "air" first if needed, and lay \
your own floor or ground blocks.
- Block states (facing, etc.) are not supported; only plain block names.
- Choose materials like a real builder: stone bricks, quartz, sandstone or calcite for grand and \nhistoric buildings; brick with slate (deepslate_tiles) or clay-tile (red_terracotta) roofs for homes; \nplanks and logs for timber buildings; glass panes for windows; slabs for roof edges and trims; \ncopper (oxidized_copper) for green domes and spires. Vary textures (e.g. cracked or mossy stone \nbricks for old walls) and add trim so walls aren't one flat block.
- Blocks behave with real game physics: sand and gravel fall and water flows down and \
spreads, so always put solid blocks under them and walls around water.
"""

_HELPERS_JS = f"""
var console = {{ log: function() {{}}, warn: function() {{}}, error: function() {{}} }};
var __ops = [];
function __push(op) {{
  if (__ops.length >= {MAX_OPS}) throw new Error("too many blocks (max {MAX_OPS} operations)");
  __ops.push(op);
}}
function safeFill(x1, y1, z1, x2, y2, z2, blockType, options) {{
  __push([x1, y1, z1, x2, y2, z2, blockType, options || {{}}]);
}}
function safeSetBlock(x, y, z, blockType, options) {{
  __push([x, y, z, x, y, z, blockType, options || {{}}]);
}}
function safeFillBiome() {{}}
var __marks = {{}};
function markPlayerStart(x, y, z) {{ __marks.start = [x, y, z]; }}
function markTaskArea(x1, y1, z1, x2, y2, z2) {{ __marks.area = [x1, y1, z1, x2, y2, z2]; }}
"""


class BuildError(Exception):
    """A build failed in a way worth telling the player about."""


@dataclass(frozen=True)
class BuildOp:
    """An axis-aligned box of one block type (x1 <= x2, y1 <= y2, z1 <= z2)."""

    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int
    block: str
    mode: str = ""
    replace_filter: str = ""

    @property
    def volume(self) -> int:
        return (self.x2 - self.x1 + 1) * (self.y2 - self.y1 + 1) * (self.z2 - self.z1 + 1)


@dataclass(frozen=True)
class PlayerPosition:
    x: float
    y: float  # eye level, as reported by /querytarget
    z: float
    yaw: float

    @property
    def feet(self) -> tuple[int, int, int]:
        return math.floor(self.x), math.floor(round(self.y - PLAYER_EYE_HEIGHT, 3)), math.floor(self.z)


@dataclass(frozen=True)
class Design:
    """Result of running a build script: block operations plus optional assessment marks."""

    ops: list[BuildOp]
    start: tuple[int, int, int] | None = None  # markPlayerStart
    area: BuildOp | None = None  # markTaskArea, as a box (block "air")


@dataclass(frozen=True)
class BuildResult:
    size: tuple[int, int, int]  # width (x), height (y), depth (z) as designed
    commands: int
    failed: int
    first_error: str = ""


def build_prompt(width: int = 32, height: int = 32) -> str:
    return (
        BUILD_SYSTEM_PROMPT.replace("%BLOCKS%", ", ".join(sorted(BEDROCK_BLOCKS)))
        .replace("%WIDTH%", str(width)).replace("%HEIGHT%", str(height))
    )


def extract_code(text: str) -> str:
    match = re.search(r"<code>\s*(.*?)\s*</code>", text, re.DOTALL | re.IGNORECASE)
    if not match:
        match = re.search(r"```(?:javascript|js)?\s*\n(.*?function\s+buildCreation.*?)```", text, re.DOTALL)
    if not match:
        raise BuildError("The AI didn't return any build code.")
    return match.group(1)


def _normalize_block(value: object) -> str | None:
    block = str(value).strip().lower().removeprefix("minecraft:").split("[", 1)[0]
    return block if block in BEDROCK_BLOCKS else None


def _parse_op(raw: list) -> BuildOp | None:
    try:
        coords = [int(float(v)) for v in raw[:6]]
    except (TypeError, ValueError, OverflowError):
        return None
    block = _normalize_block(raw[6])
    if block is None:
        return None
    options = raw[7] if isinstance(raw[7], dict) else {}
    mode = str(options.get("mode") or "").lower()
    mode = mode if mode in FILL_MODES else ""
    replace_filter = ""
    if mode == "replace" and options.get("replaceFilter"):
        replace_filter = _normalize_block(options["replaceFilter"]) or ""
    x1, y1, z1, x2, y2, z2 = coords
    return BuildOp(
        min(x1, x2), min(y1, y2), min(z1, z2), max(x1, x2), max(y1, y2), max(z1, z2),
        block, mode, replace_filter,
    )


def run_build_script(code: str, max_size: int = MAX_SIZE) -> list[BuildOp]:
    """Run the model's JavaScript in a sandbox and return its block operations."""
    return run_design_script(code, max_size).ops


def run_design_script(code: str, max_size: int = MAX_SIZE) -> Design:
    """Run the model's JavaScript in a sandbox and return its operations and marks."""
    # As in BuilderGPT: tolerate async/await even though the prompt forbids it.
    code = re.sub(r"\basync\s+function\b", "function", code)
    code = re.sub(r"\bawait\s+", "", code)

    ctx = quickjs.Context()
    ctx.set_time_limit(SCRIPT_TIME_LIMIT)
    ctx.set_memory_limit(SCRIPT_MEMORY_LIMIT)
    try:
        ctx.eval(_HELPERS_JS)
        ctx.eval(code)
        ctx.eval("buildCreation(0, 0, 0)")
        raw_ops = json.loads(ctx.eval("JSON.stringify(__ops)"))
        marks = json.loads(ctx.eval("JSON.stringify(__marks)"))
    except quickjs.JSException as exc:
        message = str(exc).splitlines()[0] if str(exc) else "unknown error"
        raise BuildError(f"The build code failed: {message}") from exc

    ops = [op for op in (_parse_op(raw) for raw in raw_ops if isinstance(raw, list) and len(raw) == 8) if op]
    if not ops:
        raise BuildError("The design didn't contain any usable blocks.")
    ops = add_supports(ops)
    width, height, depth = bounding_size(ops)
    if max(width, height, depth) > max_size:
        raise BuildError(f"The design is too big ({width}x{height}x{depth}, max {max_size} per side).")

    start = area = None
    if isinstance(marks.get("start"), list) and len(marks["start"]) == 3:
        try:
            start = tuple(int(float(v)) for v in marks["start"])
        except (TypeError, ValueError, OverflowError):
            pass
    if isinstance(marks.get("area"), list) and len(marks["area"]) == 6:
        area = _parse_op([*marks["area"], "air", {}])
    return Design(ops, start, area)


def add_supports(ops: list[BuildOp]) -> list[BuildOp]:
    """Put stone under blocks that fall or flow (sand, gravel, water), where there would be air.

    Builds are placed on cleared ground, so without this a sand riverbed falls and water pours away.
    """
    supported = []
    for op in ops:
        if op.block in NEEDS_SUPPORT:
            supported.append(BuildOp(op.x1, op.y1 - 1, op.z1, op.x2, op.y1 - 1, op.z2, SUPPORT_BLOCK, "keep"))
        supported.append(op)
    return supported


def bounding_size(ops: list[BuildOp]) -> tuple[int, int, int]:
    return (
        max(op.x2 for op in ops) - min(op.x1 for op in ops) + 1,
        max(op.y2 for op in ops) - min(op.y1 for op in ops) + 1,
        max(op.z2 for op in ops) - min(op.z1 for op in ops) + 1,
    )


def facing_from_yaw(yaw: float) -> tuple[int, int]:
    """Cardinal (dx, dz) the player is looking along. Bedrock yaw 0 = +Z (south), 90 = -X."""
    rad = math.radians(yaw)
    dx, dz = -math.sin(rad), math.cos(rad)
    if abs(dx) > abs(dz):
        return (1 if dx > 0 else -1), 0
    return 0, (1 if dz > 0 else -1)


class Placement:
    """Maps design coordinates (origin 0,0,0, front facing -Z) into the world in front of a player."""

    def __init__(self, ops: list[BuildOp], player: PlayerPosition, gap: int = GAP):
        self.fx, self.fz = facing_from_yaw(player.yaw)
        self.px, py, self.pz = player.feet
        self.ground = py - 1
        self.centre_x = (min(op.x1 for op in ops) + max(op.x2 for op in ops)) // 2
        self.front_z = min(op.z1 for op in ops)
        self.gap = gap

    def point(self, x: int, y: int, z: int) -> tuple[int, int, int]:
        lx, lz = x - self.centre_x, z - self.front_z + self.gap
        # Rotate so local +Z points the way the player faces.
        return self.px + lx * self.fz + lz * self.fx, y + self.ground, self.pz - lx * self.fx + lz * self.fz

    def op(self, op: BuildOp) -> BuildOp:
        ax, ay, az = self.point(op.x1, op.y1, op.z1)
        bx, by, bz = self.point(op.x2, op.y2, op.z2)
        return replace(op, x1=min(ax, bx), x2=max(ax, bx), y1=ay, y2=by, z1=min(az, bz), z2=max(az, bz))


def place_ops(ops: list[BuildOp], player: PlayerPosition, gap: int = GAP) -> list[BuildOp]:
    """Move designed ops (origin 0,0,0, front facing -Z) into the world in front of the player."""
    placement = Placement(ops, player, gap)
    return [placement.op(op) for op in ops]


def _split(op: BuildOp) -> list[BuildOp]:
    """Split a box into pieces Bedrock's /fill accepts, preserving what it would build."""
    if op.volume <= MAX_FILL_VOLUME:
        return [op]
    if op.mode in ("hollow", "outline"):
        # Rebuild as six solid walls, plus an air interior for "hollow".
        x1, y1, z1, x2, y2, z2 = op.x1, op.y1, op.z1, op.x2, op.y2, op.z2
        walls = [
            (x1, y1, z1, x2, y1, z2), (x1, y2, z1, x2, y2, z2),
            (x1, y1, z1, x1, y2, z2), (x2, y1, z1, x2, y2, z2),
            (x1, y1, z1, x2, y2, z1), (x1, y1, z2, x2, y2, z2),
        ]
        pieces = [BuildOp(*w, op.block) for w in walls]
        if op.mode == "hollow" and x2 - x1 > 1 and y2 - y1 > 1 and z2 - z1 > 1:
            pieces.append(BuildOp(x1 + 1, y1 + 1, z1 + 1, x2 - 1, y2 - 1, z2 - 1, "air"))
        return [part for piece in pieces for part in _split(piece)]
    # Halve along the longest axis.
    spans = {"x": op.x2 - op.x1, "y": op.y2 - op.y1, "z": op.z2 - op.z1}
    axis = max(spans, key=spans.get)
    lo, hi = getattr(op, f"{axis}1"), getattr(op, f"{axis}2")
    mid = (lo + hi) // 2
    first = replace(op, **{f"{axis}2": mid})
    second = replace(op, **{f"{axis}1": mid + 1})
    return _split(first) + _split(second)


def to_commands(ops: list[BuildOp]) -> list[str]:
    commands = []
    for op in ops:
        if op.volume == 1 and op.mode in SETBLOCK_MODES | {""} and not op.replace_filter:
            mode = f" {op.mode}" if op.mode in ("destroy", "keep") else ""
            commands.append(f"setblock {op.x1} {op.y1} {op.z1} {op.block}{mode}")
            continue
        for piece in _split(op):
            mode = f" {piece.mode}" if piece.mode else ""
            if piece.mode == "replace" and piece.replace_filter:
                mode += f" {piece.replace_filter}"
            commands.append(
                f"fill {piece.x1} {piece.y1} {piece.z1} {piece.x2} {piece.y2} {piece.z2} {piece.block}{mode}"
            )
    return commands


async def design(llm: ChatModel, request: str, width: int = 32, height: int = 32, max_size: int = MAX_SIZE) -> list[BuildOp]:
    """Ask the LLM for a build script and run it."""
    reply = await llm.complete([
        {"role": "system", "content": build_prompt(width, height)},
        {"role": "user", "content": request},
    ])
    return await asyncio.to_thread(run_build_script, extract_code(reply), max_size)


async def build_for_player(
    llm: ChatModel,
    conn: MinecraftConnection,
    player: str,
    request: str,
    on_status: Callable[[str], None] = lambda status: None,
    progress: Progress | None = None,
) -> BuildResult:
    progress = progress or Progress.silent()
    on_status("designing")
    progress.stage(f"The AI is designing {request}")
    ops = await design(llm, request)
    size = bounding_size(ops)

    try:
        info = await conn.query_player(player)
        position = PlayerPosition(info["position"]["x"], info["position"]["y"], info["position"]["z"], info.get("yRot", 0.0))
    except Exception as exc:
        raise BuildError("I couldn't find where you are. Are cheats turned on in this world?") from exc

    commands = to_commands(place_ops(ops, position))
    on_status(f"placing {len(commands)} commands at {position.feet}")
    progress.stage("Placing blocks", total=len(commands))
    errors = [e for e in await conn.run_commands(commands, on_done=progress.tick) if not _NOTHING_CHANGED.search(e)]
    return BuildResult(size=size, commands=len(commands), failed=len(errors), first_error=errors[0] if errors else "")
