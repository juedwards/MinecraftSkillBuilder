"""`!map`: build a real place from OpenStreetMap data, live, around the player.

Our own implementation of the approach used by Arnis (https://github.com/louis-e/arnis):
geocode the place, download its OpenStreetMap features from the Overpass API, project them onto
a block grid, rasterise areas and lines, extrude buildings to their real heights, and place it
all with `fill` commands. The terrain is flat for now.

Map data © OpenStreetMap contributors, available under the Open Database Licence (ODbL).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import aiohttp

from .builder import _NOTHING_CHANGED, BuildError, BuildOp, PlayerPosition, add_supports, to_commands
from .llm import ChatModel
from .minecraft import MinecraftConnection, quote_target
from .progress import Progress

USER_AGENT = "MinecraftSkillBuilder/0.2 (+https://github.com/juedwards/MinecraftSkillBuilder)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_ROUNDS = 2  # tries through the server list (worst case about 1.5 minutes)
MAP_CACHE_DIR = Path(".mapcache")
MAP_CACHE_SECONDS = 30 * 24 * 3600  # reuse downloaded map data for 30 days
ATTRIBUTION = "Map data © OpenStreetMap contributors"

DEFAULT_SIZE = 96  # blocks along each side
MIN_SIZE, MAX_SIZE = 32, 128
DEFAULT_SCALE = 2.0  # metres per block
MIN_SCALE, MAX_SCALE = 0.5, 10.0
MAX_BUILDING_BLOCKS = 40
METRES_PER_LEVEL = 3.0
MAX_ELEMENTS = 30_000

Cell = tuple[int, int]  # grid x, z relative to the centre (north is -z)
Point = tuple[float, float]  # projected x, z in blocks

# --- Requests -------------------------------------------------------------------------


@dataclass(frozen=True)
class MapRequest:
    query: str
    size: int = DEFAULT_SIZE
    scale: float = DEFAULT_SCALE
    bridges: bool = True


def parse_map_args(text: str) -> MapRequest:
    """'tower bridge london 120 3' or 'Bath size 80 scale 3 without bridges' -> MapRequest."""
    text = " ".join(text.split())
    size, scale, bridges = DEFAULT_SIZE, DEFAULT_SCALE, True
    if re.search(r"\b(without|no) bridges?\b", text, re.IGNORECASE):
        bridges = False
        text = re.sub(r"\b(without|no) bridges?\b", " ", text, flags=re.IGNORECASE)
    if match := re.search(r"\bsize\s+(\d+)\b", text, re.IGNORECASE):
        size = int(match.group(1))
        text = text[: match.start()] + text[match.end():]
    if match := re.search(r"\bscale\s+(\d+(?:\.\d+)?)\b", text, re.IGNORECASE):
        scale = float(match.group(1))
        text = text[: match.start()] + text[match.end():]
    numbers = re.search(r"(?:^|\s)(\d+)(?:\s+(\d+(?:\.\d+)?))?\s*$", text)
    if numbers:
        size = int(numbers.group(1))
        if numbers.group(2):
            scale = float(numbers.group(2))
        text = text[: numbers.start()]
    query = text.strip(" ,.")[:120]
    return MapRequest(
        query=query,
        size=max(MIN_SIZE, min(MAX_SIZE, size)),
        scale=max(MIN_SCALE, min(MAX_SCALE, scale)),
        bridges=bridges,
    )


# --- OpenStreetMap ---------------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float


class MapSource(Protocol):
    async def geocode(self, query: str) -> Place | None: ...
    async def features(self, south: float, west: float, north: float, east: float) -> list[dict[str, Any]]: ...


def overpass_query(south: float, west: float, north: float, east: float) -> str:
    b = f"{south:.6f},{west:.6f},{north:.6f},{east:.6f}"
    return f"""[out:json][timeout:40];
