"""Minecraft Quest Builder: the web interface (activity, players & history, quests, costs, settings).

Locally it only answers on localhost. Hosted (Settings.hosted), it also accepts Minecraft
connections at /mc/<join code>, and every other page requires the platform's sign-in
(Azure App Service authentication with Microsoft Entra ID), failing closed if that's off.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .assessment import RubricError
from .minecraft import MinecraftConnection
from .runtime import Runtime, SettingsError

STATIC_DIR = Path(__file__).parent / "static"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
KEEPALIVE_SECONDS = 15

RUNTIME = web.AppKey("runtime", Runtime)


PRINCIPAL_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"  # set by App Service authentication after sign-in


def platform_sign_in_enabled() -> bool:
    """App Service sets this when its authentication (Easy Auth) is turned on for the app."""
    return os.environ.get("WEBSITE_AUTH_ENABLED", "").strip().lower() == "true"


@web.middleware
async def guard(request: web.Request, handler):
    """Who may use the teacher pages.

    Locally: only requests addressed to localhost (no DNS rebinding) and not sent by other sites (CSRF).
    Hosted: only signed-in users, via the platform's sign-in, which also strips forged identity headers;
    if the platform's sign-in isn't on, nothing is served (fail closed). Minecraft's /mc endpoint is
    protected by its join code instead.
    """
    if request.path == "/mc" or request.path.startswith("/mc/"):
        return await handler(request)
    origin = request.headers.get("Origin")
    if request.app[RUNTIME].settings.hosted:
        if not platform_sign_in_enabled():
            return web.json_response({"error": "Sign-in isn't set up for this app, so the teacher pages are off."}, status=503)
        if not request.headers.get(PRINCIPAL_HEADER):
            return web.json_response({"error": "Sign in required."}, status=401)
        if origin and urlsplit(origin).netloc != request.host:
            return web.json_response({"error": "Forbidden"}, status=403)
        return await handler(request)
    host = urlsplit(f"//{request.host}").hostname or ""
    if host not in LOCAL_HOSTS or (origin and urlsplit(origin).hostname not in LOCAL_HOSTS):
        return web.json_response({"error": "Forbidden"}, status=403)
    return await handler(request)


def create_app(runtime: Runtime) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[RUNTIME] = runtime
    app.router.add_get("/mc", minecraft_socket)
    app.router.add_get("/mc/{code}", minecraft_socket)
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_post("/api/settings", post_settings)
    app.router.add_get("/api/players", players)
    app.router.add_get("/api/players/{name}/history", player_history)
    app.router.add_delete("/api/players/{name}/history", reset_player)
    app.router.add_get("/api/usage", usage)
    app.router.add_delete("/api/usage", clear_usage)
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


def connect_command(request: web.Request) -> str:
    """The /connect command students type in Minecraft to reach this server."""
    settings = request.app[RUNTIME].settings
    if not settings.hosted:
        return f"/connect localhost:{request.app[RUNTIME].server.port}"
    # Minecraft Education connects with plain ws:// (in testing it didn't accept wss://), so the
    # hosting must allow unencrypted HTTP for /mc; the join code is what keeps strangers out.
    host = urlsplit(settings.public_url).netloc if settings.public_url else request.host
    return f"/connect ws://{host}/mc/{settings.join_code}"


async def status(request: web.Request) -> web.Response:
    body = request.app[RUNTIME].status()
    body["connect"] = connect_command(request)
    body["user"] = request.headers.get(PRINCIPAL_HEADER, "")
    return web.json_response(body)


async def minecraft_socket(request: web.Request) -> web.StreamResponse:
    """Minecraft connecting through the web server (hosted): /connect wss://<host>/mc/<join code>."""
    runtime = request.app[RUNTIME]
    expected = runtime.settings.join_code
    code = request.match_info.get("code", "")
    if (runtime.settings.hosted or expected) and not (expected and hmac.compare_digest(code, expected)):
        return web.Response(status=403, text="Wrong or missing join code.")
    ws = web.WebSocketResponse(heartbeat=None, max_msg_size=16 * 1024 * 1024)
    if not ws.can_prepare(request).ok:
        return web.Response(text=f"This is the Minecraft address. In Minecraft, type: {connect_command(request)}")
    await ws.prepare(request)
    remote = request.headers.get("X-Forwarded-For", request.remote or "unknown").split(",")[0].strip()

    async def frames() -> AsyncIterator[str | bytes]:
        async for message in ws:
            if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                yield message.data
            elif message.type == WSMsgType.ERROR:
                break

    async def send(text: str) -> None:
        await ws.send_str(text)

    try:
        await runtime.server.serve_connection(MinecraftConnection(send, remote), frames())
    except ConnectionResetError:
        pass
    return ws


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
        return web.json_response({"error": "Quest not found."}, status=404)
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
    runtime.events.publish({"type": "settings", "status": f"quest saved: {rubric.title}"})
    return web.json_response({"id": rubric.id, "title": rubric.title, "text": rubric.text})


async def delete_rubric(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME]
    try:
        deleted = runtime.rubrics.delete(request.match_info["id"])
    except RubricError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not deleted:
        return web.json_response({"error": "Quest not found."}, status=404)
    runtime.events.publish({"type": "settings", "status": f"quest deleted: {request.match_info['id']}"})
    return web.json_response({"ok": True})


async def usage(request: web.Request) -> web.Response:
    """AI usage and costs. ?days=1|7|30 limits the period (omit for all time)."""
    try:
        days = int(request.query["days"]) if request.query.get("days") else None
    except ValueError:
        return web.json_response({"error": "days must be a number."}, status=400)
    if days is not None and not 1 <= days <= 3660:
        return web.json_response({"error": "days must be between 1 and 3660."}, status=400)
    return web.json_response(request.app[RUNTIME].usage_summary(days))


async def clear_usage(request: web.Request) -> web.Response:
    runtime = request.app[RUNTIME]
    runtime.usage.clear()
    runtime.events.publish({"type": "settings", "status": "AI usage history cleared"})
    return web.json_response({"ok": True})
