"""Command-line interface: `skillbuilder [serve]` and `skillbuilder check` (also available as `mcchat`)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from .bridge import Event
from .config import Settings
from .runtime import Runtime, build_llm
from .webapp import start_web


def log_line(text: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True)


def print_event(event: Event) -> None:
    kind = event["type"]
    if kind == "question":
        log_line(f"<{event['player']}> {event['text']}")
    elif kind == "answer":
        log_line(f"<AI -> {event['player']}> {event['text']}")
    elif kind == "error":
        log_line(f"!! LLM error for {event['player']}: {event['error']}")
    elif kind in ("setup", "build", "assess"):
        log_line(f"[{kind}] {event['player']}: {event['status']}")
    elif kind == "assessment":
        levels = ", ".join(f"{c['name']}: {c['level']}" for c in event["criteria"])
        log_line(f"[assessment] {event['player']} ({event['rubric']}, attempt {event['attempt']}): {levels}")
        if event["report"]:
            log_line(f"[assessment] report saved to {event['report']}")
    elif kind == "connected":
        log_line(f"Minecraft connected from {event['remote']}")
    elif kind == "disconnected":
        log_line(f"Minecraft disconnected ({event['remote']})")
    elif kind == "settings":
        log_line(f"[settings] {event['status']}")
    elif kind == "reset":
        log_line(f"[reset] {event['player']}: conversation cleared")


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


async def run_serve(settings: Settings, mock: bool, web: bool, open_web: bool = False) -> None:
    llm = build_llm(settings, mock)
    runtime = Runtime(settings, llm, mock=mock)
    runtime.events.add_listener(print_event)
    await runtime.start()
    port = runtime.server.port
    if web:
        await start_web(runtime, settings.web_host, settings.web_port)

    log_line(f"Listening on ws://{settings.host}:{port}  (LLM: {runtime.model_label or 'not configured'})")
    if llm is None:
        log_line("No Azure credentials found. Type !setup in Minecraft chat or use the Settings page.")
    if settings.trigger:
        log_line(f'Only answering chat that starts with "{settings.trigger}"')
    if web:
        log_line(f"Minecraft Skill Builder: http://localhost:{settings.web_port}")
    print("\nIn Minecraft Education, open chat and run:")
    print(f"    /connect localhost:{port}")
    ip = local_ip()
    if ip:
        print(f"If that fails, try:\n    /connect {ip}:{port}")
    print("Press Ctrl+C to stop.\n", flush=True)
    if web and open_web:
        open_browser(f"http://localhost:{settings.web_port}")

    await runtime.server.serve_forever()


async def run_check(settings: Settings, prompt: str) -> None:
    llm = build_llm(settings, mock=False)
    if llm is None:
        missing = ", ".join(settings.missing_azure_settings())
        sys.exit(f"Missing settings: {missing}. Fill in .env (see .env.example) or run `mcchat serve` and use !setup in chat.")
    print(f"Asking {settings.azure_model}: {prompt}")
    try:
        reply = await llm.complete([{"role": "user", "content": prompt}])
    except Exception as exc:
        sys.exit(f"Azure AI Foundry request failed: {exc}")
    print(f"Reply: {reply}")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `serve` is the default command: `skillbuilder --open` means `skillbuilder serve --open`.
    if not {"serve", "check", "-h", "--help"} & set(argv):
        global_flags = [a for a in argv if a in ("-v", "--verbose")]
        argv = global_flags + ["serve"] + [a for a in argv if a not in global_flags]

    parser = argparse.ArgumentParser(
        prog="skillbuilder",
        description="Minecraft Skill Builder: an AI companion for Minecraft Education. Runs `serve` by default.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the server Minecraft connects to, and the web interface (default)")
    serve.add_argument("--open", action="store_true", help="open Minecraft Skill Builder in the browser")
    serve.add_argument("--host", help="bind address (default 0.0.0.0, env MC_HOST)")
    serve.add_argument("--port", type=int, help="port (default 3000, env MC_PORT)")
    serve.add_argument("--web-port", type=int, help="Minecraft Skill Builder web UI port (default 8080, env WEB_PORT)")
    serve.add_argument("--no-web", action="store_true", help="don't start the web UI")
    serve.add_argument("--trigger", help='only answer messages starting with this, e.g. "!ai" (env MC_TRIGGER)')
    serve.add_argument("--private", action="store_true", help="reply only to the asking player")
    serve.add_argument("--mock", action="store_true", help="echo messages back instead of calling Azure")

    check = sub.add_parser("check", help="send one test prompt to Azure AI Foundry")
    check.add_argument("prompt", nargs="?", default="Say hello in five words.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

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
            asyncio.run(run_serve(settings, args.mock, web=not args.no_web, open_web=args.open))
        elif args.command == "check":
            asyncio.run(run_check(settings, args.prompt))
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as exc:
        sys.exit(f"Error: {exc}")
