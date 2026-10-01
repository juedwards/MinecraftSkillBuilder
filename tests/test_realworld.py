import asyncio

import pytest

from mcchat.assessment import RubricStore
from mcchat.bridge import BridgeConfig, ChatBridge
from mcchat.builder import BuildOp
from mcchat.realworld import (
    MapRequest, Place, Projection, assemble_rings, building_height, building_materials, colour_block, fill_polygon,
    lay_out_map, map_ops, parse_map_args, rectangles, thick_line,
)
from test_village import run_with_minecraft

LAT, LON = 51.5, -0.1
PROJECT = Projection(LAT, LON, 2.0)


def latlon(x: float, z: float) -> dict:
    """Inverse of PROJECT for building test geometry in block coordinates."""
    return {"lat": LAT - z * 2.0 / 110_540.0, "lon": LON + x * 2.0 / PROJECT._mx}


def way(tags: dict, points: list[tuple[float, float]], closed: bool = False) -> dict:
    geometry = [latlon(x, z) for x, z in points]
    if closed:
        geometry.append(geometry[0])
    return {"type": "way", "tags": tags, "geometry": geometry}


def square(x1, z1, x2, z2):
    return [(x1, z1), (x2, z1), (x2, z2), (x1, z2)]


# --- Parsing ----------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("tower bridge london", MapRequest("tower bridge london")),
    ("eiffel tower 120 3", MapRequest("eiffel tower", 120, 3.0)),
    ("Bath size 80 scale 4 without bridges", MapRequest("Bath", 80, 4.0, bridges=False)),
    ("big ben 999 0.1", MapRequest("big ben", 128, 0.5)),
    ("", MapRequest("")),
])
def test_parse_map_args(text, expected):
    assert parse_map_args(text) == expected


# --- Geometry ---------------------------------------------------------------------------

def test_projection_round_trip_and_bbox():
    x, z = PROJECT(**latlon(10, -5))
    assert (round(x, 6), round(z, 6)) == (10, -5)
    south, west, north, east = PROJECT.bbox(96)
    assert south < LAT < north and west < LON < east


def test_fill_polygon_square_and_clipping():
    cells = fill_polygon(square(0, 0, 4, 3), -10, 10)
    assert cells == {(x, z) for x in range(4) for z in range(3)}
    assert fill_polygon(square(-50, -50, 50, 50), -2, 2) == {(x, z) for x in range(-2, 3) for z in range(-2, 3)}


def test_thick_line_width():
    cells = thick_line([(0, 0.5), (10, 0.5)], 3, -20, 20)
    assert {z for _, z in cells} == {-1, 0, 1} and {x for x, _ in cells} >= set(range(0, 10))


def test_assemble_rings_joins_split_ways():
    a, b, c, d = (0, 0), (0, 1), (1, 1), (1, 0)
    rings = assemble_rings([[a, b], [c, b], [c, d, a]])  # second piece reversed
    assert len(rings) == 1 and rings[0][0] == rings[0][-1] and set(rings[0]) == {a, b, c, d}


def test_rectangles_cover_cells_exactly():
    cells = {(x, z) for x in range(5) for z in range(3)} | {(10, 10), (11, 10), (2, 7)}
    rects = rectangles(cells)
    covered = [(x, z) for x1, z1, x2, z2 in rects for x in range(x1, x2 + 1) for z in range(z1, z2 + 1)]
    assert sorted(covered) == sorted(cells), "every cell exactly once"
    assert len(rects) == 3


# --- Tags -------------------------------------------------------------------------------

def test_building_height_and_materials():
    assert building_height({"building": "yes", "height": "30 m"}, 2.0) == 15
    assert building_height({"building": "apartments", "building:levels": "4"}, 2.0) == 6
    assert building_height({"building": "house"}, 2.0) == 3
    assert building_height({"building": "yes", "height": "330"}, 2.0) == 160, "capped (the Eiffel Tower at 2 m per block)"
    assert building_materials({"building": "house"}) == ("brick_block", "dark_oak_planks")
    assert building_materials({"building": "yes", "building:colour": "#ffffff", "roof:colour": "red"}) == ("white_concrete", "red_concrete")
    assert building_materials({"building": "office", "building:material": "glass"})[0] == "glass"
    assert colour_block("grey") == "gray_concrete" and colour_block("#2030a0") == "blue_concrete" and colour_block("nonsense") is None


