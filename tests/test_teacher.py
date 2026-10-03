import asyncio
import io

from mcchat.config import Settings
from mcchat.console import Console
from mcchat.llm import EchoLLM
from mcchat.runtime import Runtime
from mcchat.teacher import TeacherConsole, parse_reply

QUEST = "# Build a Tower\n\n## Task\n\nBuild a tower.\n"


class ScriptedLLM:
    """Replies with the given texts in turn, and remembers what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    async def complete(self, messages):
        self.sent.append(messages)
        return self.replies.pop(0)


def make(tmp_path, llm=None, answers=(), edit=None):
    runtime = Runtime(Settings(), llm or EchoLLM(), env_path=tmp_path / ".env",
                      rubrics_dir=tmp_path / "rubrics", reports_dir=tmp_path / "reports")
    out = io.StringIO()
    queue = list(answers)

    async def read(prompt, hidden):
        return queue.pop(0)

    async def no_editor(text):
        return None

    console = TeacherConsole(runtime, Console(out, colour=False, unicode=True), read=read, edit=edit or no_editor)
    return runtime, console, out


def test_parse_reply_with_action():
    reply = parse_reply('Done soon.\n```action\n{"action": "update_settings", "settings": {"trigger": "!ai", "api_key": "x"}}\n```')
    assert reply.text == "Done soon."
    assert reply.proposal.action == "update_settings"
    assert reply.proposal.data == {"settings": {"trigger": "!ai"}}   # credentials are never taken from the AI


def test_parse_reply_rejects_bad_json_and_unknown_settings():
    assert parse_reply("ok ```action {nope ```").problem
    assert parse_reply('```action\n{"action": "update_settings", "settings": {"azure_model": "x"}}\n```').problem
    assert parse_reply("Just text.").proposal is None


def test_assistant_writes_a_quest_after_confirmation(tmp_path):
    action = '```action\n{"action": "save_quest", "id": "tower", "text": "# Build a Tower\\n\\n## Task\\n\\nBuild a tower.\\n"}\n```'
    llm = ScriptedLLM("Here's a tower quest.\n" + action)
    runtime, console, out = make(tmp_path, llm, answers=["y"])
    asyncio.run(console.handle("write a quest about towers"))
    assert runtime.rubrics.get("tower").title == "Build a Tower"
    assert "New quest" in out.getvalue() and "saved as tower.md" in out.getvalue()
    assert "Quest template" in llm.sent[0][0]["content"]


def test_declined_change_is_not_applied(tmp_path):
    llm = ScriptedLLM('Sure.\n```action\n{"action": "update_settings", "settings": {"trigger": "!ai"}}\n```')
    runtime, console, out = make(tmp_path, llm, answers=["n"])
    asyncio.run(console.handle("only answer !ai"))
    assert runtime.bridge.config.trigger == ""
    assert "Not applied." in out.getvalue()
    assert "did not apply" in console.assistant.history[-1]["content"]


def test_set_commands(tmp_path):
    runtime, console, out = make(tmp_path)
    asyncio.run(console.handle("/set trigger !ai"))
    asyncio.run(console.handle("/set private on"))
    asyncio.run(console.handle("/set history 999"))
    assert runtime.bridge.config.trigger == "!ai" and runtime.bridge.config.reply_private is True
    assert "between 0 and 200" in out.getvalue()
    asyncio.run(console.handle("/set trigger off"))
    assert runtime.bridge.config.trigger == ""


def test_quest_commands(tmp_path):
    edited = []

    async def editor(text):
        edited.append(text)
        return QUEST

    runtime, console, out = make(tmp_path, answers=["y"], edit=editor)
    asyncio.run(console.handle("/new tower"))
    assert "## Learning aims" in edited[0]           # starts from the template
    asyncio.run(console.handle("/quests"))
    asyncio.run(console.handle("/show tower"))
    assert "Build a Tower" in out.getvalue()
    asyncio.run(console.handle("/delete tower"))
    assert runtime.rubrics.get("tower") is None
    asyncio.run(console.handle("/show nothing"))
    assert 'There is no quest "nothing"' in out.getvalue()


def test_new_quest_without_editor(tmp_path):
    runtime, console, out = make(tmp_path)
    asyncio.run(console.handle("/new tower"))
    assert "No text editor found" in out.getvalue() and runtime.rubrics.get("tower") is None


def test_quit_and_unknown(tmp_path):
    _, console, out = make(tmp_path)
    assert asyncio.run(console.handle("/quit")) is False
    assert asyncio.run(console.handle("/nope")) is True
    assert "Unknown command /nope" in out.getvalue()


def test_setup_saves_all_credentials_at_once(tmp_path):
    runtime = Runtime(Settings(), None, llm_factory=lambda creds: EchoLLM(), env_path=tmp_path / ".env",
                      rubrics_dir=tmp_path / "rubrics", reports_dir=tmp_path / "reports")
    answers = ["https://me.services.ai.azure.com/", "gpt-5-chat", "secret"]

    async def read(prompt, hidden):
        assert hidden == prompt.startswith("API key")
        return answers.pop(0)

    console = TeacherConsole(runtime, Console(io.StringIO(), colour=False, unicode=True), read=read)
    asyncio.run(console.handle("/setup"))
    assert runtime.bridge.llm is not None and runtime.settings.azure_model == "gpt-5-chat"
    assert "AZURE_AI_API_KEY" in (tmp_path / ".env").read_text()
