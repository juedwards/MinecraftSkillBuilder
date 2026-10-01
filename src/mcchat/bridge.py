"""Connects Minecraft chat to an LLM: per-player history, triggers and chat commands.

UI-agnostic: the CLI (and later a web UI) observes activity through `on_event`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .assessment import AssessmentManager, RubricStore
from .builder import BuildError, build_for_player
from .llm import ChatModel, Message
from .minecraft import ChatMessage, GameEvent, MinecraftConnection, quote_target
from .setup_wizard import AzureCredentials, SetupSession
from .village import build_village, design_village, parse_village_args

DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, helpful assistant inside Minecraft Education, talking with "
    "students through the in-game chat. Keep answers short (1-3 sentences unless asked "
    "for more), use plain text with no Markdown, and keep everything school-appropriate."
)

Event = dict[str, Any]
LLMFactory = Callable[[AzureCredentials], ChatModel]
CredentialSaver = Callable[[AzureCredentials], None]


@dataclass
class BridgeConfig:
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    trigger: str = ""
    max_history: int = 20
    reply_private: bool = False
    bot_name: str = "AI"


def strip_markdown(text: str) -> str:
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text


class ChatBridge:
    def __init__(
        self,
        llm: ChatModel | None,
        config: BridgeConfig | None = None,
        on_event: Callable[[Event], None] | None = None,
        llm_factory: LLMFactory | None = None,
        save_credentials: CredentialSaver | None = None,
        rubrics: RubricStore | None = None,
        reports_dir: Path = Path("assessments"),
    ):
        """`llm` may be None until credentials are provided via `!setup` (needs `llm_factory`).

        `!assess` is available when `rubrics` is given.
        """
        self.llm = llm
        self.config = config or BridgeConfig()
        self._on_event = on_event or (lambda event: None)
        self._llm_factory = llm_factory
        self._save_credentials = save_credentials
        self._history: dict[str, list[Message]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._setup: SetupSession | None = None
        self._building: str | None = None  # player whose build is in progress
        self.assessments = AssessmentManager(self, rubrics, reports_dir) if rubrics else None

    def emit(self, type_: str, **data: Any) -> None:
        self._on_event({"type": type_, **data})

    @property
    def building(self) -> str | None:
        """The player whose build is in progress, if any."""
        return self._building

    def begin_build(self, player: str) -> str | None:
        """Claim the (single) build slot. Returns the player already building, or None if claimed."""
        if self._building:
            return self._building
        self._building = player
        return None

    def end_build(self) -> None:
        self._building = None

    def handle_game_event(self, conn: MinecraftConnection, event: GameEvent) -> None:
        if self.assessments:
            self.assessments.record_game_event(event)

    def players(self) -> list[str]:
        """Players with a conversation history."""
        return sorted(self._history)

    def history(self, player: str) -> list[Message]:
        return list(self._history.get(player, []))

    def reset(self, player: str) -> None:
        if self._history.pop(player, None) is not None:
            self.emit("reset", player=player)

    async def handle_chat(self, conn: MinecraftConnection, msg: ChatMessage) -> None:
        # Messages from one player are handled strictly in order.
        async with self._locks.setdefault(msg.sender, asyncio.Lock()):
            await self._handle(conn, msg)

    async def _handle(self, conn: MinecraftConnection, msg: ChatMessage) -> None:
        text = msg.message.strip()
        command = text.lower()
        setup = self._setup
        if setup and setup.player == msg.sender and not setup.expired() and command != "!setup":
            await self._continue_setup(conn, setup, text)
            return
        if command == "!setup":
            await self._start_setup(conn, msg.sender)
            return
        if self.assessments and await self.assessments.handle_chat(conn, msg.sender, text):
            return
        if command == "!reset":
            self.reset(msg.sender)
            await self.reply(conn, msg.sender, "Conversation cleared.")
            return
        if command == "!help":
            await self.reply(conn, msg.sender, self.help_text(), private=True)
            return
        if command == "!build" or command.startswith("!build "):
            await self._build(conn, msg.sender, text[len("!build"):].strip())
            return
        if command == "!village" or command.startswith("!village "):
            await self._village(conn, msg.sender, text[len("!village"):].strip())
            return

        trigger = self.config.trigger
        if trigger:
            if not command.startswith(trigger.lower()):
                return
            text = text[len(trigger):].strip()
        if not text:
            return
        if self.llm is None:
            await self.reply(conn, msg.sender, "I'm not connected to an AI yet. Type !setup to connect me to Azure AI Foundry.", error=True)
            return
        await self._answer(conn, msg.sender, text)

    def help_text(self) -> str:
        trigger = self.config.trigger
        chat = f'Start a message with {trigger} to ask me anything' if trigger else "Just type in chat to ask me anything"
        lines = [
            "Commands:",
            f"- Chat: {chat}. I remember our conversation.",
            "- !build <thing>: I design and build it in front of you, e.g. !build a lighthouse",
            "- !village [style] [number]: I build a village of about 20 buildings around you, with villagers, "
            "e.g. !village viking or !village japanese 12. It clears a big area around you!",
        ]
        if self.assessments:
            lines.append(
                "- !assess: take a building assessment. Choose a task, build it, then type finished "
                "to get feedback and tips. !cancel stops it."
            )
        lines += [
            "- !reset: forget our conversation and start fresh.",
            "- !setup: connect me to Azure AI Foundry (only when I'm not set up yet).",
            "- !help: show this list.",
            "Building commands need cheats turned on in the world.",
        ]
        return "\n".join(lines)

    async def _village(self, conn: MinecraftConnection, player: str, args: str) -> None:
        if self.llm is None:
            await self.reply(conn, player, "I'm not connected to an AI yet. Type !setup to connect me to Azure AI Foundry.", error=True)
            return
        busy = self.begin_build(player)
        if busy:
            await self.reply(conn, player, f"I'm busy building for {busy}. Try again in a moment.")
            return

        style, count = parse_village_args(args)

        async def say(text: str) -> None:
            await self.reply(conn, player, text)

        def status(text: str) -> None:
            self.emit("build", player=player, status=f"village: {text}")

        try:
            await say(f"Planning a {style + ' ' if style else ''}village of {count} buildings around you. This takes a few minutes...")
            village = await design_village(self.llm, conn, player, style, count, say=say, on_status=status)
            failed, villagers = await build_village(conn, player, village, say, on_status=status)
        except Exception as exc:
            status(f"failed: {exc}")
            message = str(exc) if isinstance(exc, BuildError) else short_error(exc)
            await self.reply(conn, player, f"Village failed: {message}", error=True)
            return
        finally:
            self.end_build()

        summary = f"Welcome to {village.plan.name}! {len(village.built)} buildings and {villagers} villagers."
        if village.failed:
            summary += f" {len(village.failed)} buildings couldn't be designed."
        if failed:
            summary += f" {failed} parts didn't place."
        status(f"done: {len(village.built)} buildings, {villagers} villagers, {len(village.failed)} not designed, {failed} commands failed")
        await self.reply(conn, player, summary)

    async def _build(self, conn: MinecraftConnection, player: str, request: str) -> None:
        if not request:
            await self.reply(conn, player, "Tell me what to build, e.g. !build a small oak cabin with a red roof")
            return
        if self.llm is None:
            await self.reply(conn, player, "I'm not connected to an AI yet. Type !setup to connect me to Azure AI Foundry.", error=True)
            return
        busy = self.begin_build(player)
        if busy:
            await self.reply(conn, player, f"I'm busy building for {busy}. Try again in a moment.")
            return

        try:
            self.emit("build", player=player, status=f"requested: {request}")
            await self.reply(conn, player, f"Designing {request}... this can take a minute.")
            result = await build_for_player(
                self.llm, conn, player, request,
                on_status=lambda status: self.emit("build", player=player, status=status),
            )
        except BuildError as exc:
            self.emit("build", player=player, status=f"failed: {exc}")
            await self.reply(conn, player, f"Build failed: {exc}", error=True)
            return
        except Exception as exc:
            self.emit("build", player=player, status=f"failed: {exc}")
            await self.reply(conn, player, f"Build failed: {short_error(exc)}", error=True)
            return
        finally:
            self.end_build()

        width, height, depth = result.size
        summary = f"Built {request} ({width}x{height}x{depth}, {result.commands} commands)."
        self.emit("build", player=player, status=f"done: {result.commands} commands, {result.failed} failed {result.first_error}".rstrip())
        if result.failed:
            summary += f" {result.failed} parts didn't place: {short_error(result.first_error, 100)}"
        await self.reply(conn, player, summary, error=result.failed == result.commands)

    async def _start_setup(self, conn: MinecraftConnection, player: str) -> None:
        if self.llm is not None:
            await self.reply(conn, player, "I'm already connected. To change credentials, edit the .env file on the server and restart it.", private=True)
            return
        if self._llm_factory is None:
            await self.reply(conn, player, "Setup from chat isn't available on this server.", error=True, private=True)
            return
        if self._setup and self._setup.player != player and not self._setup.expired():
            await self.reply(conn, player, f"{self._setup.player} is already running setup.", private=True)
            return
        self._setup = SetupSession(player)
        self.emit("setup", player=player, status="started")
        await self.reply(
            conn,
            player,
            "Setup started. Warning: everyone in this world can see what you type in chat, "
            "so only do this in a private world. Type !cancel to stop.\n" + self._setup.prompt,
            private=True,
        )

    async def _continue_setup(self, conn: MinecraftConnection, setup: SetupSession, text: str) -> None:
        player = setup.player
        if text.lower() == "!cancel":
            self._setup = None
            self.emit("setup", player=player, status="cancelled")
            await self.reply(conn, player, "Setup cancelled.", private=True)
            return

        field = setup.field
        error = setup.answer(text)
        if error:
            await self.reply(conn, player, f"{error}\n{setup.prompt}", error=True, private=True)
            return
        self.emit("setup", player=player, status=f"{field} received")
        if not setup.done:
            await self.reply(conn, player, setup.prompt, private=True)
            return

        # The session stays active during the test so no one else can start setup meanwhile.
        credentials = setup.credentials()
        await self.reply(conn, player, "Testing the connection...", private=True)
        assert self._llm_factory is not None
        try:
            llm = self._llm_factory(credentials)
            await llm.complete([{"role": "user", "content": "Reply with the single word OK."}])
        except Exception as exc:
            self._setup = None
            self.emit("setup", player=player, status=f"connection test failed: {exc}")
            await self.reply(conn, player, f"Couldn't connect: {short_error(exc)}\nType !setup to try again.", error=True, private=True)
            return

        self._setup = None
        self.llm = llm
        saved = ""
        if self._save_credentials is not None:
            try:
                self._save_credentials(credentials)
                saved = " Saved for next time."
            except Exception as exc:
                self.emit("setup", player=player, status=f"could not save credentials: {exc}")
        self.emit("setup", player=player, status=f"connected to {credentials.model}")
        await self.reply(conn, player, f"Connected to {credentials.model}!{saved} Ask me anything.", private=True)

    async def _answer(self, conn: MinecraftConnection, player: str, text: str) -> None:
        assert self.llm is not None
        self.emit("question", player=player, text=text)
        history = self._history.setdefault(player, [])
        messages: list[Message] = [
            {"role": "system", "content": f"{self.config.system_prompt}\nYou are talking with the player named {player}."},
            *history,
            {"role": "user", "content": text},
        ]
        try:
            answer = strip_markdown(await self.llm.complete(messages)) or "(no response)"
        except Exception as exc:
            self.emit("error", player=player, error=str(exc))
            await self.reply(conn, player, "Sorry, I couldn't reach the AI right now.", error=True)
            return

        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
        if self.config.max_history > 0:
            del history[: max(0, len(history) - self.config.max_history)]
        else:
            history.clear()

        self.emit("answer", player=player, text=answer)
        await self.reply(conn, player, answer)

    async def reply(
        self, conn: MinecraftConnection, player: str, text: str, error: bool = False, private: bool | None = None
    ) -> None:
        if private is None:
            private = self.config.reply_private
        target = quote_target(player) if private else "@a"
        colour = "§c" if error else "§b"
        await conn.send_chat(text, target=target, prefix=f"{colour}[{self.config.bot_name}]§r ")


def short_error(error: object, limit: int = 150) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."
