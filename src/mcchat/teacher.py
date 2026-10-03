"""The teacher console: a prompt in the server's terminal for settings and quests.

Lines starting with / are commands (/help lists them). Anything else goes to the teacher's
assistant, an AI that can propose a change (a setting, or a quest to write, change or delete).
Every change is shown and confirmed before it's applied. Activity keeps scrolling above the prompt.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .assessment import RUBRIC_ID, RubricError
from .bridge import DEFAULT_SYSTEM_PROMPT, short_error
from .console import Console
from .runtime import Runtime, SettingsError
from .usage import TEACHER, usage_context

QUEST_TEMPLATE = """# Quest title

## Learning aims

What students should understand by the end.

## Learning objectives

1. **Objective.** What the student can do.

## Task

What the student is asked to build. End with: Type **finished** in chat when you are done.

## Starter build

The partially completed scene the AI builds before the student starts, and which part is left for the student.

## Assessment criteria

| Criterion | Beginning | Developing | Secure | Excellent |
|---|---|---|---|---|
| Criterion name | ... | ... | ... | ... |
"""

# Settings the assistant may change. Credentials are only changed with /set, never by the AI.
ASSISTANT_SETTINGS = {
    "trigger", "reply_private", "max_history", "system_prompt",
    "currency", "price_input_per_million", "price_output_per_million",
}
MAX_HISTORY_TURNS = 12      # assistant conversation kept, in messages
MAX_QUEST_CONTEXT = 30_000  # characters of quest text shown to the assistant

ASSISTANT_PROMPT = """\
You are the teacher's assistant in Minecraft Quest Builder, an AI companion for Minecraft Education \
and Bedrock. The teacher types to you in the server's terminal. You help them change the app's \
settings and write, improve and delete quests.

A quest is a Markdown file with a building challenge. Students take one in game with !challenge: the \
AI builds a starter scene, the student builds, types "finished", and gets formative feedback against \
the quest's assessment criteria (levels Beginning, Developing, Secure, Excellent).

Reply briefly in plain text: short sentences, no Markdown headings or tables (a short list is fine). \
To make a change, say in a sentence what you'll do and end your reply with exactly one action block:

