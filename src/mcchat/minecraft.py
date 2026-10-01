"""WebSocket server speaking the Minecraft Bedrock / Education Edition protocol.

In game, a player runs `/connect <host>:<port>`. Minecraft then opens a WebSocket
to this server. We subscribe to `PlayerMessage` events to read chat, and send
`commandRequest` messages (e.g. `tellraw`) to write back into chat.

The protocol code doesn't depend on the WebSocket library: `MinecraftServer` listens on its
own port (local use), and `serve_connection` also runs connections accepted by the web server
(hosted use, where Minecraft connects to `/mc/<join code>` on the one public address).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import textwrap
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterable, Awaitable, Callable

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# Minecraft rejects very long command lines, so replies are split into chunks.
MAX_CHAT_CHUNK = 220
COMMAND_TIMEOUT = 10.0


@dataclass(frozen=True)
class ChatMessage:
    sender: str
    message: str
    type: str


@dataclass(frozen=True)
class GameEvent:
    """Any subscribed event other than chat, e.g. BlockPlaced / BlockBroken."""

    name: str
    body: dict[str, Any]

    @property
    def player(self) -> str:
        player = self.body.get("player")
        return str(player.get("name", "")) if isinstance(player, dict) else ""


# Block events recorded during assessments.
GAME_EVENTS = ("BlockPlaced", "BlockBroken")


def build_subscribe(event_name: str, request_id: str | None = None) -> dict[str, Any]:
    return {
        "header": {
            "version": 1,
            "requestId": request_id or str(uuid.uuid4()),
            "messageType": "commandRequest",
            "messagePurpose": "subscribe",
        },
        "body": {"eventName": event_name},
    }


def build_command(command_line: str, request_id: str | None = None) -> dict[str, Any]:
    return {
        "header": {
            "version": 1,
            "requestId": request_id or str(uuid.uuid4()),
            "messageType": "commandRequest",
            "messagePurpose": "commandRequest",
        },
        "body": {
            "version": 1,
            "commandLine": command_line,
            "origin": {"type": "player"},
        },
    }


def parse_player_message(data: dict[str, Any]) -> ChatMessage | None:
    """Extract a chat message from a PlayerMessage event, or None if it isn't one."""
    header = data.get("header", {})
    if header.get("messagePurpose") != "event":
        return None
    body = data.get("body", {})
    event_name = header.get("eventName") or body.get("eventName")
    if event_name != "PlayerMessage":
        return None
    # Older clients nest fields under "properties" with capitalised keys.
    props = body.get("properties", body)
    message = props.get("message", props.get("Message"))
    sender = props.get("sender", props.get("Sender", ""))
    msg_type = props.get("type", props.get("MessageType", "chat"))
    if message is None:
        return None
    return ChatMessage(sender=str(sender), message=str(message), type=str(msg_type))


def parse_game_event(data: dict[str, Any]) -> GameEvent | None:
    header = data.get("header", {})
    if header.get("messagePurpose") != "event":
        return None
    body = data.get("body", {})
    name = header.get("eventName") or body.get("eventName")
    if not name or name == "PlayerMessage":
        return None
    return GameEvent(str(name), body.get("properties", body) if isinstance(body, dict) else {})


_BLOCK_IS = re.compile(r"\bis (.+?)(?: \(expected.*)?\.?\s*$", re.IGNORECASE | re.DOTALL)


def parse_testforblock(response: dict[str, Any]) -> str:
    """Block name from a `testforblock x y z air` response: "air" on success, else the block Minecraft reports."""
    if response.get("statusCode", 0) >= 0:
        return "air"
    match = _BLOCK_IS.search(str(response.get("statusMessage", "")))
    if not match:
        return "unknown"
    name = match.group(1).strip().lower().removeprefix("minecraft:")
    return re.sub(r"[^a-z0-9]+", "_", name).strip("_") or "unknown"


