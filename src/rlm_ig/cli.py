"""The ``rlm`` command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, procs
from .api import Engine, Result
from .config import PROJECT_FILE, Config, user_config_path
from .errors import RlmError
from .loaders import load_many
from .models import detect, resolve
from .trace import latest_trace, read_trace
from .trace.report import outline, render_html

COMMANDS = {"run", "doctor", "trace", "demo", "version"}

EPILOG = """\
examples:
  rlm "Which customers churned twice?" -f events.log
  cat server.log | rlm "Summarise the outage"
  rlm "List every API endpoint and its auth requirement" -f ./src --model claude-code:sonnet
  rlm "Extract all invoices" -f scans/ --schema schema.json --json > invoices.json
  rlm doctor            check what is installed and which model would be used
  rlm trace             show the most recent run step by step
  rlm demo              watch a complete run offline, no model needed
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rlm",
        description="Answer a question over data of any size by letting a model explore it through a Python REPL.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"rlm-ig {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="solve a task (the default command)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    run.add_argument("task", help="what to find out or produce")
    run.add_argument(
        "-f",
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="data to explore: a file or directory; repeat for several (default: stdin if piped)",
    )
    run.add_argument("-m", "--model", help="root model, provider:model (default: first available backend)")
    run.add_argument("-s", "--sub-model", help="model for ask() calls and sub-agents (default: same as --model)")
    run.add_argument("--schema", metavar="FILE|JSON", help="JSON Schema the answer must satisfy")
    run.add_argument("--session", metavar="NAME", help="keep the REPL's variables between runs under this name")
    run.add_argument("--json", action="store_true", help="print the full result (answer, status, usage) as JSON")
    run.add_argument("-o", "--output", metavar="FILE", help="also write the answer to FILE")
    run.add_argument("--max-usd", type=float, metavar="N", help="spend ceiling in USD")
    run.add_argument("--max-calls", type=int, metavar="N", help="ceiling on model calls")
    run.add_argument("--max-steps", type=int, metavar="N", help="turns per agent")
    run.add_argument("--max-depth", type=int, metavar="N", help="levels of sub-agents")
    run.add_argument("--timeout", type=float, metavar="SECONDS", help="wall-clock limit for the run")
    run.add_argument("--parallel", type=int, dest="max_parallel", metavar="N", help="simultaneous model requests")
    run.add_argument("--no-trace", action="store_true", help="do not write a trace file")
    run.add_argument(
        "--config", metavar="FILE", help=f"config file (default: {PROJECT_FILE} here or in a parent directory)"
    )
    noise = run.add_mutually_exclusive_group()
    noise.add_argument("-q", "--quiet", action="store_true", help="print only the answer")
    noise.add_argument("-v", "--verbose", action="store_true", help="show the code and output of every step")

    doctor = sub.add_parser("doctor", help="check the installation and available backends")
    doctor.add_argument("--live", action="store_true", help="also send one tiny request to the default model")
    doctor.add_argument("-m", "--model", help="model to check instead of the default")
    doctor.add_argument("--config", metavar="FILE")

    trace = sub.add_parser("trace", help="show a past run")
    trace.add_argument("path", nargs="?", help="trace file (default: the most recent run)")
    trace.add_argument("--full", action="store_true", help="include the code and output of every step")
    trace.add_argument("--html", metavar="FILE", help="write a standalone HTML report instead")
    trace.add_argument("--list", action="store_true", help="list recent traces")
    trace.add_argument("--config", metavar="FILE")

    demo = sub.add_parser("demo", help="run a complete example offline with a scripted model")
    demo.add_argument("-v", "--verbose", action="store_true", help="show the code and output of every step")

    sub.add_parser("version", help="print the version")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `rlm "question" -f data` is shorthand for `rlm run "question" -f data`.
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "trace":
            return _trace(args)
        if args.command == "demo":
            return _demo(args)
        print(f"rlm-ig {__version__}")
        return 0
    except RlmError as exc:
        print(f"rlm: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nrlm: interrupted", file=sys.stderr)
        return 130


# --------------------------------------------------------------------- run


def _schema_arg(value: str | None) -> Any:
    if not value:
        return None
    text = Path(value).read_text(encoding="utf-8") if os.path.isfile(value) else value
    try:
        return json.loads(text)
    except ValueError as exc:
        raise RlmError(f"--schema is neither a readable file nor valid JSON: {exc}") from exc


def _execute(coro_factory) -> Any:
    """Run a coroutine so that SIGTERM cleans up like Ctrl-C does."""

    async def runner():
        task = asyncio.ensure_future(coro_factory())
        procs.cancel_on_signals(asyncio.get_running_loop(), task)
        return await task

    try:
        return asyncio.run(runner())
    except asyncio.CancelledError:
        raise KeyboardInterrupt from None


def _run(args: argparse.Namespace) -> int:
    context = None
    if args.file:
        context = load_many(args.file)
    elif not sys.stdin.isatty():
        piped = sys.stdin.read()
        context = piped if piped else None
    verbosity = 0 if args.quiet else 2 if args.verbose else 1
    config = Config.load(
        args.config,
        model=args.model,
        sub_model=args.sub_model,
        max_usd=args.max_usd,
        max_calls=args.max_calls,
        max_steps=args.max_steps,
        max_depth=args.max_depth,
        timeout=args.timeout,
        max_parallel=args.max_parallel,
        verbosity=verbosity,
        trace=False if args.no_trace else None,
    )
    schema = _schema_arg(args.schema)

    async def go() -> Result:
        if args.session:
            from .session import Session

            async with Session(config=config, path=_session_path(args.session)) as session:
                return await session.arun(args.task, context, schema=schema)
        engine = Engine(config)
        try:
            return await engine.solve(args.task, context, schema=schema)
        finally:
            await engine.close()

    result = _execute(go)
    return _emit(result, args)


def _session_path(name: str) -> str:
    if os.sep in name or name.endswith(".json"):
        return name
    return str(Path(".rlm") / "sessions" / f"{name}.json")


def _emit(result: Result, args: argparse.Namespace) -> int:
    if args.json:
        text = json.dumps(result.to_dict(), indent=2, ensure_ascii=False, default=str)
    elif result.status == "failed":
        text = ""
    elif isinstance(result.answer, str):
        text = result.answer
    else:
        text = json.dumps(result.answer, indent=2, ensure_ascii=False, default=str)
    if text:
        print(text)
    if args.output and result.status != "failed":
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    if result.status == "failed" and (args.quiet or args.json):
        print(f"rlm: {result.error}", file=sys.stderr)
    return {"ok": 0, "partial": 2}.get(result.status, 1)


# ------------------------------------------------------------------ doctor


def _version_of(command: str) -> str | None:
    path = shutil.which(command)
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0][:60]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "installed (version unknown)"