# --- Layout ------------------------------------------------------------------------------

ELEMENTS = [
    way({"leisure": "park"}, square(-20, -20, 0, 0), closed=True),
    way({"natural": "water"}, square(5, -20, 15, 20), closed=True),
    way({"highway": "residential"}, [(-20, 10.5), (20, 10.5)]),
    way({"highway": "primary", "bridge": "yes"}, [(0, -10.5), (20, -10.5)]),
    way({"highway": "service", "tunnel": "yes"}, [(-20, 15), (20, 15)]),
    way({"building": "house", "building:levels": "2"}, square(-10, 2, -4, 8), closed=True),
    {"type": "node", "tags": {"natural": "tree"}, **latlon(-15.5, 15.5)},
]


def test_lay_out_map():
    layout = lay_out_map(ELEMENTS, PROJECT, 40)
    assert layout.half == 20 and len(layout.surface) + len(layout.water) == 40 * 40
    assert layout.surface[(-10, -10)] == "grass_block" and layout.surface[(-18, 10)] == "gray_concrete"
    assert (10, 0) in layout.water and (10, 10) not in layout.water, "the road crosses the water"
    assert (10, -11) not in layout.water, "the bridge crosses the water"
    assert layout.surface[(0, 15)] == "grass_block", "tunnels aren't drawn"
    (house,) = layout.buildings
    assert house.height == 3 and house.wall == "brick_block" and len(house.cells) == 36
    assert (-16, 15) in layout.trees
    assert layout.counts["roads"] == 2 and layout.counts["buildings"] == 1
    assert layout.nearest_open((-7, 5)) not in house.cells


def test_lay_out_map_without_bridges():
    layout = lay_out_map(ELEMENTS, PROJECT, 40, bridges=False)
    assert (10, -11) in layout.water and layout.counts["bridges skipped"] == 1


def test_map_ops_world_coordinates():
    layout = lay_out_map(ELEMENTS, PROJECT, 40)
    clear, ops = map_ops(layout, centre=(100, 200), ground=63)
    assert clear == BuildOp(80, 64, 180, 119, 63 + 6 + 2, 219, "air")  # tallest of (house 3, minimum 6) + 2
    assert ops[0] == BuildOp(80, 62, 180, 119, 62, 219, "dirt")
    walls = [op for op in ops if op.block == "brick_block"]
    assert walls and all(op.y1 == 64 and op.y2 == 66 for op in walls)
    assert any(op.block == "water" and op.y1 == 62 for op in ops)
    assert any(op.block == "stone" and op.mode == "keep" and op.y1 == 61 for op in ops), "water is supported"
    roof = [op for op in ops if op.block == "dark_oak_planks"]
    assert roof and all(op.y1 == 67 for op in roof)


# --- End to end ---------------------------------------------------------------------------

class FakeMap:
    def __init__(self, found: bool = True):
        self.found = found
        self.queries: list[str] = []

    async def geocode(self, query):
        self.queries.append(query)
        return Place("Testford, Testshire", LAT, LON) if self.found else None

    async def features(self, south, west, north, east):
        assert south < LAT < north
        return ELEMENTS


def test_map_end_to_end():
    events = []
    bridge = ChatBridge(None, BridgeConfig(), on_event=events.append, map_source=FakeMap())

    async def script(mc):
        await mc.chat("Steve", "!map testford 40 2")
        await mc.wait_for("Map data © OpenStreetMap contributors", timeout=20)

    mc = run_with_minecraft(bridge, script)
    text = "\n".join(mc.replies)
    trees = len(lay_out_map(ELEMENTS, PROJECT, 40).trees)  # the mapped tree plus some scattered in the park
    assert trees > 1 and f"Welcome to Testford: 1 buildings, 2 roads and paths, water, {trees} trees." in text
    tp = next(c for c in mc.commands if c.startswith("tp "))
    assert mc.commands.index(tp) == 2, "after the clear, before any building"
    # Player feet (10, -60, 20) -> the map is centred there, ground y=-61.
    assert mc.world[(10 - 10, -61, 20 - 10)] == "grass_block"
    assert mc.world[(10 + 10, -61, 20)] == "water"
    assert mc.world[(10 - 7, -60, 20 + 2)] == "brick_block", "house walls"
    assert bridge.building is None