(
  way["building"]({b});
  relation["building"]["type"="multipolygon"]({b});
  way["highway"]({b});
  way["railway"~"^(rail|light_rail|tram|subway|narrow_gauge)$"]({b});
  way["waterway"~"^(river|canal|stream|ditch|drain|riverbank)$"]({b});
  way["natural"~"^(water|wood|scrub|sand|beach|grassland|wetland|heath)$"]({b});
  relation["natural"~"^(water|wood)$"]({b});
  way["water"]({b});
  way["landuse"]({b});
  relation["landuse"]({b});
  way["leisure"~"^(park|garden|pitch|playground|common|nature_reserve|golf_course)$"]({b});
  relation["leisure"="park"]({b});
  way["amenity"="parking"]({b});
  node["natural"="tree"]({b});
);
out geom;"""


class OpenStreetMap:
    """Nominatim for geocoding and Overpass for features (both free, with usage limits).

    Downloads are cached on disk, so building the same place again (or retrying a map challenge)
    doesn't depend on the public servers, which are often busy.
    """

    def __init__(
        self, timeout: float = 30, urls: tuple[str, ...] = OVERPASS_URLS, cache_dir: Path | None = MAP_CACHE_DIR,
    ):
        self._timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=5)  # unreachable servers fail fast
        self._urls = urls
        self._cache_dir = cache_dir

    def _cache_path(self, query: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"{hashlib.sha256(query.encode()).hexdigest()[:24]}.json"

    def _cached(self, query: str) -> list[dict[str, Any]] | None:
        path = self._cache_path(query)
        if path is None or not path.is_file() or time.time() - path.stat().st_mtime > MAP_CACHE_SECONDS:
            return None
        try:
            return list(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def _store(self, query: str, elements: list[dict[str, Any]]) -> None:
        path = self._cache_path(query)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(elements), encoding="utf-8")
        except OSError:
            pass  # caching is only an optimisation

    async def geocode(self, query: str) -> Place | None:
        params = {"q": query, "format": "jsonv2", "limit": "1"}
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=self._timeout) as session:
            async with session.get(NOMINATIM_URL, params=params) as response:
                response.raise_for_status()
                results = await response.json()
        if not results:
            return None
        top = results[0]
        return Place(str(top.get("display_name") or query), float(top["lat"]), float(top["lon"]))

    async def features(self, south: float, west: float, north: float, east: float) -> list[dict[str, Any]]:
        """Download features (or use the cache), retrying: the public Overpass servers often answer 429/504 when busy."""
        query = overpass_query(south, west, north, east)
        if (cached := self._cached(query)) is not None:
            return cached
        last_error = ""
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=self._timeout) as session:
            for attempt in range(OVERPASS_ROUNDS):
                if attempt:
                    await asyncio.sleep(3)
                for url in self._urls:
                    try:
                        async with session.post(url, data={"data": query}) as response:
                            response.raise_for_status()
                            data = await response.json(content_type=None)
                            elements = list(data.get("elements", []))
                            self._store(query, elements)
                            return elements
                    except Exception as exc:  # busy or unreachable: try the next server
                        last_error = f"{type(exc).__name__} {exc}".strip()
        raise BuildError(
            "the free OpenStreetMap servers are too busy right now. Try again in a few minutes, "
            f"or try somewhere else. ({last_error[:80]})"
        )


# --- Projection and rasterising --------------------------------------------------------


class Projection:
    """Local flat projection around the centre: x east, z south, in blocks."""

    def __init__(self, lat: float, lon: float, scale: float):
        self.lat, self.lon, self.scale = lat, lon, scale
        self._mx = 111_320.0 * math.cos(math.radians(lat))
        self._mz = 110_540.0

    def __call__(self, lat: float, lon: float) -> Point:
        return (lon - self.lon) * self._mx / self.scale, (self.lat - lat) * self._mz / self.scale

    def bbox(self, size: int) -> tuple[float, float, float, float]:
        """(south, west, north, east) covering a square of `size` blocks around the centre."""
        half_m = (size / 2 + 2) * self.scale
        dlat, dlon = half_m / self._mz, half_m / self._mx
        return self.lat - dlat, self.lon - dlon, self.lat + dlat, self.lon + dlon


def fill_polygon(ring: list[Point], lo: int, hi: int) -> set[Cell]:
    """Cells whose centres are inside the polygon (even-odd rule), within [lo, hi] on both axes."""
    cells: set[Cell] = set()
    if len(ring) < 3:
        return cells
    edges = list(zip(ring, ring[1:] + ring[:1]))
    z_min = max(math.floor(min(p[1] for p in ring)), lo)
    z_max = min(math.ceil(max(p[1] for p in ring)), hi)
    for gz in range(z_min, z_max + 1):
        zc = gz + 0.5
        xs = sorted(
            x1 + (zc - z1) * (x2 - x1) / (z2 - z1)
            for (x1, z1), (x2, z2) in edges
            if (z1 <= zc < z2) or (z2 <= zc < z1)
        )
        for a, b in zip(xs[::2], xs[1::2]):
            for gx in range(max(math.ceil(a - 0.5), lo), min(math.ceil(b - 0.5) - 1, hi) + 1):
                cells.add((gx, gz))
    return cells


def thick_line(points: list[Point], width: float, lo: int, hi: int) -> set[Cell]:
    """Cells within width/2 of a polyline, within [lo, hi]."""
    cells: set[Cell] = set()
    r = max(width / 2, 0.5)
    reach = math.ceil(r)
    for (x1, z1), (x2, z2) in zip(points, points[1:]):
        if max(x1, x2) < lo - r or min(x1, x2) > hi + 1 + r or max(z1, z2) < lo - r or min(z1, z2) > hi + 1 + r:
            continue
        steps = max(1, int(math.hypot(x2 - x1, z2 - z1) * 2))
        for i in range(steps + 1):
            x, z = x1 + (x2 - x1) * i / steps, z1 + (z2 - z1) * i / steps
            cx, cz = math.floor(x), math.floor(z)
            for gx in range(cx - reach, cx + reach + 1):
                for gz in range(cz - reach, cz + reach + 1):
                    if lo <= gx <= hi and lo <= gz <= hi and (gx + 0.5 - x) ** 2 + (gz + 0.5 - z) ** 2 <= r * r:
                        cells.add((gx, gz))
    return cells


def assemble_rings(segments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Join way segments that share end points into closed rings (multipolygon members come in pieces)."""
    pending = [list(s) for s in segments if len(s) >= 2]
    rings = []
    while pending:
        ring = pending.pop(0)
        while ring[0] != ring[-1]:
            for i, seg in enumerate(pending):
                if seg[0] == ring[-1]:
                    ring += seg[1:]
                elif seg[-1] == ring[-1]:
                    ring += seg[-2::-1]
                elif seg[-1] == ring[0]:
                    ring = seg[:-1] + ring
                elif seg[0] == ring[0]:
                    ring = seg[:0:-1] + ring
                else:
                    continue
                pending.pop(i)
                break
            else:
                break  # can't close it; fill_polygon closes it implicitly
        rings.append(ring)
    return rings


