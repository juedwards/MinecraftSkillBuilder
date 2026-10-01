import asyncio
import json

from websockets.asyncio.client import connect

from mcchat.bridge import BridgeConfig, ChatBridge, strip_markdown
from mcchat.config import save_azure_credentials
from mcchat.llm import EchoLLM, normalize_endpoint
from mcchat.minecraft import MinecraftServer, parse_player_message, quote_target, split_for_chat, tellraw_command
from mcchat.setup_wizard import AzureCredentials


def player_message_event(sender: str, message: str, msg_type: str = "chat") -> str:
    return json.dumps({
        "header": {"version": 1, "requestId": "00000000-0000-0000-0000-000000000000",
                   "messagePurpose": "event", "eventName": "PlayerMessage"},
        "body": {"message": message, "sender": sender, "receiver": "", "type": msg_type},
    })


def test_parse_player_message():
    msg = parse_player_message(json.loads(player_message_event("Steve", "hello")))
    assert (msg.sender, msg.message, msg.type) == ("Steve", "hello", "chat")


def test_parse_legacy_properties_format():
    data = {"header": {"messagePurpose": "event"},
            "body": {"eventName": "PlayerMessage", "properties": {"Message": "hi", "Sender": "Alex", "MessageType": "chat"}}}
    msg = parse_player_message(data)
    assert (msg.sender, msg.message) == ("Alex", "hi")


def test_parse_ignores_other_events():
    assert parse_player_message({"header": {"messagePurpose": "commandResponse"}, "body": {}}) is None


def test_tellraw_escapes_quotes():
    cmd = tellraw_command('say "hi"', quote_target('Bob "B"'))
    prefix = 'tellraw "Bob \\"B\\"" '
    assert cmd.startswith(prefix)
    assert json.loads(cmd[len(prefix):]) == {"rawtext": [{"text": 'say "hi"'}]}


def test_split_for_chat():
    chunks = split_for_chat("first line\n\n" + "word " * 100, width=50)
    assert chunks[0] == "first line"
    assert all(len(c) <= 50 for c in chunks)


def test_strip_markdown():
    assert strip_markdown("## Title\n**bold** and `code`") == "Title\nbold and code"


def test_normalize_endpoint():
    expected = "https://res.services.ai.azure.com/openai/v1/"
    for url in [
        "https://res.services.ai.azure.com",
        "https://res.services.ai.azure.com/",
        "https://res.services.ai.azure.com/openai/v1/",
        "https://res.services.ai.azure.com/models",
        "https://res.services.ai.azure.com/api/projects/myproj",
    ]:
        assert normalize_endpoint(url) == expected


def test_normalize_non_azure_endpoint():
    assert normalize_endpoint("http://localhost:8000") == "http://localhost:8000/v1/"
    assert normalize_endpoint("http://localhost:8000/v1") == "http://localhost:8000/v1/"


PLAYER_INFO = {"dimension": 0, "position": {"x": 10.5, "y": -58.38, "z": 20.5}, "uniqueId": "-1", "yRot": 0.0}


async def fake_minecraft(port: int, chats: list[tuple[str, str]], expected_replies: int,
                         commands: list[str] | None = None) -> list[tuple[str, str]]:
    """Act like Minecraft: send chat events, answer command requests, collect (target, text) of each tellraw.

    Other commands are appended to `commands`; /querytarget answers with PLAYER_INFO.
    """
    replies: list[tuple[str, str]] = []
    async with connect(f"ws://127.0.0.1:{port}") as ws:
        subscribe = json.loads(await ws.recv())
        assert subscribe["header"]["messagePurpose"] == "subscribe"
        assert subscribe["body"]["eventName"] == "PlayerMessage"
        for sender, text in chats:
            await ws.send(player_message_event(sender, text))
        while len(replies) < expected_replies:
            request = json.loads(await asyncio.wait_for(ws.recv(), 5))
            command = request["body"]["commandLine"]
            body = {"statusCode": 0}
            if command.startswith("tellraw "):
                target, payload = command[len("tellraw "):].split(' {"rawtext"', 1)
                replies.append((target, json.loads('{"rawtext"' + payload)["rawtext"][0]["text"]))
            else:
                assert commands is not None, f"unexpected command: {command}"
                commands.append(command)
                if command.startswith("querytarget "):
                    body["details"] = json.dumps([PLAYER_INFO])
            # Minecraft echoes nothing for tellraw, but does send a commandResponse.
            await ws.send(json.dumps({
                "header": {"requestId": request["header"]["requestId"], "messagePurpose": "commandResponse"},
                "body": body,
            }))
    return replies


