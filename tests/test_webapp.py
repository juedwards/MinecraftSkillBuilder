import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from mcchat.config import Settings
from mcchat.llm import EchoLLM
from mcchat.runtime import Runtime
from mcchat.webapp import create_app


class FailingLLM:
    async def complete(self, messages):
        raise RuntimeError("401 invalid key")


def make_runtime(tmp_path, factory=lambda creds: EchoLLM()) -> Runtime:
    settings = Settings(azure_endpoint="https://old.openai.azure.com/", azure_api_key="old-secret", azure_model="old-model")
    return Runtime(settings, EchoLLM(), llm_factory=factory, env_path=tmp_path / ".env",
                   rubrics_dir=tmp_path / "rubrics", reports_dir=tmp_path / "reports")


def run(runtime: Runtime, scenario):
    async def main():
        async with TestClient(TestServer(create_app(runtime))) as client:
            return await scenario(client)

    return asyncio.run(main())


def test_status_and_settings_never_expose_key(tmp_path):
    async def scenario(client):
        status = await (await client.get("/api/status")).json()
        settings = await (await client.get("/api/settings")).json()
        return status, settings

    status, settings = run(make_runtime(tmp_path), scenario)
    assert status["ai"] == {"configured": True, "model": "old-model"}
    assert status["minecraft"]["connections"] == []
    assert settings["api_key_set"] is True
    assert "old-secret" not in json.dumps(settings)


def test_update_chat_settings_applies_and_saves(tmp_path):
    runtime = make_runtime(tmp_path)

    async def scenario(client):
        resp = await client.post("/api/settings", json={
            "trigger": "!ai", "reply_private": True, "max_history": 6, "system_prompt": "Talk like a pirate.",
        })
        return resp.status

    assert run(runtime, scenario) == 200
    config = runtime.bridge.config
    assert (config.trigger, config.reply_private, config.max_history, config.system_prompt) == ("!ai", True, 6, "Talk like a pirate.")
    env = (tmp_path / ".env").read_text()
    assert "MC_TRIGGER=!ai" in env and "MAX_HISTORY=6" in env and "SYSTEM_PROMPT='Talk like a pirate.'" in env
    assert runtime.events.recent()[-1]["type"] == "settings"


def test_invalid_settings_rejected(tmp_path):
    runtime = make_runtime(tmp_path)

    async def scenario(client):
        resp = await client.post("/api/settings", json={"max_history": 999, "trigger": "!ai"})
        return resp.status, await resp.json()

    status, body = run(runtime, scenario)
    assert status == 400 and "between 0 and 200" in body["error"]
    assert runtime.bridge.config.trigger == "", "nothing applied when any field is invalid"


def test_credentials_are_tested_before_saving(tmp_path):
    runtime = make_runtime(tmp_path, factory=lambda creds: FailingLLM())
    old_llm = runtime.bridge.llm

    async def scenario(client):
        resp = await client.post("/api/settings", json={"azure_model": "new-model", "api_key": "new-secret"})
        return resp.status, await resp.json()

    status, body = run(runtime, scenario)
    assert status == 400 and "Couldn't connect" in body["error"] and "401 invalid key" in body["error"]
    assert runtime.bridge.llm is old_llm and runtime.settings.azure_model == "old-model"
    assert not (tmp_path / ".env").exists()


def test_credentials_switch_when_test_passes(tmp_path):
    runtime = make_runtime(tmp_path)
    old_llm = runtime.bridge.llm

    async def scenario(client):
        resp = await client.post("/api/settings", json={"azure_model": "new-model", "api_key": "new-secret", "azure_endpoint": ""})
        return resp.status, await resp.json()

    status, body = run(runtime, scenario)
    assert status == 200 and body["azure_model"] == "new-model" and "new-secret" not in json.dumps(body)
    assert runtime.bridge.llm is not old_llm
    env = (tmp_path / ".env").read_text()
    assert "AZURE_AI_MODEL=new-model" in env and "AZURE_AI_API_KEY=new-secret" in env
    assert "AZURE_AI_ENDPOINT=https://old.openai.azure.com/" in env, "blank endpoint keeps the current one"


