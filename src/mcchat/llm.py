"""LLM clients. AzureFoundryLLM talks to Azure AI Foundry's OpenAI-compatible v1 API."""

from __future__ import annotations

from typing import Callable, Protocol
from urllib.parse import urlparse

from openai import AsyncOpenAI

Message = dict[str, str]


class ChatModel(Protocol):
    async def complete(self, messages: list[Message]) -> str: ...


def normalize_endpoint(url: str) -> str:
    """Turn any Foundry/Azure OpenAI endpoint form into the `/openai/v1/` base URL.

    Non-Azure OpenAI-compatible servers (e.g. a local vLLM) are used as given,
    with `/v1` added if the URL has no path.
    """
    url = url.strip().rstrip("/")
    host = urlparse(url).hostname or ""
    if not host.endswith(".azure.com"):
        return url + ("/" if urlparse(url).path else "/v1/")
    if "/api/projects/" in url:
        url = url.split("/api/projects/")[0]
    for suffix in ("/openai/v1", "/openai", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url + "/openai/v1/"


UsageCallback = Callable[[str, int, int], None]  # (model, input tokens, output tokens)


class AzureFoundryLLM:
    def __init__(self, endpoint: str, api_key: str, model: str, on_usage: UsageCallback | None = None):
        self.model = model
        self._on_usage = on_usage
        # Local servers like vLLM accept any key, but the SDK requires a non-empty one.
        self._client = AsyncOpenAI(base_url=normalize_endpoint(endpoint), api_key=api_key or "none")

    async def complete(self, messages: list[Message]) -> str:
        response = await self._client.chat.completions.create(model=self.model, messages=messages)
        usage = getattr(response, "usage", None)
        if usage is not None and self._on_usage is not None:
            self._on_usage(self.model, usage.prompt_tokens or 0, usage.completion_tokens or 0)
        return (response.choices[0].message.content or "").strip()


class EchoLLM:
    """Offline stand-in for testing the Minecraft side without Azure."""

    async def complete(self, messages: list[Message]) -> str:
        return f"You said: {messages[-1]['content']}"