def rectangles(cells: set[Cell]) -> list[tuple[int, int, int, int]]:
    """Cover cells exactly with axis-aligned rectangles (x1, z1, x2, z2): row runs merged downwards."""
    rows: dict[int, list[int]] = defaultdict(list)
    for x, z in cells:
        rows[z].append(x)
    result: list[tuple[int, int, int, int]] = []
    open_runs: dict[tuple[int, int], tuple[int, int]] = {}  # (x1, x2) -> (z_start, z_last)
    for z in sorted(rows):
        xs = sorted(rows[z])
        runs, start = set(), xs[0]
        for a, b in zip(xs, xs[1:] + [None]):
            if b != a + 1:
                runs.add((start, a))
                start = b
        still_open = {}
        for run, (z_start, z_last) in open_runs.items():
            if run in runs and z_last == z - 1:
                still_open[run] = (z_start, z)
                runs.discard(run)
            else:
                result.append((run[0], z_start, run[1], z_last))
        for run in runs:
            still_open[run] = (z, z)
        open_runs = still_open
    result += [(run[0], z_start, run[1], z_last) for run, (z_start, z_last) in open_runs.items()]
    return result


# --- Interpreting OpenStreetMap tags ----------------------------------------------------

AREA_SURFACES = {
    ("landuse", "grass"): "grass_block", ("landuse", "meadow"): "grass_block",
    ("landuse", "recreation_ground"): "grass_block", ("landuse", "village_green"): "grass_block",
    ("landuse", "cemetery"): "grass_block", ("landuse", "allotments"): "podzol",
    ("landuse", "farmland"): "dirt", ("landuse", "orchard"): "grass_block", ("landuse", "vineyard"): "grass_block",
    ("landuse", "forest"): "grass_block", ("landuse", "commercial"): "smooth_stone",
    ("landuse", "retail"): "smooth_stone", ("landuse", "industrial"): "light_gray_concrete",
    ("landuse", "railway"): "gravel", ("landuse", "construction"): "dirt", ("landuse", "brownfield"): "dirt",
    ("leisure", "park"): "grass_block", ("leisure", "garden"): "grass_block", ("leisure", "pitch"): "moss_block",
    ("leisure", "playground"): "sand", ("leisure", "common"): "grass_block", ("leisure", "golf_course"): "grass_block",
    ("leisure", "nature_reserve"): "grass_block", ("natural", "wood"): "grass_block", ("natural", "scrub"): "moss_block",
    ("natural", "heath"): "podzol", ("natural", "grassland"): "grass_block", ("natural", "sand"): "sand",
    ("natural", "beach"): "sand", ("natural", "wetland"): "moss_block", ("amenity", "parking"): "light_gray_concrete",
    ("highway", "pedestrian"): "polished_andesite",
}
WOODED = {("landuse", "forest"): 9, ("natural", "wood"): 9, ("leisure", "park"): 45, ("landuse", "orchard"): 16}
WATER_AREAS = {("natural", "water"), ("landuse", "reservoir"), ("landuse", "basin"), ("waterway", "riverbank")}
WATERWAY_WIDTHS = {"river": 12.0, "canal": 8.0, "stream": 2.0, "ditch": 1.0, "drain": 1.0}
ROAD_WIDTHS = {
    "motorway": 12.0, "trunk": 10.0, "primary": 9.0, "secondary": 8.0, "tertiary": 7.0, "residential": 6.0,
    "unclassified": 6.0, "living_street": 5.0, "service": 4.0, "pedestrian": 4.0, "track": 3.0,
    "motorway_link": 6.0, "trunk_link": 6.0, "primary_link": 6.0, "secondary_link": 6.0, "tertiary_link": 5.0,
    "footway": 2.0, "path": 2.0, "cycleway": 2.0, "steps": 2.0, "bridleway": 2.0,
}
ROAD_BLOCKS = {"footway": "polished_andesite", "path": "polished_andesite", "steps": "polished_andesite",
               "pedestrian": "polished_andesite", "cycleway": "red_terracotta", "bridleway": "dirt", "track": "dirt"}