def test_map_not_found_uses_ai_suggestion():
    class Suggester:
        async def complete(self, messages):
            return "Testford, Testshire"

    source = FakeMap(found=False)
    bridge = ChatBridge(Suggester(), BridgeConfig(), map_source=source)

    async def script(mc):
        await mc.chat("Steve", "!map the place with the big wheel")
        await mc.wait_for("Map failed: I couldn't find", timeout=10)

    run_with_minecraft(bridge, script)
    assert source.queries == ["the place with the big wheel", "Testford, Testshire"]


def test_assessment_with_starter_map(tmp_path):
    store = RubricStore(tmp_path / "rubrics")
    store.save("river", "# Cross the River\n\n## Task\n\nBuild a bridge.\n\n## Starter map\n\nTestford size 40 scale 2 without bridges\n")

    class Teacher:
        async def complete(self, messages):
            return '{"summary": "Good.", "criteria": [], "strengths": [], "next_steps": []}'

    bridge = ChatBridge(Teacher(), BridgeConfig(), rubrics=store, reports_dir=tmp_path / "reports", map_source=FakeMap())

    async def script(mc):
        await mc.chat("Steve", "!challenge")
        await mc.wait_for("1. Cross the River")
        await mc.chat("Steve", "1")
        i = await mc.wait_for("Your task: Build a bridge.", timeout=20)
        await mc.chat("Steve", "finished")
        await mc.wait_for("Would you like to try again?", i)
        await mc.chat("Steve", "no")

    mc = run_with_minecraft(bridge, script)
    probes = [c for c in mc.commands if c.startswith("testforblock ")]
    assert len(probes) == 22 * 10 * 22, "the middle 22x22 of the map, 10 high"
    tps = [c for c in mc.commands if c.startswith("tp ")]
    assert len(tps) == 2 and tps[0] == tps[1], "teleported before and after building"
    assert (10 + 10, -61, 20 - 11) in mc.world and mc.world[(10 + 10, -61, 20 - 11)] == "water", "bridge left out"


def test_demo_map_rubric_parses():
    from pathlib import Path

    from mcchat.assessment import rubric_section

    rubric = RubricStore(Path(__file__).resolve().parent.parent / "rubrics").get("bridge_the_avon")
    request = parse_map_args(" ".join(rubric_section(rubric.text, "Starter map").split()))
    assert request == MapRequest("Clifton Suspension Bridge, Bristol", 96, 5.0, bridges=False)


def test_map_downloads_are_cached(tmp_path):
    import os
    import time

    from mcchat.builder import BuildError
    from mcchat.realworld import OpenStreetMap, overpass_query

    osm = OpenStreetMap(urls=(), cache_dir=tmp_path)  # no servers: only the cache can answer
    bbox = (51.0, -0.2, 51.1, -0.1)
    with pytest.raises(BuildError, match="too busy"):
        asyncio.run(osm.features(*bbox))
    osm._store(overpass_query(*bbox), ELEMENTS)
    assert asyncio.run(osm.features(*bbox)) == ELEMENTS
    (cached,) = tmp_path.glob("*.json")
    month_ago = time.time() - 31 * 24 * 3600
    os.utime(cached, (month_ago, month_ago))
    with pytest.raises(BuildError):
        asyncio.run(osm.features(*bbox))


# --- 3D buildings and landmarks -----------------------------------------------------------

from mcchat.realworld import Building, building_base, inset_depth, roof_layers  # noqa: E402


def test_building_base_and_parts_tags():
    assert building_base({"min_height": "57.63"}, 2.0) == 29
    assert building_base({"building:min_level": "2"}, 3.0) == 2
    assert building_base({}, 2.0) == 0
    assert building_materials({"building:part": "yes", "building:material": "steel"}) == ("iron_bars", "iron_block")


def test_inset_depth_and_roof_layers():
    square = {(x, z) for x in range(7) for z in range(7)}
    depth = inset_depth(square)
    assert depth[(0, 0)] == 1 and depth[(3, 3)] == 4
    pyramid = roof_layers(square, "pyramidal", 4)
    assert [len(layer) for layer in pyramid] == [49, 25, 9, 1], "each layer steps in to a point"
    dome = roof_layers(square, "dome", 4)
    assert len(dome[0]) == 49 and len(dome[1]) == 49 and len(dome[-1]) < len(dome[1]), "domes rise steeply then curve in"
    long_box = {(x, z) for x in range(5) for z in range(12)}
    gable = roof_layers(long_box, "gabled", 3)
    assert {z for _, z in gable[-1]} == set(range(12)), "a gabled ridge runs the full length"
    assert {x for x, _ in gable[-1]} == {2}


