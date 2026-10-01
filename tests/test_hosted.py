import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from mcchat.bridge import BridgeConfig, ChatBridge
from mcchat.cli import use_data_dir
from mcchat.config import Settings
from mcchat.llm import EchoLLM
from mcchat.minecraft import MinecraftConnection
from mcchat.runtime import Runtime
from mcchat.webapp import create_app

SIGNED_IN = {"X-MS-CLIENT-PRINCIPAL-NAME": "teacher@school.example"}


def hosted_runtime(tmp_path, join_code="secret-123") -> Runtime:
    settings = Settings(hosted=True, join_code=join_code, azure_model="gpt-test")
    return Runtime(settings, EchoLLM(), env_path=tmp_path / ".env", rubrics_dir=tmp_path / "rubrics")


def run(runtime: Runtime, scenario):
    async def main():
        async with TestClient(TestServer(create_app(runtime))) as client:
            return await scenario(client)

    return asyncio.run(main())


def test_hosted_pages_fail_closed_without_platform_sign_in(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)

    async def scenario(client):
        return (await client.get("/api/status", headers=SIGNED_IN)).status  # a forged header doesn't help

    assert run(hosted_runtime(tmp_path), scenario) == 503


def test_hosted_pages_require_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "True")

    async def scenario(client):
        anonymous = await client.get("/api/settings")
        page = await client.get("/", headers=SIGNED_IN)
        status = await client.get("/api/status", headers={**SIGNED_IN, "X-Forwarded-Proto": "https", "Host": "msb.example.net"})
        cross_site = await client.post("/api/settings", json={"trigger": "x"}, headers={**SIGNED_IN, "Origin": "https://evil.example"})
        return anonymous.status, page.status, await status.json(), cross_site.status

    anonymous, page, status, cross_site = run(hosted_runtime(tmp_path), scenario)
    assert anonymous == 401 and page == 200 and cross_site == 403
    assert status["user"] == "teacher@school.example"
    assert status["connect"] == "/connect wss://msb.example.net/mc/secret-123"


def test_public_url_overrides_connect_address(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSITE_AUTH_ENABLED", "true")
    runtime = hosted_runtime(tmp_path)
    runtime.settings.public_url = "https://skills.example.org"

    async def scenario(client):
        return await (await client.get("/api/status", headers=SIGNED_IN)).json()

    assert run(runtime, scenario)["connect"] == "/connect wss://skills.example.org/mc/secret-123"


def player_message(sender: str, text: str) -> str:
    return json.dumps({"header": {"messagePurpose": "event", "eventName": "PlayerMessage"},
                       "body": {"message": text, "sender": sender, "type": "chat"}})


def test_minecraft_connects_through_the_web_server_with_the_join_code(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBSITE_AUTH_ENABLED", raising=False)  # Minecraft's path doesn't need sign-in
    runtime = hosted_runtime(tmp_path)

    async def scenario(client):
        wrong = await client.get("/mc/guess")
        missing = await client.get("/mc")
        browser = await client.get("/mc/secret-123")
        ws = await client.ws_connect("/mc/secret-123")
        subscribed = json.loads((await ws.receive()).data)
        assert subscribed["header"]["messagePurpose"] == "subscribe"
        await ws.send_str(player_message("Steve", "hello"))
        replies = []
        while not any("You said: hello" in r for r in replies):
            request = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
            if request["header"]["messagePurpose"] != "commandRequest":
                continue
            replies.append(request["body"]["commandLine"])
            await ws.send_str(json.dumps({"header": {"requestId": request["header"]["requestId"], "messagePurpose": "commandResponse"},
                                          "body": {"statusCode": 0}}))
        connected = len(runtime.connections)
        await ws.close()
        await asyncio.sleep(0.05)
        return wrong.status, missing.status, await browser.text(), replies, connected, len(runtime.connections)

    wrong, missing, browser, replies, connected, after = run(runtime, scenario)
    assert wrong == 403 and missing == 403
    assert "/connect" in browser and "secret-123" in browser
    assert any("AI chat connected" in r for r in replies) and any("You said: hello" in r for r in replies)
    assert connected == 1 and after == 0


def test_hosted_needs_a_join_code(tmp_path):
    runtime = hosted_runtime(tmp_path, join_code="")

    async def scenario(client):
        return (await client.get("/mc")).status

    assert run(runtime, scenario) == 403, "no join code configured: Minecraft is refused"


def test_each_world_builds_independently():
    async def send(text):
        pass

    bridge = ChatBridge(EchoLLM(), BridgeConfig())
    class_a, class_b = MinecraftConnection(send, "a"), MinecraftConnection(send, "b")
    assert bridge.begin_build(class_a, "Steve") is None
    assert bridge.begin_build(class_b, "Alex") is None, "another world isn't blocked"
    assert bridge.begin_build(class_a, "Sam") == "Steve", "the same world waits its turn"
    assert bridge.building == "Steve, Alex"
    bridge.end_build(class_a)
    assert bridge.begin_build(class_a, "Sam") is None


def test_data_dir_gets_example_rubrics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    use_data_dir(tmp_path / "data")
    assert (tmp_path / "data" / "rubrics" / "build_a_bridge.md").is_file()
    assert __import__("pathlib").Path.cwd() == tmp_path / "data"
    (tmp_path / "data" / "rubrics" / "build_a_bridge.md").write_text("# Changed\n")
    use_data_dir(tmp_path / "data")
    assert (tmp_path / "data" / "rubrics" / "build_a_bridge.md").read_text() == "# Changed\n", "never overwrites teachers' rubrics"
