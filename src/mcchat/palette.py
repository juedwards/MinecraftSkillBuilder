"""The blocks builds may use, with fallbacks, and colour palettes for choosing facade materials.

Bedrock has renamed many blocks over recent versions ("stonebrick" became "stone_bricks", slabs
and walls were split into one block per material, ...), and Minecraft Education can lag behind.
So newer blocks carry fallbacks: if Minecraft rejects a block name, the server retries the command
with the next fallback and remembers the result for that world (see MinecraftConnection).
Restricting builds to this list also guarantees nothing but a plain identifier reaches a command.
"""

from __future__ import annotations

COLOURS = (
    "white orange magenta light_blue yellow lime pink gray "
    "light_gray cyan purple blue brown green red black"
).split()

# Proven in Minecraft Education (used in live builds without errors).
PROVEN = frozenset(
    """
    air stone granite polished_granite diorite polished_diorite andesite polished_andesite
    cobblestone mossy_cobblestone smooth_stone brick_block sandstone red_sandstone
    deepslate cobbled_deepslate polished_deepslate deepslate_bricks deepslate_tiles
    blackstone polished_blackstone polished_blackstone_bricks tuff calcite mud_bricks packed_mud
    dirt grass_block podzol moss_block sand gravel clay snow ice packed_ice blue_ice water
    oak_planks spruce_planks birch_planks jungle_planks acacia_planks dark_oak_planks
    mangrove_planks cherry_planks bamboo_planks crimson_planks warped_planks
    oak_log spruce_log birch_log jungle_log acacia_log dark_oak_log mangrove_log cherry_log
    stripped_oak_log stripped_spruce_log stripped_birch_log stripped_dark_oak_log
    oak_leaves spruce_leaves birch_leaves jungle_leaves cherry_leaves azalea_leaves
    oak_stairs spruce_stairs birch_stairs dark_oak_stairs stone_brick_stairs brick_stairs
    sandstone_stairs quartz_stairs oak_fence spruce_fence dark_oak_fence cobblestone_wall
    glass glass_pane iron_bars ladder vine
    quartz_block quartz_bricks purpur_block prismarine end_bricks nether_brick red_nether_brick
    netherrack obsidian bone_block honeycomb_block
    iron_block gold_block diamond_block emerald_block lapis_block redstone_block coal_block
    copper_block amethyst_block
    glowstone sea_lantern lantern torch bookshelf crafting_table hay_block
    pumpkin carved_pumpkin melon_block cactus
    """.split()
    + [f"{c}_{kind}" for c in COLOURS for kind in ("wool", "concrete", "terracotta", "stained_glass")]
)

# Newer or renamed blocks, each with fallbacks tried in order if Minecraft rejects it.
FALLBACKS: dict[str, tuple[str, ...]] = {
    # Masonry
    "stone_bricks": ("stonebrick", "polished_andesite"),
    "mossy_stone_bricks": ("stone_bricks", "stonebrick", "mossy_cobblestone"),
    "cracked_stone_bricks": ("stone_bricks", "stonebrick", "polished_andesite"),
    "chiseled_stone_bricks": ("stone_bricks", "stonebrick", "polished_andesite"),
    "cut_sandstone": ("sandstone",),
    "smooth_sandstone": ("sandstone",),
    "chiseled_sandstone": ("sandstone",),
    "cut_red_sandstone": ("red_sandstone",),
    "smooth_red_sandstone": ("red_sandstone",),
    "smooth_quartz": ("quartz_block",),
    "chiseled_quartz_block": ("quartz_block",),
    "quartz_pillar": ("quartz_block",),
    "terracotta": ("hardened_clay", "brown_terracotta"),
    "prismarine_bricks": ("prismarine",),
    "dark_prismarine": ("prismarine",),
    "tuff_bricks": ("tuff",),
    "polished_tuff": ("tuff",),
    "chiseled_tuff_bricks": ("tuff",),
    "cracked_deepslate_bricks": ("deepslate_bricks",),
    "chiseled_deepslate": ("deepslate_bricks",),
    "smooth_basalt": ("polished_deepslate",),
    "polished_basalt": ("polished_deepslate",),
    "end_stone": ("end_bricks",),
    # Copper ages from orange to green
    "cut_copper": ("copper_block",),
    "exposed_copper": ("copper_block",),
    "weathered_copper": ("copper_block",),
    "oxidized_copper": ("weathered_copper", "copper_block"),
    "oxidized_cut_copper": ("oxidized_copper", "copper_block"),
    # Slabs (half blocks), handy for roof edges and floors
    "stone_brick_slab": ("smooth_stone",),
    "smooth_stone_slab": ("smooth_stone",),
    "brick_slab": ("brick_block",),
    "sandstone_slab": ("sandstone",),
    "quartz_slab": ("quartz_block",),
    "cobblestone_slab": ("cobblestone",),
    "deepslate_tile_slab": ("deepslate_tiles",),
    "oak_slab": ("oak_planks",),
    "spruce_slab": ("spruce_planks",),
    "dark_oak_slab": ("dark_oak_planks",),
    # More stairs, fences and walls
    "stone_stairs": ("stone_brick_stairs",),
    "jungle_stairs": ("oak_stairs",),
    "acacia_stairs": ("oak_stairs",),
    "red_sandstone_stairs": ("sandstone_stairs",),
    "mud_brick_stairs": ("brick_stairs",),
    "deepslate_brick_stairs": ("stone_brick_stairs",),
    "deepslate_tile_stairs": ("stone_brick_stairs",),
    "birch_fence": ("oak_fence",),
    "jungle_fence": ("oak_fence",),
    "acacia_fence": ("oak_fence",),
    "stone_brick_wall": ("cobblestone_wall",),
    "brick_wall": ("cobblestone_wall",),
    # Windows and trim
    **{f"{c}_stained_glass_pane": ("glass_pane",) for c in ("white", "light_gray", "gray", "light_blue", "blue", "black")},
    "tinted_glass": ("gray_stained_glass", "glass"),
}