RAILWAY_BLOCKS = {"rail": "cobblestone", "narrow_gauge": "cobblestone", "light_rail": "andesite", "tram": "andesite", "subway": "andesite"}

RESIDENTIAL = {"house", "residential", "detached", "semidetached_house", "terrace", "bungalow", "apartments", "cabin", "farm", "dormitory"}
SMALL = {"garage", "garages", "shed", "hut", "carport", "kiosk", "toilets"}
BUILDING_WALLS = {
    **{t: "brick_block" for t in RESIDENTIAL | {"school", "university", "college", "kindergarten"}},
    **{t: "oak_planks" for t in SMALL},
    **{t: "white_concrete" for t in ("commercial", "retail", "office", "hospital", "hotel", "supermarket")},
    **{t: "light_gray_concrete" for t in ("industrial", "warehouse", "train_station", "transportation", "parking")},
    **{t: "polished_andesite" for t in ("church", "cathedral", "chapel", "civic", "public", "government", "museum")},
    "castle": "stone", "tower": "stone", "bridge": "stone", "barn": "spruce_planks", "greenhouse": "glass",
}
MATERIAL_WALLS = {
    "brick": "brick_block", "stone": "stone", "sandstone": "sandstone", "limestone": "smooth_stone",
    "wood": "oak_planks", "timber_framing": "oak_planks", "glass": "glass", "concrete": "light_gray_concrete",
    "metal": "iron_block", "steel": "iron_block", "plaster": "white_concrete", "marble": "quartz_block", "granite": "polished_granite",
}
CONCRETE_RGB = {
    "white": (207, 213, 214), "orange": (224, 97, 0), "magenta": (169, 48, 159), "light_blue": (35, 137, 198),
    "yellow": (241, 175, 21), "lime": (94, 168, 24), "pink": (213, 101, 142), "gray": (54, 57, 61),
    "light_gray": (125, 125, 115), "cyan": (21, 119, 136), "purple": (100, 31, 156), "blue": (44, 46, 143),
    "brown": (96, 59, 31), "green": (73, 91, 36), "red": (142, 32, 32), "black": (8, 10, 15),
}
COLOUR_NAMES = {
    "grey": "gray", "silver": "light_gray", "lightgrey": "light_gray", "lightgray": "light_gray", "beige": "white",
    "cream": "white", "ivory": "white", "tan": "brown", "maroon": "red", "navy": "blue", "darkgreen": "green",
    "gold": "yellow", "violet": "purple", **{c: c for c in CONCRETE_RGB},
}


