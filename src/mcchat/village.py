"""`!village`: an AI-planned village of ~20 buildings, built around the player, with villagers.

A planner agent (one LLM call) designs the village: name, ground and path blocks, and a list of
buildings in one style. Builder agents (one LLM call per building, a few at a time) then design
each building with the same sandboxed pipeline as `!build`. The buildings are laid out on a grid
of plots around a central square, separated by streets, each with its entrance facing the square.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .builder import (
    _NOTHING_CHANGED, BEDROCK_BLOCKS, NEEDS_SUPPORT, PLAYER_EYE_HEIGHT, BuildError, BuildOp, Placement,
    PlayerPosition, bounding_size, design, facing_from_yaw, to_commands,
)
from .llm import ChatModel
from .minecraft import MinecraftConnection, quote_target
from .progress import Progress

DEFAULT_BUILDINGS = 20
MAX_BUILDINGS = 24
PLOT = 11  # max building footprint (X and Z)
CENTREPIECE = 7  # max footprint of the centrepiece in the square
STREET = 3
PITCH = PLOT + STREET
MAX_HEIGHT = 20
CLEAR_HEIGHT = 24  # air cleared above the village ground
DESIGN_CONCURRENCY = 4  # builder agents running at once

PLANNER_PROMPT = """\
You plan villages for Minecraft Bedrock Edition (Minecraft Education). The village will be seen \
by students, so keep it school-appropriate.

Plan a village of exactly {count} buildings{style_clause}. The first building is the centrepiece \
of the village square (for example a well, fountain, statue, totem or campfire). The rest should \
be varied and fit the style: homes of different sizes, workshops, a meeting hall, farms, towers, \
market stalls and so on. Each building fits within {plot} x {plot} blocks and is at most \
{height} blocks tall (the centrepiece within {centre} x {centre}).

Choose a ground block and a path block from this list, avoiding sand, gravel and water:
{blocks}