def tower_element(osm_id, tags, square_points):
    return {**way(tags, square_points, closed=True), "id": osm_id}


TOWER_ELEMENTS = [
    # The outline of a tower whose shape is mapped as 3D parts (like the Eiffel Tower).
    tower_element(1, {"building": "tower", "building:part": "no", "height": "100", "wikidata": "Q1", "name": "Big Tower"},
                  square(-6, -6, 6, 6)),
    tower_element(2, {"building:part": "yes", "height": "40"}, square(-6, -6, 6, 6)),
    tower_element(3, {"building:part": "yes", "min_height": "40", "height": "44"}, square(-4, -4, 4, 4)),
    tower_element(4, {"building:part": "yes", "min_height": "44", "height": "80", "roof:shape": "pyramidal"}, square(-2, -2, 2, 2)),
    # A famous building with only an outline: a candidate for an AI model.
    tower_element(5, {"building": "cathedral", "height": "30", "wikidata": "Q2", "name": "Old Cathedral", "historic": "yes"},
                  square(8, 8, 18, 18)),
    tower_element(6, {"building": "house"}, square(-18, 10, -12, 16)),
]


def test_3d_parts_replace_the_outline():
    layout = lay_out_map(TOWER_ELEMENTS, PROJECT, 40)
    parts = [b for b in layout.buildings if b.part]
    assert len(parts) == 3 and layout.counts["outlines replaced by 3D parts"] == 1
    assert not any(b.name == "Big Tower" for b in layout.buildings)
    assert sorted((p.base, p.height) for p in parts) == [(0, 20), (20, 22), (22, 40)]
    spire = max(parts, key=lambda p: p.base)
    assert spire.roof_shape == "pyramidal" and spire.roof_height >= 1
    _, ops = map_ops(layout, centre=(0, 0), ground=0)
    platform = [op for op in ops if op.y1 == 21 and op.y2 == 21]
    assert platform, "a part that starts above the ground gets a solid underside"
    assert max(op.y2 for op in ops) == 40, "the spire's tip is the top of the tallest part"


def test_landmark_choice_prefers_the_requested_place():
    layout = lay_out_map(TOWER_ELEMENTS, PROJECT, 40, target="way/5")
    chosen = [b for b in layout.buildings if b.landmark]
    assert [b.name for b in chosen] == ["Old Cathedral"]
    assert layout.counts["AI landmarks"] == 1
    plain = lay_out_map(TOWER_ELEMENTS, PROJECT, 40)
    assert [b.name for b in plain.buildings if b.landmark] == ["Old Cathedral"], "famous: wikidata + historic"


class LandmarkLLM:
    def __init__(self):
        self.requests = []

    async def complete(self, messages):
        self.requests.append(messages)
        return """<code>function buildCreation(x, y, z) {
  safeFill(x, y, z, x + 9, y + 14, z + 9, "stone", { mode: "hollow" });
  safeFill(x + 4, y + 15, z + 4, x + 5, y + 20, z + 5, "stone");
}</code>"""


def test_ai_models_landmarks_at_their_footprint():
    from mcchat.progress import Progress
    from mcchat.realworld import model_landmarks

    layout = lay_out_map(TOWER_ELEMENTS, PROJECT, 40, target="way/5")
    llm = LandmarkLLM()
    asyncio.run(model_landmarks(llm, layout, 2.0, Progress.silent()))
    (cathedral,) = [b for b in layout.buildings if b.landmark]
    assert cathedral.model and layout.counts["AI landmarks"] == 1
    system, request = llm.requests[0][0]["content"], llm.requests[0][1]["content"]
    assert "Old Cathedral" in request and "15 blocks tall" in request
    assert "fit within 10 blocks along X and Z and 15 blocks tall" in system
    xs = [c[0] for c in cathedral.cells]
    model_x = (min(op.x1 for op in cathedral.model) + max(op.x2 for op in cathedral.model)) // 2
    assert model_x == (min(xs) + max(xs)) // 2, "centred on the real footprint"
    _, ops = map_ops(layout, centre=(100, 0), ground=10)
    assert any(op.block == "stone" and op.y2 == 10 + 20 for op in ops), "the model is placed in world coordinates"