def colour_block(value: str | None) -> str | None:
    """'#aa3322' or 'red' -> the nearest concrete colour block."""
    if not value:
        return None
    value = value.strip().lower().replace(" ", "")
    if value in COLOUR_NAMES:
        return f"{COLOUR_NAMES[value]}_concrete"
    match = re.fullmatch(r"#?([0-9a-f]{6}|[0-9a-f]{3})", value)
    if not match:
        return None
    hex_ = match.group(1)
    if len(hex_) == 3:
        hex_ = "".join(c * 2 for c in hex_)
    rgb = tuple(int(hex_[i:i + 2], 16) for i in (0, 2, 4))
    name = min(CONCRETE_RGB, key=lambda c: sum((a - b) ** 2 for a, b in zip(CONCRETE_RGB[c], rgb)))
    return f"{name}_concrete"


def _metres(value: str | None) -> float | None:
    if not value:
        return None
    match = re.match(r"\s*(\d+(?:\.\d+)?)", value.replace(",", "."))
    return float(match.group(1)) if match else None


def building_height(tags: dict[str, str], scale: float) -> int:
    """Height in blocks from height / building:levels tags, or a guess from the building type."""
    metres = _metres(tags.get("height")) or _metres(tags.get("building:height"))
    if metres is None:
        levels = _metres(tags.get("building:levels"))
        if levels is not None:
            metres = (levels + (_metres(tags.get("roof:levels")) or 0)) * METRES_PER_LEVEL
    if metres is None:
        kind = tags.get("building", "yes")
        metres = 3.0 if kind in SMALL else 6.0 if kind in RESIDENTIAL else 9.0
    return max(3, min(MAX_BUILDING_BLOCKS, round(metres / scale)))


def building_materials(tags: dict[str, str]) -> tuple[str, str]:
    """(wall block, roof block)."""
    kind = tags.get("building", "yes")
    wall = (
        colour_block(tags.get("building:colour"))
        or MATERIAL_WALLS.get(tags.get("building:material", "").lower())
        or BUILDING_WALLS.get(kind, "polished_andesite")
    )
    roof = (
        colour_block(tags.get("roof:colour"))
        or ("dark_oak_planks" if kind in RESIDENTIAL else "spruce_planks" if kind in SMALL else "smooth_stone")
    )
    return wall, roof


# --- Layout ----------------------------------------------------------------------------


@dataclass
class Building:
    cells: set[Cell]
    height: int
    wall: str
    roof: str


@dataclass
class MapLayout:
    half: int
    surface: dict[Cell, str]  # ground block per cell (water cells excluded)
    water: set[Cell]
    buildings: list[Building]
    trees: list[Cell]
    counts: dict[str, int] = field(default_factory=dict)

    def is_open(self, cell: Cell) -> bool:
        return cell not in self.water and not any(cell in b.cells for b in self.buildings)

    def nearest_open(self, cell: Cell) -> Cell:
        """The open ground cell (not water or building) nearest to `cell`."""
        blocked = self.water.union(*(b.cells for b in self.buildings)) if self.buildings else set(self.water)
        for radius in range(self.half * 2 + 1):
            ring = [
                (cell[0] + dx, cell[1] + dz)
                for dx in range(-radius, radius + 1) for dz in range(-radius, radius + 1)
                if max(abs(dx), abs(dz)) == radius
            ]
            for c in sorted(ring, key=lambda c: (c[0] - cell[0]) ** 2 + (c[1] - cell[1]) ** 2):
                if c not in blocked and -self.half <= c[0] <= self.half and -self.half <= c[1] <= self.half:
                    return c
        return cell


def _tag_key(tags: dict[str, str], table: dict | set) -> tuple[str, str] | None:
    for key in ("leisure", "natural", "landuse", "amenity", "waterway", "highway"):
        if (key, tags.get(key, "")) in table:
            return key, tags[key]
    return None