BEDROCK_BLOCKS = PROVEN.union(FALLBACKS)


def fallbacks(block: str) -> tuple[str, ...]:
    return FALLBACKS.get(block, ())


# Approximate average colours of facade blocks, for matching a building's colour tag to the
# nearest real material (brick, stone, sandstone...) rather than always a flat concrete.
FACADE_RGB: dict[str, tuple[int, int, int]] = {
    "brick_block": (150, 97, 83),
    "stone_bricks": (122, 121, 122),
    "polished_andesite": (132, 134, 133),
    "smooth_stone": (159, 159, 159),
    "calcite": (223, 224, 220),
    "quartz_block": (236, 230, 223),
    "smooth_sandstone": (216, 203, 155),
    "cut_sandstone": (218, 206, 160),
    "red_sandstone": (186, 99, 29),
    "mud_bricks": (137, 103, 79),
    "deepslate_bricks": (70, 70, 71),
    "polished_blackstone_bricks": (48, 43, 50),
    "tuff_bricks": (98, 103, 95),
    "terracotta": (152, 94, 68),
    "white_terracotta": (210, 178, 161),
    "light_gray_terracotta": (135, 107, 98),
    "gray_terracotta": (58, 42, 36),
    "orange_terracotta": (162, 84, 38),
    "yellow_terracotta": (186, 133, 35),
    "red_terracotta": (143, 61, 47),
    "brown_terracotta": (77, 51, 36),
    "pink_terracotta": (162, 78, 79),
    "light_blue_terracotta": (113, 109, 138),
    "cyan_terracotta": (87, 91, 91),
    "green_terracotta": (76, 83, 42),
    "white_concrete": (207, 213, 214),
    "light_gray_concrete": (125, 125, 115),
    "gray_concrete": (54, 57, 61),
    "blue_concrete": (44, 46, 143),
    "light_blue_concrete": (35, 137, 198),
    "green_concrete": (73, 91, 36),
    "yellow_concrete": (241, 175, 21),
    "pink_concrete": (213, 101, 142),
    "purple_concrete": (100, 31, 156),
    "black_concrete": (8, 10, 15),
}

ROOF_RGB: dict[str, tuple[int, int, int]] = {
    "red_terracotta": (143, 61, 47),
    "brown_terracotta": (77, 51, 36),
    "terracotta": (152, 94, 68),
    "orange_terracotta": (162, 84, 38),
    "deepslate_tiles": (55, 55, 56),
    "dark_prismarine": (51, 91, 75),
    "oxidized_copper": (82, 162, 132),
    "weathered_copper": (109, 145, 107),
    "copper_block": (192, 107, 79),
    "dark_oak_planks": (67, 43, 20),
    "spruce_planks": (115, 85, 49),
    "smooth_stone": (159, 159, 159),
    "light_gray_concrete": (125, 125, 115),
    "gray_concrete": (54, 57, 61),
    "black_concrete": (8, 10, 15),
    "white_concrete": (207, 213, 214),
    "blue_concrete": (44, 46, 143),
    "green_concrete": (73, 91, 36),
    "gold_block": (246, 208, 61),
}


def to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB -> CIELAB (D65), where equal distances look about equally different to people."""
    def linear(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def nearest(rgb: tuple[int, int, int], palette: dict[str, tuple[int, int, int]]) -> str:
    """The palette block whose colour looks closest (CIELAB distance)."""
    target = to_lab(rgb)
    return min(palette, key=lambda name: sum((a - b) ** 2 for a, b in zip(target, to_lab(palette[name]))))
