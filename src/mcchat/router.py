"""Decides what a `!build` request means: a real place (built from OpenStreetMap) or a design.

Players only need one command: `!build the tower of london` builds the real place from map data,
while `!build a castle` (or `!build a castle like the tower of london`) is designed by the AI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .llm import ChatModel
from .realworld import DEFAULT_SCALE, DEFAULT_SIZE, MAX_SCALE, MAX_SIZE, MIN_SCALE, MIN_SIZE, MapRequest

ROUTER_PROMPT = f"""\
You route build requests from students playing Minecraft Education. Decide which kind each request is:

- "place": the student wants a specific REAL place built from map data: a named landmark, building,
  street, square, park, bridge, campus or part of a town, e.g. "the tower of london",
  "times square", "big ben", "my school, Hillside Primary in Leeds", "the street with the Eiffel Tower".
- "design": anything else, designed from imagination: generic or imaginary things, objects, vehicles,
  creatures, and things "like", "inspired by" or "in the style of" a place, e.g. "a castle",
  "a rocket", "a japanese house", "a castle like the tower of london", "a model of a skyscraper".

Reply with only a JSON object, no other text:
- for a place: {{"kind": "place", "query": "a search query with the place, town and country",
  "size": null, "scale": null, "bridges": true}}
  Set "size" (blocks per side, {MIN_SIZE}-{MAX_SIZE}, default {DEFAULT_SIZE}) only if they ask for a bigger or smaller
  area, and "scale" (metres per block, {MIN_SCALE:g}-{MAX_SCALE:g}, default {DEFAULT_SCALE:g}) only if they ask for more
  detail (smaller) or a wider area (bigger). Set "bridges" to false only if they ask for no bridges.
- for a design: {{"kind": "design", "request": "what to build, in a few words"}}"""


@dataclass(frozen=True)
class BuildRoute:
    kind: str  # "place" or "design"
    request: str  # what to design (for "design")
    map_request: MapRequest | None = None  # what to map (for "place")


def parse_route(reply: str, request: str) -> BuildRoute:
    """Read the router's answer; anything unreadable means "design the request as asked"."""
    try:
        data = json.loads(reply[reply.index("{"): reply.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return BuildRoute("design", request)
    if data.get("kind") == "place" and str(data.get("query") or "").strip():

        def number(key: str, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(data[key]))) if data.get(key) is not None else default
            except (TypeError, ValueError):
                return default

        return BuildRoute("place", request, MapRequest(
            query=str(data["query"]).strip()[:120],
            size=int(number("size", DEFAULT_SIZE, MIN_SIZE, MAX_SIZE)),
            scale=number("scale", DEFAULT_SCALE, MIN_SCALE, MAX_SCALE),
            bridges=data.get("bridges") is not False,
        ))
    return BuildRoute("design", str(data.get("request") or "").strip()[:200] or request)


async def route_build(llm: ChatModel, request: str) -> BuildRoute:
    try:
        reply = await llm.complete([
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user", "content": request},
        ])
    except Exception:
        return BuildRoute("design", request)
    return parse_route(reply, request)