def run_scenario(bridge: ChatBridge, chats: list[tuple[str, str]], expected_replies: int,
                 on_connect=None, commands: list[str] | None = None) -> list[tuple[str, str]]:
    async def scenario():
        server = MinecraftServer("127.0.0.1", 0, bridge.handle_chat, on_connect)
        await server.start()
        try:
            return await fake_minecraft(server.port, chats, expected_replies, commands)
        finally:
            await server.close()

    return asyncio.run(scenario())


def run_bridge(config: BridgeConfig, chats: list[tuple[str, str]], expected_replies: int,
               on_connect=None) -> tuple[list[str], ChatBridge]:
    bridge = ChatBridge(EchoLLM(), config)
    replies = run_scenario(bridge, chats, expected_replies, on_connect)
    return [text for _, text in replies], bridge


def test_end_to_end_reply_and_history():
    replies, bridge = run_bridge(BridgeConfig(), [("Steve", "hello there")], 1)
    assert replies == ["§b[AI]§r You said: hello there"]
    assert [m["role"] for m in bridge.history("Steve")] == ["user", "assistant"]


def test_welcome_message_on_connect():
    async def welcome(conn):
        await conn.send_chat("welcome")

    replies, _ = run_bridge(BridgeConfig(), [("Steve", "hi")], 2, on_connect=welcome)
    assert sorted(replies) == ["welcome", "§b[AI]§r You said: hi"]


def test_trigger_filters_messages():
    replies, _ = run_bridge(BridgeConfig(trigger="!ai"), [("Steve", "ignore me"), ("Steve", "!ai what is redstone")], 1)
    assert replies == ["§b[AI]§r You said: what is redstone"]


def test_reset_command():
    replies, bridge = run_bridge(BridgeConfig(), [("Steve", "hi"), ("Steve", "!reset")], 2)
    assert replies[1].endswith("Conversation cleared.")
    assert bridge.history("Steve") == []


class FailingLLM:
    async def complete(self, messages):
        raise RuntimeError("Error code: 401 - invalid key")


SETUP_ANSWERS = [
    ("Steve", "!setup"),
    ("Steve", "https://res.services.ai.azure.com/"),
    ("Steve", "secret-key"),
    ("Steve", "gpt-test"),
]


def test_setup_collects_credentials_and_connects():
    saved, events = [], []
    bridge = ChatBridge(None, BridgeConfig(), on_event=events.append,
                        llm_factory=lambda creds: EchoLLM(), save_credentials=saved.append)
    # intro + step 1, step 2, step 3, testing, connected, then a normal answer
    replies = run_scenario(bridge, SETUP_ANSWERS + [("Steve", "hello")], 7)

    assert [t for t, _ in replies[:6]] == ['"Steve"'] * 6, "setup replies are private"
    assert "Step 1/3" in replies[1][1] and "Step 2/3" in replies[2][1] and "Step 3/3" in replies[3][1]
    assert replies[5][1].endswith("Connected to gpt-test! Saved for next time. Ask me anything.")
    assert replies[6] == ("@a", "§b[AI]§r You said: hello")
    assert saved == [AzureCredentials("https://res.services.ai.azure.com/", "secret-key", "gpt-test")]
    assert "secret-key" not in repr(replies) + repr(events), "API key is never echoed or logged"


def test_setup_connection_failure_keeps_unconfigured():
    saved = []
    bridge = ChatBridge(None, BridgeConfig(), llm_factory=lambda creds: FailingLLM(), save_credentials=saved.append)
    replies = run_scenario(bridge, SETUP_ANSWERS, 6)
    assert "Couldn't connect: Error code: 401" in replies[5][1]
    assert bridge.llm is None and saved == []