Reply with only a JSON object, no other text:
{{"name": "the village's name", "style": "the style in a few words",
 "ground_block": "...", "path_block": "...",
 "buildings": [{{"name": "...", "description": "1-2 sentences: purpose, shape, materials and details in the village style"}}]}}"""

SolidBlocks = sorted(b for b in BEDROCK_BLOCKS if b not in NEEDS_SUPPORT | {"air"})


@dataclass(frozen=True)
class BuildingPlan:
    name: str
    description: str


@dataclass(frozen=True)
class VillagePlan:
    name: str
    style: str
    ground_block: str
    path_block: str
    buildings: list[BuildingPlan]


@dataclass(frozen=True)
class Plot:
    i: int  # grid cell, (0, 0) is the square
    j: int
    centre: tuple[int, int]  # world x, z
    facing: tuple[int, int]  # direction from the square towards the plot

    @property
    def front(self) -> tuple[int, int]:
        """Middle of the plot's edge that faces the square."""
        (x, z), (fx, fz) = self.centre, self.facing
        return x - fx * (PLOT // 2), z - fz * (PLOT // 2)


@dataclass
class Village:
    """A laid-out village, ready to build: commands plus what assessments need to know."""

    plan: VillagePlan
    ground: int  # y of the ground layer
    centre: tuple[int, int]
    area: BuildOp  # the whole village, ground layer up to the cleared height
    clear_commands: list[str]
    commands: list[str]  # ground, streets and buildings
    building_commands: list[list[str]]  # per building, for progress reporting
    villagers: list[tuple[int, int, int]]
    world_ops: list[BuildOp]  # everything placed, in order (clear first)
    built: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    free_plot: Plot | None = None  # left empty for an assessment
    spawn: tuple[int, int, int] = (0, 0, 0)  # where to stand in the square

    def plot_area(self, plot: Plot, height: int = 16) -> BuildOp:
        x, z = plot.centre
        half = PLOT // 2
        return BuildOp(x - half, self.ground, z - half, x + half, self.ground + height - 1, z + half, "air")


def parse_plan(reply: str, count: int) -> VillagePlan:
    try:
        data = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise BuildError("The village plan wasn't readable.") from exc
    buildings = [
        BuildingPlan(str(b.get("name", "")).strip()[:60] or f"Building {n}", str(b.get("description", "")).strip()[:500])
        for n, b in enumerate(data.get("buildings", []), 1)
        if isinstance(b, dict)
    ][:count]
    if not buildings:
        raise BuildError("The village plan had no buildings.")

    def solid(value: object, fallback: str) -> str:
        block = str(value or "").strip().lower().removeprefix("minecraft:")
        return block if block in SolidBlocks else fallback

    return VillagePlan(
        name=str(data.get("name") or "The Village").strip()[:60],
        style=str(data.get("style") or "").strip()[:80],
        ground_block=solid(data.get("ground_block"), "grass_block"),
        path_block=solid(data.get("path_block"), "cobblestone"),
        buildings=buildings,
    )


def choose_plots(count: int, centre: tuple[int, int], reserve: tuple[int, int] | None = None) -> list[Plot]:
    """`count` plots in rings around the square (cell 0,0), spread evenly around each ring."""
    cells: list[tuple[int, int]] = []
    ring = 1
    while len(cells) < count + (1 if reserve else 0):
        ring_cells = [(i, j) for i in range(-ring, ring + 1) for j in range(-ring, ring + 1) if max(abs(i), abs(j)) == ring]
        ring_cells.sort(key=lambda c: math.atan2(c[1], c[0]))
        if reserve in ring_cells:
            cells.append(reserve)
            ring_cells.remove(reserve)
        need = count + (1 if reserve else 0) - len(cells)
        if need >= len(ring_cells):
            cells += ring_cells
        else:
            step = len(ring_cells) / need
            cells += [ring_cells[int(k * step)] for k in range(need)]
        ring += 1

    cx, cz = centre
    plots = []
    for i, j in cells:
        facing = ((1 if i > 0 else -1), 0) if abs(i) >= abs(j) else (0, (1 if j > 0 else -1))
        plots.append(Plot(i, j, (cx + i * PITCH, cz + j * PITCH), facing))
    return plots


def standing_at(x: int, feet_y: int, z: int, facing: tuple[int, int]) -> PlayerPosition:
    """A virtual player at a block, looking along `facing` (for Placement)."""
    yaw = {(0, 1): 0.0, (-1, 0): 90.0, (0, -1): 180.0, (1, 0): -90.0}[facing]
    return PlayerPosition(x + 0.5, feet_y + PLAYER_EYE_HEIGHT, z + 0.5, yaw)


def building_request(plan: VillagePlan, building: BuildingPlan, centrepiece: bool) -> str:
    size = CENTREPIECE if centrepiece else PLOT
    role = "the centrepiece of the village square" if centrepiece else "one building"
    return (
        f"{building.name}: {building.description}\n\n"
        f"This is {role} in {plan.name}, a {plan.style} village; keep the {plan.style} style consistent. "
        f"The footprint must fit within {size} x {size} blocks (X and Z) and be at most {MAX_HEIGHT} blocks tall. "
        f"Build its floor at startY. The village ground ({plan.ground_block}) and streets already exist, "
        f"so do not build any ground outside the footprint and do not clear space with air outside it."
    )


async def plan_village(llm: ChatModel, style: str, count: int) -> VillagePlan:
    style_clause = f" in this style: {style}" if style else " in a style of your choice (be creative)"
    reply = await llm.complete([
        {"role": "system", "content": PLANNER_PROMPT.format(
            count=count, style_clause=style_clause, plot=PLOT, height=MAX_HEIGHT, centre=CENTREPIECE,
            blocks=", ".join(SolidBlocks),
        )},
        {"role": "user", "content": f"Plan the village. Style: {style or 'your choice'}."},
    ])
    return parse_plan(reply, count)


async def design_buildings(
    llm: ChatModel, plan: VillagePlan, on_designed: Callable[[int, int], Awaitable[None]],
) -> list[tuple[BuildingPlan, list[BuildOp] | None, str]]:
    """Run the builder agents. Returns (building, ops or None, error) in plan order."""
    semaphore = asyncio.Semaphore(DESIGN_CONCURRENCY)
    done = 0

    async def one(index: int, building: BuildingPlan) -> tuple[BuildingPlan, list[BuildOp] | None, str]:
        nonlocal done
        async with semaphore:
            try:
                ops = await design(llm, building_request(plan, building, centrepiece=index == 0))
                width, _, depth = bounding_size(ops)
                limit = CENTREPIECE if index == 0 else PLOT
                if max(width, depth) > limit + 2:
                    result = (building, None, f"too big ({width}x{depth})")
                else:
                    result = (building, ops, "")
            except Exception as exc:
                result = (building, None, str(exc) or type(exc).__name__)
        done += 1
        await on_designed(done, len(plan.buildings))
        return result

    return list(await asyncio.gather(*(one(i, b) for i, b in enumerate(plan.buildings))))


def lay_out(
    plan: VillagePlan,
    designs: list[tuple[BuildingPlan, list[BuildOp] | None, str]],
    feet: tuple[int, int, int],
    facing: tuple[int, int],
    reserve_plot: bool = False,
) -> Village:
    """Place the designed buildings on plots around the player (who stands in the square)."""
    px, py, pz = feet
    ground = py - 1
    centre = (px, pz)
    usable = [(b, ops) for b, ops, _ in designs if ops]
    failed = [f"{b.name} ({error})" for b, ops, error in designs if not ops]
    centrepiece = usable[0] if usable and designs[0][1] else None
    others = usable[1:] if centrepiece else usable
    plots = choose_plots(len(others), centre, reserve=facing if reserve_plot else None)
    free_plot = plots.pop(0) if reserve_plot else None

    ring = max([max(abs(p.i), abs(p.j)) for p in plots + ([free_plot] if free_plot else [])] or [1])
    half = ring * PITCH + PITCH // 2
    x1, x2, z1, z2 = px - half, px + half, pz - half, pz + half

    clear = BuildOp(x1, ground + 1, z1, x2, ground + CLEAR_HEIGHT, z2, "air")
    village_ops = [BuildOp(x1, ground, z1, x2, ground, z2, plan.ground_block)]
    for k in range(-ring, ring):  # streets between columns and rows of plots
        lane = k * PITCH + PLOT // 2 + 1
        village_ops.append(BuildOp(px + lane, ground, z1, px + lane + STREET - 1, ground, z2, plan.path_block))
        village_ops.append(BuildOp(x1, ground, pz + lane, x2, ground, pz + lane + STREET - 1, plan.path_block))
    village_ops.append(BuildOp(px - PLOT // 2, ground, pz - PLOT // 2, px + PLOT // 2, ground, pz + PLOT // 2, plan.path_block))

    building_commands: list[list[str]] = []
    built: list[str] = []
    villagers: list[tuple[int, int, int]] = []
    world_ops = [clear, *village_ops]

    def place(ops: list[BuildOp], front: tuple[int, int], direction: tuple[int, int]) -> None:
        placement = Placement(ops, standing_at(front[0], ground + 1, front[1], direction), gap=0)
        placed = [placement.op(op) for op in ops]
        world_ops.extend(placed)
        building_commands.append(to_commands(placed))

    if centrepiece:
        fx, fz = facing
        # Centred on the square, its front towards the spawn point.
        depth = bounding_size(centrepiece[1])[2]
        place(centrepiece[1], (px - fx * (depth // 2), pz - fz * (depth // 2)), facing)
        built.append(centrepiece[0].name)
    for (building, ops), plot in zip(others, plots):
        place(ops, plot.front, plot.facing)
        built.append(building.name)
        (fx, fz), (x, z) = plot.facing, plot.front
        villagers.append((x - fx * 2, ground + 1, z - fz * 2))

    # Stand in the square, in front of the centrepiece, looking at it.
    fx, fz = facing
    spawn = (px - fx * (PLOT // 2), ground + 1, pz - fz * (PLOT // 2))
    return Village(
        plan=plan,
        ground=ground,
        centre=centre,
        area=BuildOp(x1, ground, z1, x2, ground + CLEAR_HEIGHT, z2, "air"),
        clear_commands=to_commands([clear]),
        commands=to_commands(village_ops) + [c for cmds in building_commands for c in cmds],
        building_commands=building_commands,
        villagers=villagers,
        world_ops=world_ops,
        built=built,
        failed=failed,
        free_plot=free_plot,
        spawn=spawn,
    )


Status = Callable[[str], None]


async def design_village(
    llm: ChatModel,
    conn: MinecraftConnection,
    player: str,
    style: str,
    count: int = DEFAULT_BUILDINGS,
    reserve_plot: bool = False,
    progress: Progress | None = None,
    on_status: Status = lambda status: None,
) -> Village:
    """Plan, design and lay out a village around the player (nothing is placed yet)."""
    progress = progress or Progress.silent()
    on_status(f"planning {count} buildings ({style or 'style of its choice'})")
    progress.stage("The AI is planning the village")
    plan = await plan_village(llm, style, count)
    on_status(f"planned {plan.name}: {plan.style}, {len(plan.buildings)} buildings")
    await progress.say(f"Planned {plan.name}, a {plan.style} village. Designing {len(plan.buildings)} buildings, this takes a few minutes...")
    progress.stage("The AI is designing the buildings", total=len(plan.buildings))

    async def on_designed(done: int, total: int) -> None:
        on_status(f"designed {done}/{total} buildings")
        progress.tick()
        if done % 5 == 0 and done < total:
            await progress.say(f"Designed {done} of {total} buildings...")

    designs = await design_buildings(llm, plan, on_designed)
    if not any(ops for _, ops, _ in designs):
        raise BuildError("None of the buildings could be designed.")

    try:
        info = await conn.query_player(player)
    except Exception as exc:
        raise BuildError("I couldn't find where you are. Are cheats turned on in this world?") from exc
    here = PlayerPosition(info["position"]["x"], info["position"]["y"], info["position"]["z"], info.get("yRot", 0.0))
    return lay_out(plan, designs, here.feet, facing_from_yaw(here.yaw), reserve_plot)


async def summon_villagers(conn: MinecraftConnection, positions: list[tuple[int, int, int]]) -> int:
    """Summon a villager at each position. Returns how many were summoned."""
    if not positions:
        return 0
    entity = "villager_v2"
    first = await conn.run_command(f"summon {entity} {positions[0][0]} {positions[0][1]} {positions[0][2]}")
    if first.get("statusCode", 0) < 0:
        entity = "villager"  # older versions only know the classic villager
        first = await conn.run_command(f"summon {entity} {positions[0][0]} {positions[0][1]} {positions[0][2]}")
    ok = int(first.get("statusCode", 0) >= 0)
    errors = await conn.run_commands([f"summon {entity} {x} {y} {z}" for x, y, z in positions[1:]])
    return ok + len(positions) - 1 - len(errors)


async def build_village(
    conn: MinecraftConnection, player: str, village: Village, progress: Progress | None = None,
    on_status: Status = lambda status: None,
) -> tuple[int, int]:
    """Place a designed village with villagers, moving the player to the square. Returns (failed commands, villagers)."""
    progress = progress or Progress.silent()
    on_status("clearing the area and laying out streets")
    await progress.say("Clearing the area and laying out the streets...")
    ground_count = len(village.commands) - sum(len(c) for c in village.building_commands)
    progress.stage("Clearing the area and laying out the streets", total=len(village.clear_commands) + ground_count)
    errors = await conn.run_commands(village.clear_commands, on_done=progress.tick)
    errors += await conn.run_commands(village.commands[:ground_count], on_done=progress.tick)
    # Move the player out of the way (the centrepiece goes where they stand) before building.
    x, y, z = village.spawn
    cx, cz = village.centre
    await conn.run_command(f"tp {quote_target(player)} {x + 0.5} {y} {z + 0.5} facing {cx + 0.5} {y + 1} {cz + 0.5}")
    total = len(village.building_commands)
    progress.stage("Building the village", total=sum(len(c) for c in village.building_commands))
    for n, commands in enumerate(village.building_commands, 1):
        errors += await conn.run_commands(commands, on_done=progress.tick)
        on_status(f"built {n}/{total} buildings")
        if n % 5 == 0 and n < total:
            await progress.say(f"Built {n} of {total} buildings...")
    progress.stage("Summoning villagers")
    villagers = await summon_villagers(conn, village.villagers)
    on_status(f"summoned {villagers} villagers")
    return len([e for e in errors if not _NOTHING_CHANGED.search(e)]), villagers


def parse_village_args(args: str) -> tuple[str, int]:
    """'viking 12' -> ('viking', 12). The count is optional (default 20, 4-24)."""
    match = re.search(r"(?:^|\s)(\d+)\s*$", args)
    count = DEFAULT_BUILDINGS
    if match:
        count = max(4, min(MAX_BUILDINGS, int(match.group(1))))
        args = args[: match.start()]
    return args.strip()[:100], count
