import pytest

from mcchat.builder import (
    MAX_FILL_VOLUME, BuildError, BuildOp, PlayerPosition, build_prompt, extract_code, facing_from_yaw,
    place_ops, run_build_script, to_commands,
)


def script(body: str) -> str:
    return f"function buildCreation(x, y, z) {{ {body} }}"


def test_run_build_script_collects_ops():
    ops = run_build_script(script('safeFill(3, 0, 0, 0, 2, 1, "minecraft:stone", {mode: "hollow"}); safeSetBlock(1, 1, 1, "glass");'))
    assert ops == [BuildOp(0, 0, 0, 3, 2, 1, "stone", "hollow"), BuildOp(1, 1, 1, 1, 1, 1, "glass")]


def test_run_build_script_skips_unknown_blocks_and_states():
    ops = run_build_script(script('safeSetBlock(0,0,0,"unobtainium"); safeSetBlock(0,1,0,"oak_stairs[facing=north]"); safeSetBlock(0,2,0,"stone; kill @a");'))
    assert [op.block for op in ops] == ["oak_stairs"]


def test_run_build_script_tolerates_async():
    code = "async function buildCreation(x, y, z) { await safeSetBlock(0, 0, 0, 'stone'); }"
    assert len(run_build_script(code)) == 1


def test_run_build_script_stops_infinite_loops():
    with pytest.raises(BuildError, match="failed"):
        run_build_script(script("while (true) {}"))


def test_run_build_script_rejects_huge_builds():
    with pytest.raises(BuildError, match="too big"):
        run_build_script(script('safeFill(0, 0, 0, 100, 1, 1, "stone");'))


def test_run_build_script_limits_operation_count():
    with pytest.raises(BuildError, match="too many blocks"):
        run_build_script(script('for (var i = 0; i < 100000; i++) safeSetBlock(0, 0, 0, "stone");'))


def test_extract_code():
    assert extract_code("x <code>\nfunction buildCreation(){}\n</code> y") == "function buildCreation(){}"
    assert "buildCreation" in extract_code("```javascript\nfunction buildCreation(a,b,c) {}\n```")
    with pytest.raises(BuildError):
        extract_code("no code here")


def test_build_prompt_lists_blocks():
    prompt = build_prompt()
    assert "oak_planks" in prompt and "%BLOCKS%" not in prompt


@pytest.mark.parametrize("yaw, facing", [(0, (0, 1)), (90, (-1, 0)), (180, (0, -1)), (-180, (0, -1)), (-90, (1, 0)), (30, (0, 1)), (60, (-1, 0))])
def test_facing_from_yaw(yaw, facing):
    assert facing_from_yaw(yaw) == facing


def test_place_ops_faces_player():
    # A 3-wide, 2-deep slab; its front (z=0) must end up nearest the player.
    ops = [BuildOp(0, 0, 0, 2, 0, 1, "stone")]
    feet_eye_y = -60 + 1.62
    east = place_ops(ops, PlayerPosition(0.5, feet_eye_y, 0.5, -90))  # looking +X
    assert east == [BuildOp(2, -61, -1, 3, -61, 1, "stone")]
    north = place_ops(ops, PlayerPosition(0.5, feet_eye_y, 0.5, 180))  # looking -Z
    assert north == [BuildOp(-1, -61, -3, 1, -61, -2, "stone")]


def test_to_commands_setblock_and_fill_modes():
    ops = [
        BuildOp(1, 2, 3, 1, 2, 3, "stone"),
        BuildOp(1, 2, 3, 1, 2, 3, "stone", "keep"),
        BuildOp(0, 0, 0, 2, 2, 2, "glass", "outline"),
        BuildOp(0, 0, 0, 2, 2, 2, "glass", "replace", "dirt"),
    ]
    assert to_commands(ops) == [
        "setblock 1 2 3 stone",
        "setblock 1 2 3 stone keep",
        "fill 0 0 0 2 2 2 glass outline",
        "fill 0 0 0 2 2 2 glass replace dirt",
    ]


def test_to_commands_splits_large_fills():
    big = BuildOp(0, 0, 0, 47, 47, 47, "stone")
    commands = to_commands([big])
    pieces = [list(map(int, c.split()[1:7])) for c in commands]
    volumes = [(x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1) for x1, y1, z1, x2, y2, z2 in pieces]
    assert all(v <= MAX_FILL_VOLUME for v in volumes) and sum(volumes) == big.volume


def test_to_commands_splits_large_hollow_into_walls_and_air():
    commands = to_commands([BuildOp(0, 0, 0, 40, 40, 40, "stone", "hollow")])
    assert any(c.endswith(" air") for c in commands)
    assert not any("hollow" in c for c in commands)


def test_falling_and_flowing_blocks_get_supports():
    ops = run_build_script(script('safeFill(0, 0, 0, 4, 0, 4, "grass_block"); safeFill(1, -2, 1, 3, -2, 3, "sand"); safeFill(1, -1, 1, 3, 0, 3, "water");'))
    assert ops == [
        BuildOp(0, 0, 0, 4, 0, 4, "grass_block"),
        BuildOp(1, -3, 1, 3, -3, 3, "stone", "keep"),
        BuildOp(1, -2, 1, 3, -2, 3, "sand"),
        BuildOp(1, -2, 1, 3, -2, 3, "stone", "keep"),  # sand is already there, so "keep" leaves it
        BuildOp(1, -1, 1, 3, 0, 3, "water"),
    ]
    assert to_commands(ops)[1] == "fill 1 -3 1 3 -3 3 stone keep"