def test_setup_rejects_bad_endpoint_and_can_cancel():
    bridge = ChatBridge(None, BridgeConfig(), llm_factory=lambda creds: EchoLLM())
    replies = run_scenario(bridge, [("Steve", "!setup"), ("Steve", "not a url"), ("Steve", "!cancel"), ("Steve", "hi")], 5)
    texts = [text for _, text in replies]
    assert "doesn't look like an endpoint URL" in texts[2] and "Step 1/3" in texts[3]
    assert texts[4].endswith("Setup cancelled.")


def test_unconfigured_bot_points_to_setup():
    bridge = ChatBridge(None, BridgeConfig(), llm_factory=lambda creds: EchoLLM())
    (_, text), = run_scenario(bridge, [("Steve", "hello")], 1)
    assert "Type !setup" in text


def test_setup_refused_when_already_configured():
    bridge = ChatBridge(EchoLLM(), BridgeConfig(), llm_factory=lambda creds: EchoLLM())
    (_, text), = run_scenario(bridge, [("Steve", "!setup")], 1)
    assert "already connected" in text


def test_setup_is_one_player_at_a_time():
    bridge = ChatBridge(None, BridgeConfig(), llm_factory=lambda creds: EchoLLM())
    replies = run_scenario(bridge, [("Steve", "!setup"), ("Alex", "!setup")], 3)
    assert ('"Alex"', "§b[AI]§r Steve is already running setup.") in replies


def test_save_azure_credentials_keeps_other_settings(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MC_PORT=3000\nAZURE_AI_MODEL=old\n")
    save_azure_credentials(AzureCredentials("https://x.openai.azure.com/", "k", "new"), env)
    text = env.read_text()
    assert "MC_PORT=3000" in text and "AZURE_AI_MODEL=new" in text and "AZURE_AI_MODEL=old" not in text
    assert "AZURE_AI_API_KEY=k" in text


class ScriptedLLM:
    def __init__(self, reply: str):
        self.reply = reply

    async def complete(self, messages):
        return self.reply


HUT = """<build_planning>tiny hut</build_planning>
<description>A tiny stone hut.</description>
<code>
function buildCreation(startX, startY, startZ) {
  safeFill(startX, startY, startZ, startX + 4, startY, startZ + 4, "stone");
  safeFill(startX, startY + 1, startZ, startX + 4, startY + 3, startZ + 4, "oak_planks", { mode: "hollow" });
  safeSetBlock(startX + 2, startY + 1, startZ, "air");
}
</code>"""


def test_build_end_to_end():
    commands, events = [], []
    bridge = ChatBridge(ScriptedLLM(HUT), BridgeConfig(), on_event=events.append)
    replies = run_scenario(bridge, [("Steve", "!build a tiny hut")], 2, commands=commands)

    assert replies[0][1].endswith("Designing a tiny hut... this can take a minute.")
    assert replies[1][1].endswith("Built a tiny hut (5x4x5, 3 commands).")
    # Player feet at (10, -60, 20) facing south (+Z): ground is y=-61, build starts 2 blocks ahead, centred on x.
    assert commands == [
        'querytarget "Steve"',
        "fill 8 -61 22 12 -61 26 stone",
        "fill 8 -60 22 12 -58 26 oak_planks hollow",
        "setblock 10 -60 22 air",
    ]


def test_build_reports_bad_code():
    bridge = ChatBridge(ScriptedLLM("Sorry, I can't."), BridgeConfig())
    replies = run_scenario(bridge, [("Steve", "!build a castle")], 2, commands=[])
    assert "Build failed: The AI didn't return any build code." in replies[1][1]


def test_build_needs_a_request():
    bridge = ChatBridge(ScriptedLLM(HUT), BridgeConfig())
    (_, text), = run_scenario(bridge, [("Steve", "!build")], 1)
    assert "Tell me what to build" in text
