"""Command-line interface: `skillbuilder [serve]` and `skillbuilder check` (also available as `mcchat`)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import webbrowser
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .config import Settings
from .console import Console, describe_os_error
from .runtime import Runtime, build_llm
from .webapp import start_web

APP_NAME = "Minecraft Quest Builder"
APP_TAGLINE = "For Minecraft Education and Bedrock"


def app_version() -> str:
    try:
        return version("minecraft-skill-builder")
    except PackageNotFoundError:
        return ""


def ai_status(console: Console, runtime: Runtime) -> str:
    if runtime.bridge.llm is None:
        return console.warn("not connected") + "  type !setup in Minecraft, or use Settings on the web page"
    if runtime.mock:
        return console.warn("mock echo") + "  (replies repeat the message, no Azure)"
    return console.ok(runtime.model_label) + "  Azure AI Foundry"


def replies_summary(settings: Settings) -> str:
    who = "only the player who asked" if settings.reply_private else "everyone"
    what = f'chat starting with "{settings.trigger}"' if settings.trigger else "all chat"
    return f"{what}, replies to {who}"


def open_browser(url: str) -> None:
    """Open a URL in the desktop browser, including from WSL (where it's the Windows browser)."""
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            subprocess.Popen(["explorer.exe", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    except OSError:
        pass
    webbrowser.open(url)


def local_ip() -> str | None:
    """Best-effort guess at this machine's (WSL VM's) LAN address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


BUNDLED_RUBRICS = Path(__file__).resolve().parents[2] / "rubrics"


def use_data_dir(data_dir: Path) -> None:
    """Keep settings, rubrics, reports, usage and the map cache in `data_dir` (e.g. persistent storage
    when hosted). The example rubrics are copied there the first time."""
    data_dir.mkdir(parents=True, exist_ok=True)
    rubrics = data_dir / "rubrics"
    if not rubrics.is_dir() and BUNDLED_RUBRICS.is_dir():
        shutil.copytree(BUNDLED_RUBRICS, rubrics)
    os.chdir(data_dir)


async def run_hosted(settings: Settings, mock: bool) -> None:
    """One public web address: teacher pages behind the platform's sign-in, Minecraft at /mc/<join code>."""
    if not settings.join_code:
        sys.exit("JOIN_CODE must be set when HOSTED=true (it stops strangers using your AI).")
    console = Console.for_stream()
    runtime = Runtime.create(settings, mock)
    runtime.events.add_listener(console.event)
    await start_web(runtime, settings.web_host, settings.web_port)
    address = settings.public_url or "https://<this app's address>"
    minecraft = address.replace("https://", "ws://").replace("http://", "ws://") + "/mc/<join code>"
    console.banner(APP_NAME, APP_TAGLINE, app_version(), [
        ("Mode", "hosted"),
        ("Web", f"{address}  (listening on {settings.web_host}:{settings.web_port}, behind sign-in)"),
        ("Minecraft", f"/connect {minecraft}  (the join code is shown on the web page)"),
        ("AI", ai_status(console, runtime)),
    ], footer="Activity is logged below.")
    await asyncio.Event().wait()  # serve until stopped


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


async def run_serve(settings: Settings, mock: bool, web: bool, open_web: bool = False, interactive: bool = False) -> None:
    if settings.hosted:
        await run_hosted(settings, mock)
        return
    console = Console.for_stream()
    runtime = Runtime.create(settings, mock)
    runtime.events.add_listener(console.event)
    await runtime.start()
    port = runtime.server.port
    if web:
        await start_web(runtime, settings.web_host, settings.web_port)

    connect = console.code(f"/connect localhost:{port}")
    ip = local_ip()
    if ip:
        connect += f"\n{console.code(f'/connect {ip}:{port}')}  {console.style('if localhost does not work', '2')}"
    rows = [("Minecraft", connect)]
    if web:
        rows.append(("Web", console.style(f"http://localhost:{settings.web_port}", "4")))
    rows.append(("AI", ai_status(console, runtime)))
    rows.append(("Chat", replies_summary(settings)))
    if interactive:
        footer = "Type /help for commands, or ask for changes in your own words. Ctrl+C stops the server."
    else:
        footer = "In Minecraft, open chat and type the /connect command. Press Ctrl+C to stop."
    console.banner(APP_NAME, APP_TAGLINE, app_version(), rows, footer=footer)
    if web and open_web:
        open_browser(f"http://localhost:{settings.web_port}")

    if not interactive:
        await runtime.server.serve_forever()
        return
    from .teacher import TeacherConsole

    await TeacherConsole(runtime, console).run()
    console.info("Stopped.")
    await runtime.server.close()


async def run_check(settings: Settings, prompt: str) -> None:
    console = Console.for_stream()
    llm = build_llm(settings, mock=False)
    if llm is None:
        missing = ", ".join(settings.missing_azure_settings())
        sys.exit(f"Missing settings: {missing}. Fill in .env (see .env.example) or run `skillbuilder` and use the Settings page.")
    console.write(f"{console.style('Model', '2')}   {settings.azure_model}")
    console.write(f"{console.style('Prompt', '2')}  {prompt}")
    try:
        reply = await llm.complete([{"role": "user", "content": prompt}])
    except Exception as exc:
        console.write(f"{console.style('Result', '2')}  {console.style('request failed', '31')}")
        sys.exit(f"Azure AI Foundry request failed: {exc}")
    console.write(f"{console.style('Reply', '2')}   {reply}")
    console.write(f"{console.style('Result', '2')}  {console.ok('Azure AI Foundry is working')}")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `serve` is the default command: `skillbuilder --open` means `skillbuilder serve --open`.
    if not {"serve", "check", "-h", "--help"} & set(argv):
        global_flags = [a for a in argv if a in ("-v", "--verbose")]
        argv = global_flags + ["serve"] + [a for a in argv if a not in global_flags]

    parser = argparse.ArgumentParser(
        prog="skillbuilder",
        description="Minecraft Quest Builder: an AI companion for Minecraft Education and Bedrock. Runs `serve` by default.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the server Minecraft connects to, and the web interface (default)")
    serve.add_argument("--open", action="store_true", help="open Minecraft Quest Builder in the browser")
    serve.add_argument("--host", help="bind address (default 0.0.0.0, env MC_HOST)")
    serve.add_argument("--port", type=int, help="port (default 3000, env MC_PORT)")
    serve.add_argument("--web-port", type=int, help="Minecraft Quest Builder web UI port (default 8080, env WEB_PORT)")
    serve.add_argument("--no-web", action="store_true", help="don't start the web UI")
    serve.add_argument("--no-console", action="store_true", help="no teacher console prompt in the terminal (just the activity log)")
    serve.add_argument("--trigger", help='only answer messages starting with this, e.g. "!ai" (env MC_TRIGGER)')
    serve.add_argument("--private", action="store_true", help="reply only to the asking player")
    serve.add_argument("--mock", action="store_true", help="echo messages back instead of calling Azure")
    serve.add_argument("--data-dir", help="folder for settings, rubrics, reports and usage (env DATA_DIR; default: current folder)")

    check = sub.add_parser("check", help="send one test prompt to Azure AI Foundry")
    check.add_argument("prompt", nargs="?", default="Say hello in five words.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    data_dir = getattr(args, "data_dir", None) or os.environ.get("DATA_DIR")
    if data_dir:
        use_data_dir(Path(data_dir))
    settings = Settings.from_env()
    try:
        if args.command == "serve":
            if args.host:
                settings.host = args.host
            if args.port:
                settings.port = args.port
            if args.web_port:
                settings.web_port = args.web_port
            if args.trigger is not None:
                settings.trigger = args.trigger
            if args.private:
                settings.reply_private = True
            interactive = not args.no_console and interactive_terminal()
            asyncio.run(run_serve(settings, args.mock, web=not args.no_web, open_web=args.open, interactive=interactive))
        elif args.command == "check":
            asyncio.run(run_check(settings, args.prompt))
    except KeyboardInterrupt:
        console = Console.for_stream()
        console.write()
        console.info("Stopped.")
    except OSError as exc:
        sys.exit(f"Error: {describe_os_error(exc)}")