def _rings(element: dict[str, Any], role: str = "outer") -> list[list[tuple[float, float]]]:
    """(lat, lon) rings of a way or a multipolygon relation."""
    if element.get("type") == "way":
        return [[(p["lat"], p["lon"]) for p in element.get("geometry", [])]]
    segments = [
        [(p["lat"], p["lon"]) for p in member.get("geometry", [])]
        for member in element.get("members", [])
        if member.get("type") == "way" and member.get("role", "outer") in (role, "" if role == "outer" else role)
    ]
    return assemble_rings(segments)


def _area_cells(element: dict[str, Any], project: Projection, lo: int, hi: int) -> set[Cell]:
    cells: set[Cell] = set()
    for ring in _rings(element, "outer"):
        cells |= fill_polygon([project(*p) for p in ring], lo, hi)
    if element.get("type") == "relation":
        for ring in _rings(element, "inner"):
            cells -= fill_polygon([project(*p) for p in ring], lo, hi)
    return cells


def _line_points(element: dict[str, Any], project: Projection) -> list[Point]:
    return [project(p["lat"], p["lon"]) for p in element.get("geometry", [])]


def _scatter(cells: set[Cell], every: int) -> list[Cell]:
    """Deterministic, roughly even sprinkle of cells (for trees), at least 3 apart."""
    picked: list[Cell] = []
    taken: set[Cell] = set()
    for x, z in sorted(cells):
        if (x * 73856093 ^ z * 19349663) % every == 0 and not any((x + dx, z + dz) in taken for dx in range(-2, 3) for dz in range(-2, 3)):
            picked.append((x, z))
            taken.add((x, z))
    return picked


def lay_out_map(elements: list[dict[str, Any]], project: Projection, size: int, bridges: bool = True) -> MapLayout:
    half = size // 2
    lo, hi = -half, half - 1 if size % 2 == 0 else half
    surface: dict[Cell, str] = {(x, z): "grass_block" for x in range(lo, hi + 1) for z in range(lo, hi + 1)}
    water: set[Cell] = set()
    buildings: list[Building] = []
    tree_cells: set[Cell] = set()
    counts: dict[str, int] = defaultdict(int)

    areas, water_areas, waterways, railways, roads, building_elements = [], [], [], [], [], []
    for element in elements:
        tags = element.get("tags", {})
        kind = element.get("type")
        if kind == "node":
            if tags.get("natural") == "tree":
                x, z = project(element["lat"], element["lon"])
                tree_cells.add((math.floor(x), math.floor(z)))
            continue
        if not bridges and (tags.get("building") == "bridge" or tags.get("man_made") == "bridge"):
            counts["bridges skipped"] += 1
            continue
        if "building" in tags and tags.get("building") not in ("no", "roof"):
            building_elements.append(element)
        elif _tag_key(tags, WATER_AREAS) or (kind == "way" and "water" in tags and "natural" not in tags):
            water_areas.append(element)
        elif kind == "way" and tags.get("waterway") in WATERWAY_WIDTHS:
            waterways.append(element)
        elif kind == "way" and tags.get("railway") in RAILWAY_BLOCKS and tags.get("tunnel") in (None, "no"):
            railways.append(element)
        elif kind == "way" and tags.get("highway") in ROAD_WIDTHS and tags.get("area") != "yes":
            if tags.get("tunnel") not in (None, "no", "building_passage"):
                continue
            if not bridges and tags.get("bridge") not in (None, "no"):
                counts["bridges skipped"] += 1
                continue
            roads.append(element)
        elif _tag_key(tags, AREA_SURFACES):
            areas.append(element)

    # Areas: biggest first, so smaller (more specific) areas inside them win.
    area_cells = [(element, _area_cells(element, project, lo, hi)) for element in areas]
    for element, cells in sorted(area_cells, key=lambda item: -len(item[1])):
        tags = element["tags"]
        key = _tag_key(tags, AREA_SURFACES)
        for cell in cells:
            surface[cell] = AREA_SURFACES[key]
        if (every := WOODED.get(_tag_key(tags, WOODED) or ("", ""))):
            tree_cells.update(_scatter(cells, every))
    for element in water_areas:
        water |= _area_cells(element, project, lo, hi)
    for element in waterways:
        width = _metres(element["tags"].get("width")) or WATERWAY_WIDTHS[element["tags"]["waterway"]]
        water |= thick_line(_line_points(element, project), width / project.scale, lo, hi)
    for element in railways:
        block = RAILWAY_BLOCKS[element["tags"]["railway"]]
        for cell in thick_line(_line_points(element, project), 3.0 / project.scale, lo, hi):
            surface[cell] = block
            water.discard(cell)
        counts["railways"] += 1
    for element in roads:
        tags = element["tags"]
        width = _metres(tags.get("width")) or ROAD_WIDTHS[tags["highway"]]
        block = ROAD_BLOCKS.get(tags["highway"], "gray_concrete")
        for cell in thick_line(_line_points(element, project), width / project.scale, lo, hi):
            surface[cell] = block
            water.discard(cell)  # bridges cross the water at ground level
        counts["roads"] += 1
    for element in building_elements:
        cells = _area_cells(element, project, lo, hi)
        if not cells:
            continue
        wall, roof = building_materials(element["tags"])
        buildings.append(Building(cells, building_height(element["tags"], project.scale), wall, roof))
        water -= cells
    for cell in water:
        surface.pop(cell, None)

    blocked = water.union(*(b.cells for b in buildings)) if buildings else set(water)
    trees = [c for c in sorted(tree_cells) if c not in blocked and surface.get(c) == "grass_block" and lo + 1 <= c[0] <= hi - 1 and lo + 1 <= c[1] <= hi - 1]
    counts.update({"buildings": len(buildings), "trees": len(trees), "water blocks": len(water)})
    return MapLayout(half, surface, water, buildings, trees, dict(counts))


