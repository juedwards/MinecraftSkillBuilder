"""Settings loaded from environment variables (and a .env file in the working directory)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv, set_key

from .bridge import DEFAULT_SYSTEM_PROMPT
from .setup_wizard import AzureCredentials

ENV_FILE = Path(".env")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value else default
    except ValueError:
        return default


@dataclass
class Settings:
    azure_endpoint: str = ""
    azure_api_key: str = ""
    azure_model: str = ""
    host: str = "0.0.0.0"
    port: int = 3000
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    trigger: str = ""
    reply_private: bool = False
    max_history: int = 20
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Prices per million tokens, for the Costs page (defaults: GPT-5 list prices; set your Azure rates).
    price_input_per_million: float = 1.25
    price_output_per_million: float = 10.0
    currency: str = "$"
    # Hosting (e.g. Azure App Service): one public address, Minecraft connects to /mc/<join code>,
    # and the teacher pages require the platform's sign-in.
    hosted: bool = False
    join_code: str = ""
    public_url: str = ""

    @classmethod
    def from_env(cls, path: Path = ENV_FILE) -> "Settings":
        # When hosted, the platform's app settings are only starting values: changes saved from the
        # Settings page (in .env) must win after a restart.
        load_dotenv(path, override=_bool(os.environ.get("HOSTED")))
        env = os.environ.get
        # AZURE_OPENAI_* names (as shown in the Azure portal) are accepted as fallbacks.
        return cls(
            azure_endpoint=env("AZURE_AI_ENDPOINT") or env("AZURE_OPENAI_ENDPOINT", ""),
            azure_api_key=env("AZURE_AI_API_KEY") or env("AZURE_OPENAI_API_KEY", ""),
            azure_model=env("AZURE_AI_MODEL") or env("AZURE_OPENAI_DEPLOYMENT_NAME", ""),
            host=env("MC_HOST") or "0.0.0.0",
            port=int(env("MC_PORT") or 3000),
            web_host=env("WEB_HOST") or "127.0.0.1",
            web_port=int(env("WEB_PORT") or 8080),
            trigger=env("MC_TRIGGER", ""),
            reply_private=_bool(env("MC_REPLY_PRIVATE")),
            max_history=int(env("MAX_HISTORY") or 20),
            system_prompt=env("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT,
            price_input_per_million=_float(env("PRICE_INPUT_PER_M"), 1.25),
            price_output_per_million=_float(env("PRICE_OUTPUT_PER_M"), 10.0),
            currency=env("CURRENCY") or "$",
            hosted=_bool(env("HOSTED")),
            join_code=(env("JOIN_CODE") or "").strip(),
            public_url=(env("PUBLIC_URL") or "").strip().rstrip("/"),
        )

    def missing_azure_settings(self) -> list[str]:
        required = {
            "AZURE_AI_ENDPOINT": self.azure_endpoint,
            "AZURE_AI_MODEL": self.azure_model,
        }
        # Azure needs a key; local OpenAI-compatible servers (vLLM) usually don't.
        if ".azure.com" in self.azure_endpoint:
            required["AZURE_AI_API_KEY"] = self.azure_api_key
        return [name for name, value in required.items() if not value]


def save_env_values(values: dict[str, str], path: Path = ENV_FILE) -> None:
    """Write values into the .env file, keeping any other settings in it."""
    path.touch(exist_ok=True)
    for key, value in values.items():
        needs_quotes = value == "" or any(c in value for c in " #'\"\n\\")
        set_key(path, key, value, quote_mode="always" if needs_quotes else "never")


def save_azure_credentials(credentials: AzureCredentials, path: Path = ENV_FILE) -> None:
    save_env_values(
        {
            "AZURE_AI_ENDPOINT": credentials.endpoint,
            "AZURE_AI_API_KEY": credentials.api_key,
            "AZURE_AI_MODEL": credentials.model,
        },
        path,
    )