def test_rejects_other_origins_and_hosts(tmp_path):
    async def scenario(client):
        evil_origin = await client.post("/api/settings", json={"trigger": "x"}, headers={"Origin": "https://evil.example"})
        evil_host = await client.get("/api/settings", headers={"Host": "evil.example"})
        own_origin = await client.get("/api/status", headers={"Origin": f"http://localhost:{client.port}"})
        return evil_origin.status, evil_host.status, own_origin.status

    assert run(make_runtime(tmp_path), scenario) == (403, 403, 200)


def test_player_history_and_reset(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.bridge._history["Steve"] = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

    async def scenario(client):
        players = await (await client.get("/api/players")).json()
        history = await (await client.get("/api/players/Steve/history")).json()
        await client.delete("/api/players/Steve/history")
        after = await (await client.get("/api/players/Steve/history")).json()
        return players, history, after

    players, history, after = run(runtime, scenario)
    assert players == [{"name": "Steve", "online": False, "messages": 2}]
    assert [m["content"] for m in history["history"]] == ["hi", "hello"]
    assert after["history"] == []
    assert runtime.events.recent()[-1] == {**runtime.events.recent()[-1], "type": "reset", "player": "Steve"}


def test_event_stream_sends_backlog_and_live_events(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.events.publish({"type": "connected", "remote": "127.0.0.1:1"})

    async def scenario(client):
        resp = await client.get("/api/events")
        first = json.loads((await resp.content.readuntil(b"\n\n"))[len(b"data: "):])
        runtime.events.publish({"type": "question", "player": "Steve", "text": "hi"})
        second = json.loads((await resp.content.readuntil(b"\n\n"))[len(b"data: "):])
        resp.close()
        return first, second

    first, second = run(runtime, scenario)
    assert first["type"] == "connected" and first["id"] == 1
    assert second["type"] == "question" and second["text"] == "hi" and "time" in second


def test_index_page_served(tmp_path):
    async def scenario(client):
        resp = await client.get("/")
        return resp.status, await resp.text()

    status, html = run(make_runtime(tmp_path), scenario)
    assert status == 200 and "<title>Minecraft Skill Builder</title>" in html


def test_rubric_api_crud(tmp_path):
    runtime = make_runtime(tmp_path)

    async def scenario(client):
        empty = await (await client.get("/api/rubrics")).json()
        created = await client.put("/api/rubrics/castle", json={"text": "# Build a Castle\n\n## Task\n\nBuild a castle."})
        listed = await (await client.get("/api/rubrics")).json()
        one = await (await client.get("/api/rubrics/castle")).json()
        bad_id = await client.put("/api/rubrics/..%2Fescape", json={"text": "# x"})
        bad_body = await client.put("/api/rubrics/castle", json={"nope": 1})
        deleted = await client.delete("/api/rubrics/castle")
        missing = await client.get("/api/rubrics/castle")
        return empty, created.status, listed, one, bad_id.status, bad_body.status, deleted.status, missing.status

    empty, created, listed, one, bad_id, bad_body, deleted, missing = run(runtime, scenario)
    assert empty == [] and created == 200
    assert listed == [{"id": "castle", "title": "Build a Castle"}]
    assert one["text"].startswith("# Build a Castle")
    assert bad_id in (400, 404) and bad_body == 400
    assert deleted == 200 and missing == 404
    assert not (tmp_path / "escape.md").exists()
    assert not (tmp_path / "rubrics" / "castle.md").exists()


def test_status_lists_assessments(tmp_path):
    async def scenario(client):
        return await (await client.get("/api/status")).json()

    assert run(make_runtime(tmp_path), scenario)["assessments"] == []
