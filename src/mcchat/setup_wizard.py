"""State for the in-chat `!setup` flow that collects Azure credentials step by step."""

from __future__ import annotations

import time
from dataclasses import dataclass

SETUP_TIMEOUT = 300.0  # seconds of inactivity before another player may start setup


@dataclass(frozen=True)
class AzureCredentials:
    endpoint: str
    api_key: str
    model: str


STEPS = [
    ("endpoint", "Step 1/3: Paste your Azure AI Foundry endpoint, e.g. https://my-resource.openai.azure.com/"),
    ("api_key", "Step 2/3: Paste your API key."),
    ("model", "Step 3/3: Type your model deployment name, e.g. gpt-4o-mini"),
]


class SetupSession:
    def __init__(self, player: str):
        self.player = player
        self._values: dict[str, str] = {}
        self._step = 0
        self._last_activity = time.monotonic()

    @property
    def prompt(self) -> str:
        return STEPS[self._step][1]

    @property
    def field(self) -> str:
        return STEPS[self._step][0]

    @property
    def done(self) -> bool:
        return self._step >= len(STEPS)

    def expired(self) -> bool:
        return time.monotonic() - self._last_activity > SETUP_TIMEOUT

    def answer(self, text: str) -> str | None:
        """Record the answer to the current step. Returns an error message if it's invalid."""
        self._last_activity = time.monotonic()
        text = text.strip()
        if not text:
            return "That was empty."
        if self.field == "endpoint" and not text.startswith(("https://", "http://")):
            return "That doesn't look like an endpoint URL. It should start with https://"
        self._values[self.field] = text
        self._step += 1
        return None

    def credentials(self) -> AzureCredentials:
        return AzureCredentials(**self._values)