```action
{"action": "update_settings", "settings": {"trigger": "!ai"}}
```
Settings: trigger (one word that chat must start with, "" to answer all chat), reply_private (true: \
reply only to the player who asked), max_history (messages remembered per player, 0-200), \
system_prompt (the in-game AI's instructions, "" for the default), currency (e.g. "£"), \
price_input_per_million and price_output_per_million (AI prices for the Costs page).
You can't change the Azure endpoint, model or API key: tell the teacher to use /set endpoint, \
/set model or /set key.

```action
{"action": "save_quest", "id": "build_a_bridge", "text": "# Build a Bridge\\n\\n## Learning aims\\n..."}
```
The id is the file name: lowercase letters, numbers, - and _. Saving an existing id replaces that \
quest, so to change a quest send its whole updated text. Follow the template and the style of the \
existing quests: concrete, age-appropriate tasks that can be built in Minecraft in 10-20 minutes, a \
starter build the AI can make with commands, and 4-6 criteria a teacher could judge by looking.

```action
{"action": "delete_quest", "id": "build_a_bridge"}
```

The teacher confirms every action before it's applied, so never say a change has been made. \
If a request is unclear, ask one short question instead.
"""


@dataclass
class Proposal:
    """A change the assistant wants to make, waiting for the teacher's confirmation."""
    action: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantReply:
    text: str
    proposal: Proposal | None = None
    problem: str = ""   # why an action block couldn't be used


def parse_reply(reply: str) -> AssistantReply:
    """Split the assistant's reply into its text and an optional ```action block."""
    start = reply.find("```action")
    if start < 0:
        return AssistantReply(reply.strip())
    text = reply[:start].strip()
    body = reply[start + len("```action"):]
    end = body.rfind("```")
    try:
        data = json.loads(body[:end] if end >= 0 else body)
    except json.JSONDecodeError:
        return AssistantReply(text, problem="The assistant's change was not valid JSON. Ask it to try again.")
    if not isinstance(data, dict):
        return AssistantReply(text, problem="The assistant's change was not understood.")
    action = data.get("action")
    if action == "update_settings" and isinstance(data.get("settings"), dict):
        settings = {k: v for k, v in data["settings"].items() if k in ASSISTANT_SETTINGS}
        if not settings:
            return AssistantReply(text, problem="The assistant tried to change a setting it can't change.")
        return AssistantReply(text, Proposal(action, {"settings": settings}))
    if action == "save_quest" and isinstance(data.get("id"), str) and isinstance(data.get("text"), str):
        return AssistantReply(text, Proposal(action, {"id": data["id"].strip(), "text": data["text"]}))
    if action == "delete_quest" and isinstance(data.get("id"), str):
        return AssistantReply(text, Proposal(action, {"id": data["id"].strip()}))
    return AssistantReply(text, problem="The assistant's change was not understood.")


class TeacherAssistant:
    """The AI behind free-text lines in the console. It only proposes changes; it never applies them."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.history: list[dict[str, str]] = []

    def context(self) -> str:
        settings = {k: v for k, v in self.runtime.settings_view().items() if k not in ("default_system_prompt", "api_key_set")}
        if settings.get("system_prompt") == DEFAULT_SYSTEM_PROMPT:
            settings["system_prompt"] = "(default)"
        settings.update(currency=self.runtime.settings.currency,
                        price_input_per_million=self.runtime.settings.price_input_per_million,
                        price_output_per_million=self.runtime.settings.price_output_per_million)
        parts = [ASSISTANT_PROMPT, "Current settings:", json.dumps(settings, indent=1, ensure_ascii=False)]
        quests = self.runtime.rubrics.list()
        if not quests:
            parts.append("There are no quests yet.")
        used = 0
        for quest in quests:
            if used + len(quest.text) <= MAX_QUEST_CONTEXT:
                parts.append(f'Quest id "{quest.id}":\n{quest.text}')
                used += len(quest.text)
            else:
                parts.append(f'Quest id "{quest.id}": {quest.title} (text not shown)')
        parts.append("Quest template:\n" + QUEST_TEMPLATE)
        return "\n\n".join(parts)

    async def ask(self, text: str) -> AssistantReply:
        llm = self.runtime.bridge.llm
        if llm is None:
            return AssistantReply("", problem="The AI isn't connected yet. Use /set endpoint, /set model and /set key first.")
        self.history.append({"role": "user", "content": text})
        messages = [{"role": "system", "content": self.context()}, *self.history[-MAX_HISTORY_TURNS:]]
        try:
            with usage_context("(teacher)", TEACHER):
                reply = await llm.complete(messages)
        except Exception as exc:
            self.history.pop()
            return AssistantReply("", problem=f"The AI request failed: {short_error(exc, 200)}")
        self.history.append({"role": "assistant", "content": reply})
        return parse_reply(reply)

    def note(self, text: str) -> None:
        """Tell the assistant what happened to its proposal, so its next answer is accurate."""
        self.history.append({"role": "user", "content": f"(Console note: {text})"})

    def clear(self) -> None:
        self.history.clear()


# ----- the interactive console -------------------------------------------------------

InputFn = Callable[[str, bool], Awaitable[str]]          # (prompt, hide input) -> line
EditFn = Callable[[str], Awaitable["str | None"]]        # text -> edited text, None if no editor

SET_HELP = [
    ("trigger <word|off>", "only answer chat starting with this word"),
    ("private on|off", "reply only to the player who asked"),
    ("history <0-200>", "messages remembered per player"),
    ("prompt <text|default>", "the in-game AI's instructions"),
    ("endpoint <url>", "Azure AI Foundry endpoint (tested before saving)"),
    ("model <name>", "model deployment name (tested before saving)"),
    ("key", "Azure API key (asked for, hidden)"),
    ("currency <symbol>", "currency for the Costs page"),
    ("price-in <n>, price-out <n>", "AI price per million tokens"),
]

COMMAND_HELP = [
    ("/status", "Minecraft, AI and quests in progress"),
    ("/settings", "show the settings"),
    ("/set <setting> <value>", "change a setting (/set on its own lists them)"),
    ("/quests", "list the quests"),
    ("/show <quest>", "show a quest"),
    ("/new [quest]", "write a new quest in your editor"),
    ("/edit <quest>", "edit a quest in your editor"),
    ("/delete <quest>", "delete a quest"),
    ("/players", "players online and who has talked to the AI"),
    ("/say <message>", "send a message to everyone in Minecraft"),
    ("/clear", "start a new conversation with the assistant"),
    ("/quit", "stop the server"),
]


def find_editor() -> list[str] | None:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return shlex.split(editor)
    for name in ("nano", "vim", "vi", "notepad"):
        if shutil.which(name):
            return [name]
    return None


async def edit_in_editor(text: str) -> str | None:
    command = find_editor()
    if command is None:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        # In a thread, so Minecraft and the web interface keep running while the editor is open.
        await asyncio.to_thread(subprocess.call, [*command, str(path)])
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


def on_off(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("on", "true", "yes", "1"):
        return True
    if lowered in ("off", "false", "no", "0"):
        return False
    raise SettingsError("Use on or off.")


class TeacherConsole:
    def __init__(self, runtime: Runtime, console: Console, read: InputFn | None = None, edit: EditFn | None = None):
        self.runtime = runtime
        self.console = console
        self.assistant = TeacherAssistant(runtime)
        self._read = read
        self._edit = edit or edit_in_editor
        self._session: Any = None

    # ----- output helpers -----

    def say(self, text: str, tone: str = "") -> None:
        self.console.block("assistant", text, tone)

    def fail(self, text: str) -> None:
        self.console.block("error", text, "31")

    def rows(self, rows: list[tuple[str, str]]) -> None:
        width = max((len(label) for label, _ in rows), default=0) + 3
        for label, value in rows:
            self.console.write(f"  {self.console.style(f'{label:<{width}}', '2')}{value}")

    async def ask(self, prompt: str, hidden: bool = False) -> str:
        if self._read is not None:
            return await self._read(prompt, hidden)
        return await self._session.prompt_async(prompt, is_password=hidden)

    async def confirm(self, question: str) -> bool:
        answer = await self.ask(f"{question} [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    # ----- the loop -----

    async def run(self) -> None:
        """Prompt until /quit, Ctrl+C or Ctrl+D, with activity printed above the prompt."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import NestedCompleter
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.patch_stdout import patch_stdout

        self._session = PromptSession(bottom_toolbar=self.toolbar, refresh_interval=2)
        prompt = HTML("<ansibrightblue><b>quest builder</b></ansibrightblue> <ansigray>›</ansigray> ")
        with patch_stdout(raw=True):
            while True:
                ids = {quest.id: None for quest in self.runtime.rubrics.list()}
                self._session.completer = NestedCompleter.from_nested_dict({
                    "/help": None, "/status": None, "/settings": None, "/quests": None, "/players": None,
                    "/clear": None, "/quit": None, "/new": None, "/say": None,
                    "/set": {name.split()[0].rstrip(","): None for name, _ in SET_HELP} | {"price-out": None},
                    "/show": ids, "/edit": ids, "/delete": ids,
                })
                try:
                    line = await self._session.prompt_async(prompt)
                except (EOFError, KeyboardInterrupt):
                    return
                try:
                    if not await self.handle(line):
                        return
                except (EOFError, KeyboardInterrupt):
                    self.console.info("Cancelled.")

    def toolbar(self) -> Any:
        from prompt_toolkit.formatted_text import HTML

        worlds = len(self.runtime.connections)
        minecraft = f"{worlds} world{'s' if worlds != 1 else ''} connected" if worlds else "not connected"
        ai = self.runtime.model_label or "not connected"
        quests = len(self.runtime.rubrics.list())
        return HTML(f" Minecraft: <b>{minecraft}</b>  │  AI: <b>{ai}</b>  │  <b>{quests}</b> quests  │  "
                    "/help for commands, or ask for a change  │  Ctrl+C to stop ")

    async def handle(self, line: str) -> bool:
        """Run one line. Returns False to stop."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            await self.chat(line)
            return True
        command, _, rest = line.partition(" ")
        command, rest = command.lower(), rest.strip()
        handlers: dict[str, Callable[[str], Awaitable[None]]] = {
            "/help": self.cmd_help, "/status": self.cmd_status, "/settings": self.cmd_settings,
            "/set": self.cmd_set, "/quests": self.cmd_quests, "/show": self.cmd_show, "/quest": self.cmd_show,
            "/new": self.cmd_new, "/edit": self.cmd_edit, "/delete": self.cmd_delete,
            "/players": self.cmd_players, "/say": self.cmd_say, "/clear": self.cmd_clear,
        }
        if command in ("/quit", "/exit", "/stop"):
            return False
        handler = handlers.get(command)
        if handler is None:
            self.fail(f"Unknown command {command}. Type /help to see the commands.")
            return True
        try:
            await handler(rest)
        except (SettingsError, RubricError) as exc:
            self.fail(str(exc))
        return True

    # ----- the assistant -----

    async def chat(self, text: str) -> None:
        self.console.line("assistant", "", "thinking...", tone="2")
        reply = await self.assistant.ask(text)
        if reply.text:
            self.say(reply.text)
        if reply.problem:
            self.fail(reply.problem)
        if reply.proposal:
            await self.review(reply.proposal)

    async def review(self, proposal: Proposal) -> None:
        """Show a proposed change, ask for confirmation and apply it."""
        if not self.preview(proposal):
            return
        if not await self.confirm("Apply this change?"):
            self.console.info("Not applied.")
            self.assistant.note("the teacher did not apply that change")
            return
        try:
            done = await self.apply(proposal)
        except (SettingsError, RubricError) as exc:
            self.fail(str(exc))
            self.assistant.note(f"the change failed: {exc}")
            return
        self.console.info(done, tone="32")
        self.assistant.note(f"applied: {done}")

    def preview(self, proposal: Proposal) -> bool:
        c = self.console
        if proposal.action == "update_settings":
            view = self.runtime.settings_view() | {
                "currency": self.runtime.settings.currency,
                "price_input_per_million": self.runtime.settings.price_input_per_million,
                "price_output_per_million": self.runtime.settings.price_output_per_million,
            }
            c.write(f"  {c.style('Change settings', '1')}")
            for key, value in proposal.data["settings"].items():
                old = view.get(key)
                c.write(f"    {key}: {c.style(shorten(old), '31')} {c.sym('→', '->')} {c.style(shorten(value), '32')}")
            return True
        quest_id = proposal.data["id"]
        existing = self.runtime.rubrics.get(quest_id)
        if proposal.action == "delete_quest":
            if existing is None:
                self.fail(f'There is no quest "{quest_id}".')
                return False
            c.write(f"  {c.style('Delete quest', '1')} {existing.title} ({quest_id}.md)")
            return True
        text = proposal.data["text"].replace("\r\n", "\n").strip() + "\n"
        if existing is None:
            c.write(f"  {c.style('New quest', '1')} {quest_id}.md")
            for line in text.splitlines():
                c.write(f"    {c.style('+', '32')} {line}")
        else:
            c.write(f"  {c.style('Change quest', '1')} {existing.title} ({quest_id}.md)")
            self.show_diff(existing.text, text)
        return True

    def show_diff(self, old: str, new: str) -> None:
        c = self.console
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1))[2:]
        if not diff:
            c.write("    (no changes)")
        for line in diff:
            colour = {"+": "32", "-": "31", "@": "36"}.get(line[:1], "2")
            c.write(f"    {c.style(line, colour)}")

    async def apply(self, proposal: Proposal) -> str:
        if proposal.action == "update_settings":
            settings = dict(proposal.data["settings"])
            if "system_prompt" in settings and not str(settings["system_prompt"]).strip():
                settings["system_prompt"] = DEFAULT_SYSTEM_PROMPT
            await self.runtime.update_settings(settings)
            return "Settings saved."
        if proposal.action == "delete_quest":
            if not self.runtime.rubrics.delete(proposal.data["id"]):
                raise RubricError(f'There is no quest "{proposal.data["id"]}".')
            self.runtime.events.publish({"type": "settings", "status": f"quest deleted: {proposal.data['id']}"})
            return f'Quest "{proposal.data["id"]}" deleted.'
        quest = self.runtime.rubrics.save(proposal.data["id"], proposal.data["text"])
        self.runtime.events.publish({"type": "settings", "status": f"quest saved: {quest.title}"})
        return f'Quest "{quest.title}" saved as {quest.id}.md. Students can choose it with !challenge.'

    # ----- commands -----

    async def cmd_help(self, _: str) -> None:
        c = self.console
        c.write()
        c.write(f"  {c.style('Ask for anything in your own words', '1')}, for example:")
        for example in ("make a quest about building a sustainable house for 10 year olds",
                        "add a criterion about using redstone to the bridge quest",
                        "only answer chat that starts with !ai"):
            c.write(f"    {c.style(example, '36')}")
        c.write()
        c.write(f"  {c.style('Commands', '1')}")
        self.rows([(f"  {cmd}", text) for cmd, text in COMMAND_HELP])
        c.write()

    async def cmd_status(self, _: str) -> None:
        r, c = self.runtime, self.console
        worlds = [conn.remote for conn in r.connections]
        rows = [
            ("Minecraft", c.ok(f"connected ({', '.join(worlds)})") if worlds else c.warn(f"not connected, port {r.server.port}")),
            ("AI", c.ok(r.model_label) if r.model_label else c.warn("not connected")),
            ("Building", r.bridge.building or "nothing"),
        ]
        sessions = r.bridge.assessments.overview() if r.bridge.assessments else []
        rows.append(("Quests", ", ".join(f"{s['player']}: {s['rubric'] or 'choosing'} ({s['state']})" for s in sessions)
                     or f"{len(r.rubrics.list())} available, none in progress"))
        self.rows(rows)

    async def cmd_settings(self, _: str) -> None:
        r, c = self.runtime, self.console
        view = r.settings_view()
        prompt = "default" if view["system_prompt"] == DEFAULT_SYSTEM_PROMPT else f"custom: {shorten(view['system_prompt'], 60)}"
        self.rows([
            ("Trigger", f'"{view["trigger"]}"' if view["trigger"] else "none (answers all chat)"),
            ("Replies", "only to the player who asked" if view["reply_private"] else "to everyone"),
            ("History", f"{view['max_history']} messages per player"),
            ("Prompt", prompt),
            ("Endpoint", view["azure_endpoint"] or c.warn("not set")),
            ("Model", view["azure_model"] or c.warn("not set")),
            ("API key", c.ok("set") if view["api_key_set"] else c.warn("not set")),
            ("Prices", f"{r.settings.currency}{r.settings.price_input_per_million:g} in, "
                       f"{r.settings.currency}{r.settings.price_output_per_million:g} out, per million tokens"),
        ])

    async def cmd_set(self, rest: str) -> None:
        name, _, value = rest.partition(" ")
        name, value = name.lower(), value.strip()
        if not name:
            self.console.write(f"  {self.console.style('Settings you can change with /set', '1')}")
            self.rows([(f"  {usage}", text) for usage, text in SET_HELP])
            return
        if name == "key":
            value = (await self.ask("Azure API key (hidden): ", True)).strip()
            if not value:
                self.console.info("Not changed.")
                return
        elif not value and name not in ("trigger", "prompt"):
            raise SettingsError(f"Give a value, e.g. /set {name} ...  (type /set to see them all)")
        updates: dict[str, Any]
        if name == "trigger":
            updates = {"trigger": "" if value.lower() in ("", "off", "none") else value}
        elif name == "private":
            updates = {"reply_private": on_off(value)}
        elif name == "history":
            updates = {"max_history": value}
        elif name == "prompt":
            updates = {"system_prompt": "" if value.lower() in ("", "default") else value}
        elif name in ("endpoint", "model", "key"):
            updates = {{"endpoint": "azure_endpoint", "model": "azure_model", "key": "api_key"}[name]: value}
            self.console.info("Testing the connection...")
        elif name == "currency":
            updates = {"currency": value}
        elif name in ("price-in", "price-out"):
            updates = {"price_input_per_million" if name == "price-in" else "price_output_per_million": value}
        else:
            raise SettingsError(f"There's no setting called {name}. Type /set to see them.")
        await self.runtime.update_settings(updates)
        self.console.info("Saved.", tone="32")

    async def cmd_quests(self, _: str) -> None:
        quests = self.runtime.rubrics.list()
        if not quests:
            self.console.info("No quests yet. Type /new, or ask the assistant to write one.")
            return
        width = max(len(q.id) for q in quests) + 3
        for quest in quests:
            self.console.write(f"  {self.console.style(f'{quest.id:<{width}}', '36')}{quest.title}")

    def _quest(self, quest_id: str):
        if not quest_id:
            raise RubricError("Which quest? Type /quests to see them.")
        quest = self.runtime.rubrics.get(quest_id)
        if quest is None:
            raise RubricError(f'There is no quest "{quest_id}". Type /quests to see them.')
        return quest

    async def cmd_show(self, quest_id: str) -> None:
        quest = self._quest(quest_id)
        self.console.write()
        for line in quest.text.rstrip().splitlines():
            tone = "1" if line.startswith("#") else ""
            self.console.write(f"  {self.console.style(line, tone)}")
        self.console.write()

    async def _edit_and_save(self, quest_id: str, text: str) -> None:
        with self.console.hold():
            edited = await self._edit(text)
        if edited is None:
            self.fail("No text editor found. Set the EDITOR environment variable, or ask the assistant to write the quest.")
            return
        if edited.strip() == text.strip():
            self.console.info("No changes.")
            return
        quest = self.runtime.rubrics.save(quest_id, edited)
        self.runtime.events.publish({"type": "settings", "status": f"quest saved: {quest.title}"})

    async def cmd_new(self, quest_id: str) -> None:
        quest_id = quest_id or (await self.ask("File name for the quest (e.g. build_a_castle): ")).strip()
        if not quest_id:
            return
        if self.runtime.rubrics.get(quest_id):
            raise RubricError(f'There is already a quest "{quest_id}". Use /edit {quest_id} to change it.')
        if not RUBRIC_ID.match(quest_id):  # check the name before opening the editor
            raise RubricError("Quest names may only use lowercase letters, numbers, - and _.")
        await self._edit_and_save(quest_id, QUEST_TEMPLATE)

    async def cmd_edit(self, quest_id: str) -> None:
        quest = self._quest(quest_id)
        await self._edit_and_save(quest.id, quest.text)

    async def cmd_delete(self, quest_id: str) -> None:
        quest = self._quest(quest_id)
        if await self.confirm(f'Delete the quest "{quest.title}" ({quest.id}.md)?'):
            await self.apply(Proposal("delete_quest", {"id": quest.id}))
        else:
            self.console.info("Not deleted.")

    async def cmd_players(self, _: str) -> None:
        players = await self.runtime.players()
        if not players:
            self.console.info("No players yet.")
            return
        for p in players:
            state = self.console.ok("online") if p["online"] else self.console.style("offline", "2")
            self.console.write(f"  {p['name']:<20} {state}   {p['messages']} messages")

    async def cmd_say(self, text: str) -> None:
        if not text:
            raise SettingsError("Type a message, e.g. /say Five minutes left!")
        if not self.runtime.connections:
            self.fail("Minecraft isn't connected.")
            return
        for conn in list(self.runtime.connections):
            await conn.send_chat(text, prefix="§b[Teacher]§r ")
        self.console.info(f"Sent to Minecraft: {text}")

    async def cmd_clear(self, _: str) -> None:
        self.assistant.clear()
        self.console.info("Started a new conversation with the assistant.")


def shorten(value: Any, limit: int = 50) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else f'"{value}"'
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + '..."'