def map_ops(layout: MapLayout, centre: tuple[int, int], ground: int) -> tuple[BuildOp, list[BuildOp]]:
    """(clear op, build ops) in world coordinates."""
    cx, cz = centre
    xs = [c[0] for c in layout.surface] + [c[0] for c in layout.water]
    zs = [c[1] for c in layout.surface] + [c[1] for c in layout.water]
    x1, x2, z1, z2 = cx + min(xs), cx + max(xs), cz + min(zs), cz + max(zs)
    top = max([b.height for b in layout.buildings] + [6]) + 2
    clear = BuildOp(x1, ground + 1, z1, x2, ground + top, z2, "air")

    def flat(cells: set[Cell], y1: int, y2: int, block: str, mode: str = "") -> list[BuildOp]:
        return [BuildOp(cx + a, y1, cz + b, cx + c, y2, cz + d, block, mode) for a, b, c, d in rectangles(cells)]

    ops = [BuildOp(x1, ground - 1, z1, x2, ground - 1, z2, "dirt")]
    by_block: dict[str, set[Cell]] = defaultdict(set)
    for cell, block in layout.surface.items():
        by_block[block].add(cell)
    for block, cells in sorted(by_block.items(), key=lambda item: -len(item[1])):
        ops += flat(cells, ground, ground, block)
    ops += flat(layout.water, ground - 1, ground, "water")

    for building in layout.buildings:
        top_y = ground + building.height
        walls = {c for c in building.cells if any((c[0] + dx, c[1] + dz) not in building.cells for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)))}
        ops += flat(building.cells, ground, ground, "smooth_stone")
        ops += flat(walls, ground + 1, top_y, building.wall)
        if building.wall != "glass":
            for y in range(ground + 2, top_y - 1, 3):
                ops += flat(walls, y, y, "glass")
        ops += flat(building.cells, top_y + 1, top_y + 1, building.roof)

    for gx, gz in layout.trees:
        x, z = cx + gx, cz + gz
        ops += [
            BuildOp(x - 1, ground + 3, z - 1, x + 1, ground + 4, z + 1, "oak_leaves"),
            BuildOp(x, ground + 5, z, x, ground + 5, z, "oak_leaves"),
            BuildOp(x, ground + 1, z, x, ground + 4, z, "oak_log"),
        ]
    return clear, add_supports(ops)


# --- Building it in the world ----------------------------------------------------------