def quote_target(name: str) -> str:
    """Quote a player name for use as a command target selector."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def tellraw_command(text: str, target: str = "@a") -> str:
    payload = json.dumps({"rawtext": [{"text": text}]}, ensure_ascii=False)
    return f"tellraw {target} {payload}"


def split_for_chat(text: str, width: int = MAX_CHAT_CHUNK) -> list[str]:
    """Split text into chat-sized chunks, keeping paragraph breaks."""
    chunks: list[str] = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if paragraph:
            chunks.extend(textwrap.wrap(paragraph, width=width, break_long_words=True))
    return chunks


class MinecraftConnection:
    """One connected Minecraft client (one world)."""

    def __init__(self, send: Callable[[str], Awaitable[None]], remote: str = "unknown"):
        self._send = send
        self._remote = remote
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @classmethod
    def from_websocket(cls, ws: ServerConnection) -> MinecraftConnection:
        addr = ws.remote_address
        return cls(ws.send, f"{addr[0]}:{addr[1]}" if addr else "unknown")

    @property
    def remote(self) -> str:
        return self._remote

    async def subscribe(self, event_name: str) -> None:
        await self._send(json.dumps(build_subscribe(event_name)))

    async def run_command(self, command_line: str, timeout: float = COMMAND_TIMEOUT) -> dict[str, Any]:
        """Run a slash command (without the leading slash) and return Minecraft's response body."""
        request_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(json.dumps(build_command(command_line, request_id)))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def run_commands(
        self, commands: list[str], concurrency: int = 16, on_done: Callable[[], None] | None = None,
    ) -> list[str]:
        """Run many commands, a few in flight at a time. Returns the error messages of those that failed.

        `on_done` is called after each command, for progress reporting.
        """
        semaphore = asyncio.Semaphore(concurrency)
        errors: list[str] = []

        async def run(command: str) -> None:
            async with semaphore:
                try:
                    response = await self.run_command(command)
                except asyncio.TimeoutError:
                    errors.append(f"timed out: {command}")
                    return
                finally:
                    if on_done is not None:
                        on_done()
                if response.get("statusCode", 0) < 0:
                    errors.append(str(response.get("statusMessage", "command failed")))

        await asyncio.gather(*(run(command) for command in commands))
        return errors

    async def query_player(self, name: str) -> dict[str, Any]:
        """Position and rotation of a player via /querytarget: {"position": {x, y, z}, "yRot": ...}."""
        response = await self.run_command(f"querytarget {quote_target(name)}")
        details = response.get("details")
        if isinstance(details, str):
            details = json.loads(details)
        if not details:
            raise LookupError(response.get("statusMessage") or f"player {name} not found")
        return details[0]

    async def list_players(self) -> list[str]:
        """Names of online players via /list."""
        response = await self.run_command("list")
        players = response.get("players")
        if players is None:
            # Fall back to the message: "There are 1/10 players online:\nSteve, Alex"
            players = str(response.get("statusMessage", "")).partition(":")[2]
        if isinstance(players, list):
            return [str(p) for p in players]
        return [name.strip() for name in str(players).replace("\n", ",").split(",") if name.strip()]

    async def blocks_at(
        self, positions: list[tuple[int, int, int]], concurrency: int = 16, on_done: Callable[[], None] | None = None,
    ) -> list[str]:
        """Block names at many positions (via /testforblock), in the same order."""
        semaphore = asyncio.Semaphore(concurrency)

        async def probe(x: int, y: int, z: int) -> str:
            async with semaphore:
                try:
                    return parse_testforblock(await self.run_command(f"testforblock {x} {y} {z} air"))
                except asyncio.TimeoutError:
                    return "unknown"
                finally:
                    if on_done is not None:
                        on_done()

        return list(await asyncio.gather(*(probe(*pos) for pos in positions)))

    async def send_chat(self, text: str, target: str = "@a", prefix: str = "") -> None:
        for chunk in split_for_chat(text):
            response = await self.run_command(tellraw_command(prefix + chunk, target))
            if response.get("statusCode", 0) < 0:
                log.warning("tellraw failed: %s", response.get("statusMessage"))

    def handle_raw(self, raw: str | bytes) -> ChatMessage | GameEvent | None:
        """Process an incoming frame: resolve command responses, return chat messages and other events."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("Ignoring non-JSON frame: %r", raw)
            return None
        header = data.get("header", {})
        purpose = header.get("messagePurpose")
        if purpose in ("commandResponse", "error"):
            future = self._pending.get(header.get("requestId", ""))
            if future and not future.done():
                future.set_result(data.get("body", {}))
            return None
        return parse_player_message(data) or parse_game_event(data)

    def fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Minecraft disconnected"))
        self._pending.clear()


ChatHandler = Callable[[MinecraftConnection, ChatMessage], Awaitable[None]]
ConnectionHandler = Callable[[MinecraftConnection], Awaitable[None] | None]
GameEventHandler = Callable[[MinecraftConnection, GameEvent], None]


class MinecraftServer:
    def __init__(
        self,
        host: str,
        port: int,
        on_chat: ChatHandler,
        on_connect: ConnectionHandler | None = None,
        on_disconnect: ConnectionHandler | None = None,
        on_game_event: GameEventHandler | None = None,
    ):
        self.host = host
        self.port = port
        self._on_chat = on_chat
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_game_event = on_game_event
        self._server: Server | None = None

    async def start(self) -> None:
        # Minecraft doesn't reliably answer WebSocket pings, so keepalive pings are disabled.
        self._server = await serve(self._handle, self.host, self.port, ping_interval=None)
        self.port = self._server.sockets[0].getsockname()[1]

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws: ServerConnection) -> None:
        await self.serve_connection(MinecraftConnection.from_websocket(ws), ws)

    async def serve_connection(self, conn: MinecraftConnection, frames: AsyncIterable[str | bytes]) -> None:
        """Run one Minecraft connection until it closes: subscribe, then dispatch its frames."""
        tasks: set[asyncio.Task[None]] = set()

        # Callbacks run in their own tasks so this loop keeps receiving
        # the command responses they wait on.
        def spawn(coro: Awaitable[None]) -> None:
            task = asyncio.create_task(coro)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        try:
            await conn.subscribe("PlayerMessage")
            if self._on_game_event is not None:
                for event_name in GAME_EVENTS:
                    await conn.subscribe(event_name)
            spawn(self._run_callback(_maybe_await(self._on_connect, conn), "Connect handler"))
            async for raw in frames:
                message = conn.handle_raw(raw)
                if isinstance(message, GameEvent):
                    if self._on_game_event is not None:
                        try:
                            self._on_game_event(conn, message)
                        except Exception:
                            log.exception("Game event handler failed")
                    continue
                # Ignore our own tellraw/say output and other non-player chat.
                if message is None or message.type != "chat":
                    continue
                spawn(self._run_callback(self._on_chat(conn, message), "Chat handler"))
        except ConnectionClosed:
            pass
        finally:
            for task in tasks:
                task.cancel()
            conn.fail_pending()
            await _maybe_await(self._on_disconnect, conn)

    @staticmethod
    async def _run_callback(coro: Awaitable[None], name: str) -> None:
        try:
            await coro
        except Exception:
            log.exception("%s failed", name)


async def _maybe_await(callback: ConnectionHandler | None, conn: MinecraftConnection) -> None:
    if callback is None:
        return
    result = callback(conn)
    if asyncio.iscoroutine(result):
        await result
