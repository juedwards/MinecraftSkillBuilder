"""Minecraft Skill Builder: the web interface (activity, players & history, rubrics, settings)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .assessment import RubricError
from .runtime import Runtime, SettingsError

STATIC_DIR = Path(__file__).parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
KEEPALIVE_SECONDS = 15

RUNTIME = web.AppKey("runtime", Runtime)


@web.middleware
async def local_only(request: web.Request, handler):
    """Reject requests not addressed to localhost (DNS rebinding) or sent by other sites (CSRF)."""
    host = urlsplit(f"//{request.host}").hostname or ""
    origin = request.headers.get("Origin")
    if host not in LOCAL_HOSTS or (origin and urlsplit(origin).hostname not in LOCAL_HOSTS):
        return web.json_response({"error": "Forbidden"}, status=403)
    return await handler(request)


def create_app(runtime: Runtime) -> web.Application:
    app = web.Application(middlewares=[local_only])
    app[RUNTIME] = runtime
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", post_settings)
    app.router.add_get("/api/players", players)
    app.router.add_get("/api/players/{name}/history", player_history)
    app.router.add_delete("/api/players/{name}/history", reset_player)
    app.router.add_get("/api/rubrics", list_rubrics)
    app.router.add_get("/api/rubrics/{id}", get_rubric)
    app.router.add_put("/api/rubrics/{id}", put_rubric)
    app.router.add_delete("/api/rubrics/{id}", delete_rubric)
    return app


async def start_web(runtime: Runtime, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(create_app(runtime), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[RUNTIME].status())


async def events(request: web.Request) -> web.StreamResponse:
    """Server-sent events: recent history first, then live events."""
    runtime = request.app[RUNTIME]
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await response.prepare(request)
    with runtime.events.subscribe() as queue:
        try:
            for event in runtime.events.recent():
                await response.write(_sse(event))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    await response.write(_sse(event))
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except ConnectionResetError:
            pass  # browser tab closed
    return response


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


async def get_settings(request: web.Request) -> web.Response:
    return web.json_response(request.app[RUNTIME].settings_view())


async def post_settings(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Expected a JSON body."}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "Expected a JSON object."}, status=400)
    try:
        return web.json_response(await request.app[RUNTIME].update_settings(data))
    except SettingsError as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def players(request: web.Request) -> web.Response:
    return web.json_response(await request.app[RUNTIME].players())


async def player_history(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    return web.json_response({"name": name, "history": request.app[RUNTIME].bridge.history(name)})


async def reset_player(request: web.Request) -> web.Response:
    request.app[RUNTIME].reset_player(request.match_info["name"])
    return web.json_response({"ok": True})


async def list_rubrics(request: web.Request) -> web.Response:
    rubrics = request.app[RUNTIME].rubrics.list()
    return web.json_response([{"id": r.id, "title": r.title} for r in rubrics])


async def get_rubric(request: web.Request) -> web.Response:
    rubric = request.app[RUNTIME].rubrics.get(request.match_info["id"])
    if rubric is None:
        return web.json_response({"error": "Rubric not found."}, status=404)
    return web.json_response({"id": rubric.id, "title": rubric.title, "text": rubric.text})


async def put_rubric(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Expected a JSON body."}, status=400)
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return web.json_response({"error": "Expected {\"text\": \"...\"}."}, status=400)
    runtime = request.app[RUNTIME]
    try:
        rubric = runtime.rubrics.save(request.match_info["id"], data["text"])
    except RubricError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    runtime.events.publish({"type": "settings", "status": f"rubric saved: {rubric.title}"})
    return web.json_response({"id": rubric.id, "title": rubric.title, "text": rubric.text})


async def delete_rubric(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME]
    try:
        deleted = runtime.rubrics.delete(request.match_info["id"])
    except RubricError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not deleted:
        return web.json_response({"error": "Rubric not found."}, status=404)
    runtime.events.publish({"type": "settings", "status": f"rubric deleted: {request.match_info['id']}"})
    return web.json_response({"ok": True})