@dataclass
class MapScene:
    place: Place
    request: MapRequest
    ground: int
    centre: tuple[int, int]
    layout: MapLayout
    clear_commands: list[str]
    commands: list[str]
    world_ops: list[BuildOp]

    def world(self, cell: Cell) -> tuple[int, int, int]:
        return self.centre[0] + cell[0], self.ground + 1, self.centre[1] + cell[1]

    @property
    def spawn(self) -> tuple[int, int, int]:
        return self.world(self.layout.nearest_open((0, 0)))

    def summary(self) -> str:
        c = self.layout.counts
        parts = [f"{c.get('buildings', 0)} buildings", f"{c.get('roads', 0)} roads and paths"]
        if c.get("water blocks"):
            parts.append("water")
        if c.get("trees"):
            parts.append(f"{c['trees']} trees")
        return ", ".join(parts)


Status = Callable[[str], None]


async def find_place(source: MapSource, query: str, llm: ChatModel | None) -> Place:
    place = await source.geocode(query)
    if place is None and llm is not None:
        # Let the AI turn a loose description into something the geocoder knows.
        suggestion = await llm.complete([
            {"role": "system", "content": "Reply with only a short place search query (landmark or address, town, country) for OpenStreetMap Nominatim. No other text."},
            {"role": "user", "content": query},
        ])
        suggestion = suggestion.strip().strip('"').splitlines()[0][:120] if suggestion.strip() else ""
        if suggestion and suggestion.lower() != query.lower():
            place = await source.geocode(suggestion)
    if place is None:
        raise BuildError(f"I couldn't find '{query}' on the map. Try adding the town or country.")
    return place


async def design_map(
    source: MapSource,
    conn: MinecraftConnection,
    player: str,
    request: MapRequest,
    llm: ChatModel | None = None,
    progress: Progress | None = None,
    on_status: Status = lambda status: None,
) -> MapScene:
    """Find the place, download its map data and lay it out around the player (nothing placed yet)."""
    progress = progress or Progress.silent()
    if not request.query:
        raise BuildError("tell me where, e.g. !map tower bridge london")
    on_status(f"finding {request.query}")
    progress.stage(f"Looking up {request.query}")
    place = await find_place(source, request.query, llm)
    on_status(f"found {place.name} ({place.lat:.5f}, {place.lon:.5f})")
    await progress.say(f"Found {place.name.split(',')[0]}. Downloading the map ({request.size}x{request.size} blocks, {request.scale:g} m per block)...")
    progress.stage("Downloading map data from OpenStreetMap (the map servers can be slow)")
    project = Projection(place.lat, place.lon, request.scale)
    elements = await source.features(*project.bbox(request.size))
    progress.stage("Turning the map into blocks")
    if len(elements) > MAX_ELEMENTS:
        raise BuildError("that area has too much map data. Try a smaller size or a bigger scale.")
    layout = lay_out_map(elements, project, request.size, request.bridges)
    on_status(f"laid out {len(elements)} map features: {layout.counts}")

    try:
        info = await conn.query_player(player)
    except Exception as exc:
        raise BuildError("I couldn't find where you are. Are cheats turned on in this world?") from exc
    feet = PlayerPosition(info["position"]["x"], info["position"]["y"], info["position"]["z"], info.get("yRot", 0.0)).feet
    centre, ground = (feet[0], feet[2]), feet[1] - 1
    clear, ops = map_ops(layout, centre, ground)
    return MapScene(place, request, ground, centre, layout, to_commands([clear]), to_commands(ops), [clear, *ops])


async def build_map(
    conn: MinecraftConnection, player: str, scene: MapScene, progress: Progress | None = None,
    on_status: Status = lambda status: None,
) -> int:
    """Place a designed map, moving the player to open ground first. Returns failed commands."""
    progress = progress or Progress.silent()
    on_status(f"placing {len(scene.commands)} commands")
    await progress.say(f"Building {scene.summary()} ({len(scene.commands)} commands)...")
    progress.stage("Clearing the area", total=len(scene.clear_commands))
    errors = await conn.run_commands(scene.clear_commands, on_done=progress.tick)
    x, y, z = scene.spawn
    cx, cz = scene.centre
    await conn.run_command(f"tp {quote_target(player)} {x + 0.5} {y} {z + 0.5} facing {cx + 0.5} {y + 1} {cz + 0.5}")
    progress.stage("Building the map", total=len(scene.commands))
    errors += await conn.run_commands(scene.commands, on_done=progress.tick)
    return len([e for e in errors if not _NOTHING_CHANGED.search(e)])