def _doctor(args: argparse.Namespace) -> int:
    from .sandbox import Sandbox

    ok = True

    def row(mark: str, name: str, detail: str) -> None:
        print(f"  {mark} {name:<14} {detail}")

    print(f"rlm-ig {__version__}")
    print("\nruntime")
    row("+", "python", f"{sys.version.split()[0]} ({sys.executable})")

    async def sandbox_check() -> tuple[float, str]:
        sandbox = Sandbox()
        started = time.monotonic()
        try:
            await sandbox.start("ping")
            result = await sandbox.run("len(context)")
            return time.monotonic() - started, result.stdout.strip()
        finally:
            await sandbox.close()

    try:
        seconds, out = asyncio.run(sandbox_check())
        row("+" if out == "4" else "!", "sandbox", f"REPL worker starts and runs code in {seconds * 1000:.0f} ms")
    except RlmError as exc:
        ok = False
        row("x", "sandbox", str(exc))

    print("\nbackends")
    for name, command in (("claude-code", "claude"), ("opencode", "opencode")):
        version = _version_of(command)
        row("+" if version else "-", name, version or f"`{command}` not found on PATH")
    try:
        import anthropic

        key = (
            "credentials found"
            if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            else "no ANTHROPIC_API_KEY set"
        )
        row("+" if "found" in key else "-", "anthropic", f"SDK {anthropic.__version__}, {key}")
    except ImportError:
        row("-", "anthropic", 'SDK not installed (pip install "rlm-ig[anthropic]")')
    for name, variable in (
        ("openai", "OPENAI_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
    ):
        row(
            "+" if os.environ.get(variable) else "-",
            name,
            f"${variable} {'is set' if os.environ.get(variable) else 'not set'}",
        )
    row(" ", "also", 'ollama (local server) and custom [providers.NAME] entries, including kind = "command"')

    print("\nconfiguration")
    user_file = user_config_path()
    row(
        "+" if user_file.is_file() else "-",
        "user config",
        str(user_file) + ("" if user_file.is_file() else " (not present)"),
    )
    try:
        config = Config.load(args.config, model=args.model)
    except RlmError as exc:
        row("x", "config", str(exc))
        return 1
    spec = config.model or detect()
    if spec is None:
        row("x", "default model", "none: install Claude Code or opencode, or set a model (see `rlm --help`)")
        return 1
    try:
        model = resolve(spec, config)
    except RlmError as exc:
        row("x", "default model", f"{spec}: {exc}")
        return 1
    sub = config.sub_model or spec
    row("+", "default model", f"{model.spec}" + ("" if sub == spec else f"  (sub-model: {sub})"))
    row(
        " ",
        "limits",
        f"max_usd={config.max_usd} max_calls={config.max_calls} max_steps={config.max_steps} max_depth={config.max_depth}",
    )
    row(" ", "traces", config.trace_dir if config.trace else "disabled")

    if args.live:
        print("\nlive check")

        async def ping() -> tuple[str, float, Any]:
            started = time.monotonic()
            try:
                reply = await model.complete("Reply with the single word: ready", "Are you there?")
                return reply.text.strip(), time.monotonic() - started, reply.usage
            finally:
                await model.close()

        try:
            text, seconds, usage = _execute(ping)
            row(
                "+",
                model.spec,
                f"answered {text[:40]!r} in {seconds:.1f}s ({usage.input_tokens} in / {usage.output_tokens} out)",
            )
            info = model.describe()
            extras = ", ".join(f"{k}={v}" for k, v in info.items() if k not in ("spec", "command") and v)
            if extras:
                row(" ", "", extras)
        except RlmError as exc:
            ok = False
            row("x", model.spec, str(exc))
    else:
        print("\n  run `rlm doctor --live` to send one tiny request to the default model")
    return 0 if ok else 1


# ------------------------------------------------------------------- trace


def _trace(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    if args.list:
        folder = Path(config.trace_dir).expanduser()
        files = (
            sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]
            if folder.is_dir()
            else []
        )
        if not files:
            print(f"no traces in {folder}")
            return 0
        for file in files:
            events = read_trace(file)
            end = next((e for e in reversed(events) if e.get("kind") == "run.end"), {})
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(file.stat().st_mtime))
            print(f"{when}  {end.get('status', 'unfinished'):<10} {end.get('seconds', 0):>7.1f}s  {file}")
        return 0
    path = Path(args.path) if args.path else latest_trace(config.trace_dir)
    if path is None:
        raise RlmError(f"no traces found in {config.trace_dir}")
    if not path.is_file():
        raise RlmError(f"no such trace file: {path}")
    events = read_trace(path)
    if not events:
        raise RlmError(f"{path} contains no events")
    if args.html:
        Path(args.html).write_text(render_html(events), encoding="utf-8")
        print(f"wrote {args.html}")
        return 0
    print(f"trace   {path}")
    print(outline(events, full=args.full))
    return 0


# -------------------------------------------------------------------- demo


def _demo(args: argparse.Namespace) -> int:
    from .demo import TASK, demo_model, make_log

    log = make_log()
    print("This demo runs the real engine with a scripted stand-in for the model, so it works offline", file=sys.stderr)
    print(f"and costs nothing. Data: a synthetic service log, {len(log):,} characters.\n", file=sys.stderr)
    config = Config(verbosity=2 if args.verbose else 1, max_usd=None)
    engine = Engine(config.replace(model=demo_model()))

    async def go() -> Result:
        try:
            return await engine.solve(TASK, log)
        finally:
            await engine.close()

    result = _execute(go)
    print(json.dumps(result.answer, indent=2))
    if result.trace:
        print(f"\nReplay it with: rlm trace {result.trace} --full", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
