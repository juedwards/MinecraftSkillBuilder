"""Wires Minecraft, the chat bridge and the LLM together. Shared by the CLI and the web UI."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .assessment import RubricStore
from .bridge import DEFAULT_SYSTEM_PROMPT, BridgeConfig, ChatBridge, Event, LLMFactory, short_error
from .config import ENV_FILE, Settings, save_azure_credentials, save_env_values
from .llm import AzureFoundryLLM, ChatModel, EchoLLM, UsageCallback
from .minecraft import MinecraftConnection, MinecraftServer
from .setup_wizard import AzureCredentials
from .usage import SETUP, Prices, UsageLedger, usage_context

MAX_SYSTEM_PROMPT = 4000
MAX_TRIGGER = 20


def azure_llm(credentials: AzureCredentials, on_usage: UsageCallback | None = None) -> ChatModel:
    return AzureFoundryLLM(credentials.endpoint, credentials.api_key, credentials.model, on_usage=on_usage)


def build_llm(settings: Settings, mock: bool, on_usage: UsageCallback | None = None) -> ChatModel | None:
    """The configured LLM, or None if credentials are missing."""
    if mock:
        return EchoLLM()
    if settings.missing_azure_settings():
        return None
    return azure_llm(AzureCredentials(settings.azure_endpoint, settings.azure_api_key, settings.azure_model), on_usage)


class EventLog:
    """Recent events plus live fan-out to listeners (CLI) and queues (web clients)."""

    def __init__(self, maxlen: int = 500):
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._next_id = 1
        self._listeners: list[Callable[[Event], None]] = []
        self._queues: set[asyncio.Queue[Event]] = set()

    def publish(self, event: Event) -> None:
        event = {"id": self._next_id, "time": datetime.now().isoformat(timespec="seconds"), **event}
        self._next_id += 1
        self._events.append(event)
        for listener in self._listeners:
            listener(event)
        for queue in self._queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stalled client misses events rather than blocking everyone

    def recent(self) -> list[Event]:
        return list(self._events)

    def add_listener(self, listener: Callable[[Event], None]) -> None:
        self._listeners.append(listener)

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)


class SettingsError(ValueError):
    """Invalid settings, or credentials that failed their connection test."""


class Runtime:
    def __init__(
        self,
        settings: Settings,
        llm: ChatModel | None,
        llm_factory: LLMFactory | None = None,
        env_path: Path = ENV_FILE,
        mock: bool = False,
        rubrics_dir: Path = Path("rubrics"),
        reports_dir: Path = Path("assessments"),
        usage: UsageLedger | None = None,
    ):
        self.settings = settings
        self.mock = mock
        self.usage = usage or UsageLedger(None)
        self._llm_factory = llm_factory or (lambda credentials: azure_llm(credentials, self.usage.record))
        self._env_path = env_path
        self.events = EventLog()
        self.rubrics = RubricStore(rubrics_dir)
        self.connections: list[MinecraftConnection] = []
        self.bridge = ChatBridge(
            llm,
            BridgeConfig(
                system_prompt=settings.system_prompt,
                trigger=settings.trigger,
                max_history=settings.max_history,
                reply_private=settings.reply_private,
            ),
            on_event=self.events.publish,
            llm_factory=self._llm_factory,
            save_credentials=self._save_credentials,
            rubrics=self.rubrics,
            reports_dir=reports_dir,
        )
        self.server = MinecraftServer(
            settings.host, settings.port, self.bridge.handle_chat, self._on_connect, self._on_disconnect,
            on_game_event=self.bridge.handle_game_event,
        )

    async def start(self) -> None:
        await self.server.start()

    @property
    def model_label(self) -> str:
        if self.bridge.llm is None:
            return ""
        return "mock echo" if self.mock else self.settings.azure_model

    async def _on_connect(self, conn: MinecraftConnection) -> None:
        self.connections.append(conn)
        self.events.publish({"type": "connected", "remote": conn.remote})
        if self.bridge.llm is None:
            await conn.send_chat("AI chat connected, but no AI is set up yet. Type !setup to connect to Azure.", prefix="§e[AI]§r ")
        else:
            await conn.send_chat("AI chat connected! Type !help for help.", prefix="§a[AI]§r ")

    def _on_disconnect(self, conn: MinecraftConnection) -> None:
        if conn in self.connections:
            self.connections.remove(conn)
        self.events.publish({"type": "disconnected", "remote": conn.remote})

    def _save_credentials(self, credentials: AzureCredentials) -> None:
        save_azure_credentials(credentials, self._env_path)
        self.settings.azure_endpoint = credentials.endpoint
        self.settings.azure_api_key = credentials.api_key
        self.settings.azure_model = credentials.model
        self.mock = False

    # --- Views for the web UI ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "minecraft": {"connections": [c.remote for c in self.connections], "port": self.server.port},
            "ai": {"configured": self.bridge.llm is not None, "model": self.model_label},
            "building": self.bridge.building,
            "assessments": self.bridge.assessments.overview() if self.bridge.assessments else [],
        }

    def settings_view(self) -> dict[str, Any]:
        config = self.bridge.config
        return {
            "azure_endpoint": self.settings.azure_endpoint,
            "azure_model": self.settings.azure_model,
            "api_key_set": bool(self.settings.azure_api_key),
            "trigger": config.trigger,
            "reply_private": config.reply_private,
            "max_history": config.max_history,
            "system_prompt": config.system_prompt,
            "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
        }

    async def players(self) -> list[dict[str, Any]]:
        online: set[str] = set()
        if self.connections:
            try:
                online = set(await asyncio.wait_for(self.connections[0].list_players(), 3))
            except Exception:
                pass
        names = sorted(online | set(self.bridge.players()), key=str.lower)
        return [
            {"name": name, "online": name in online, "messages": len(self.bridge.history(name))}
            for name in names
        ]

    # --- Settings updates ---------------------------------------------------------------

    async def update_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate, test (for credential changes), apply and save settings. Raises SettingsError."""
        config = self.bridge.config
        env: dict[str, str] = {}
        updates: dict[str, Any] = {}

        if "trigger" in data:
            trigger = str(data["trigger"]).strip()
            if len(trigger) > MAX_TRIGGER or " " in trigger:
                raise SettingsError(f"The trigger must be one word of at most {MAX_TRIGGER} characters.")
            updates["trigger"], env["MC_TRIGGER"] = trigger, trigger
        if "reply_private" in data:
            if not isinstance(data["reply_private"], bool):
                raise SettingsError("reply_private must be true or false.")
            updates["reply_private"] = data["reply_private"]
            env["MC_REPLY_PRIVATE"] = "true" if data["reply_private"] else "false"
        if "max_history" in data:
            try:
                max_history = int(data["max_history"])
            except (TypeError, ValueError):
                raise SettingsError("History length must be a number.") from None
            if not 0 <= max_history <= 200:
                raise SettingsError("History length must be between 0 and 200.")
            updates["max_history"], env["MAX_HISTORY"] = max_history, str(max_history)
        for key, env_key in (("price_input_per_million", "PRICE_INPUT_PER_M"), ("price_output_per_million", "PRICE_OUTPUT_PER_M")):
            if key in data:
                try:
                    price = float(data[key])
                except (TypeError, ValueError):
                    raise SettingsError("Prices must be numbers.") from None
                if not 0 <= price <= 1000:
                    raise SettingsError("Prices must be between 0 and 1000 per million tokens.")
                updates[key], env[env_key] = price, f"{price:g}"
        if "currency" in data:
            currency = str(data["currency"]).strip()
            if not 1 <= len(currency) <= 3:
                raise SettingsError("The currency symbol must be 1 to 3 characters, e.g. $ or £.")
            updates["currency"], env["CURRENCY"] = currency, currency
        if "system_prompt" in data:
            prompt = str(data["system_prompt"]).strip() or DEFAULT_SYSTEM_PROMPT
            if len(prompt) > MAX_SYSTEM_PROMPT:
                raise SettingsError(f"The system prompt can be at most {MAX_SYSTEM_PROMPT} characters.")
            updates["system_prompt"], env["SYSTEM_PROMPT"] = prompt, prompt

        new_llm = None
        credentials = AzureCredentials(
            endpoint=str(data.get("azure_endpoint") or self.settings.azure_endpoint).strip(),
            api_key=str(data.get("api_key") or self.settings.azure_api_key).strip(),
            model=str(data.get("azure_model") or self.settings.azure_model).strip(),
        )
        current = AzureCredentials(self.settings.azure_endpoint, self.settings.azure_api_key, self.settings.azure_model)
        if credentials != current:
            if not credentials.endpoint.startswith(("https://", "http://")):
                raise SettingsError("The endpoint must start with https://")
            if not credentials.model:
                raise SettingsError("Enter the model deployment name.")
            try:
                new_llm = self._llm_factory(credentials)
                with usage_context("(teacher)", SETUP):
                    await new_llm.complete([{"role": "user", "content": "Reply with the single word OK."}])
            except Exception as exc:
                raise SettingsError(f"Couldn't connect with these credentials: {short_error(exc, 200)}") from exc

        # Everything is valid: apply and save.
        for key, value in updates.items():
            setattr(config, key, value)
            setattr(self.settings, key, value)
        if env:
            save_env_values(env, self._env_path)
        changed = list(updates)
        if new_llm is not None:
            self._save_credentials(credentials)
            self.bridge.llm = new_llm
            changed.append("AI connection")
        if changed:
            self.events.publish({"type": "settings", "status": "updated " + ", ".join(changed)})
        return self.settings_view()

    @classmethod
    def create(cls, settings: Settings, mock: bool = False) -> Runtime:
        """The runtime the CLI uses: usage is recorded to usage.jsonl."""
        usage = UsageLedger()
        return cls(settings, build_llm(settings, mock, usage.record), mock=mock, usage=usage)

    def prices(self) -> Prices:
        return Prices(self.settings.price_input_per_million, self.settings.price_output_per_million, self.settings.currency)

    def usage_summary(self, days: int | None = None) -> dict[str, Any]:
        summary = self.usage.summary(self.prices(), days)
        summary["model"] = self.model_label
        return summary

    def reset_player(self, player: str) -> None:
        self.bridge.reset(player)
