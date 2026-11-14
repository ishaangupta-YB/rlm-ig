from __future__ import annotations

import io
import json
import sys
import textwrap

import pytest

from rlm_ig import __version__
from rlm_ig.cli import main

# The fake "model" is a small Python program run by the `command` provider. It reads the transcript on
# stdin and prints one reply. `turn` (1, 2, ...) counts the "### USER" messages and `task` is the task
# line of the first message; `reply(code)` prints a reply holding one fenced python block.
PRELUDE = """\
import re, sys
prompt = sys.stdin.read()
turn = prompt.count("### USER")
found = re.search(r"## Task\\n\\n(.*)", prompt)
task = found.group(1).strip() if found else ""
def reply(code, note="Working on it."):
    print(note + "\\n```python\\n" + code + "\\n```")
"""

ECHO = 'reply("submit(context)")'


class Tty(io.StringIO):
    """A stdin that looks like an interactive terminal, so the CLI does not try to read it."""

    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def sandboxed_environment(tmp_path, monkeypatch):
    """Keep every config, state and trace file inside tmp_path, and the process free of RLM_* settings."""
    for name in [n for n in __import__("os").environ if n.startswith("RLM_")]:
        monkeypatch.delenv(name)
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.chdir(work)
    monkeypatch.setattr(sys, "stdin", Tty())
    return work


@pytest.fixture
def make_config(tmp_path):
    """Write a config whose model is a command program; returns the config file path."""

    def _make(body: str = ECHO, *, extra: str = "", model: str | None = "fake:m") -> str:
        script = tmp_path / "fake_model.py"
        script.write_text(PRELUDE + textwrap.dedent(body))
        lines = []
        if model:
            lines.append(f"model = {json.dumps(model)}")
        lines.append(f"trace_dir = {json.dumps(str(tmp_path / 'traces'))}")
        lines.append(extra)
        lines += ["[providers.fake]", 'kind = "command"', f"command = {json.dumps([sys.executable, str(script)])}"]
        path = tmp_path / "rlm.toml"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    return _make


@pytest.fixture
def config(make_config):
    return make_config()


@pytest.fixture
def traces(tmp_path):
    return tmp_path / "traces"


def run_cli(capsys, *argv):
    code = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------- default command, run


def test_bare_task_is_shorthand_for_run(capsys, config, tmp_path):
    data = write(tmp_path / "data.txt", "hello world")
    code, out, _ = run_cli(capsys, "Echo it", "-f", data, "--config", config)
    assert code == 0 and out == "hello world\n"


def test_explicit_run_gives_the_same_result_as_the_shorthand(capsys, config, tmp_path):
    data = write(tmp_path / "data.txt", "hello world")
    shorthand = run_cli(capsys, "Echo it", "-f", data, "--config", config)
    explicit = run_cli(capsys, "run", "Echo it", "-f", data, "--config", config)
    assert shorthand[:2] == explicit[:2] == (0, "hello world\n")


def test_options_may_come_before_the_task_in_the_shorthand(capsys, config, tmp_path):
    # a leading option is not a command, so main() inserts `run` in front of it
    data = write(tmp_path / "data.txt", "x")
    code, out, _ = run_cli(capsys, "-f", data, "--config", config, "--no-trace", "Echo it")
    assert code == 0 and out == "x\n"


def test_no_context_at_all_runs_with_context_none(capsys, make_config):
    config = make_config('reply("submit(repr(context))")')
    code, out, _ = run_cli(capsys, "t", "--config", config)
    assert code == 0 and out == "None\n"


# ---------------------------------------------------------------------- context


def test_file_context_json_is_decoded(capsys, config, tmp_path):
    data = write(tmp_path / "d.json", '{"a": [1, 2]}')
    code, out, _ = run_cli(capsys, "t", "-f", data, "--config", config)
    assert code == 0 and json.loads(out) == {"a": [1, 2]}


def test_several_files_become_a_dict_keyed_by_file_name(capsys, config, tmp_path):
    a = write(tmp_path / "a.txt", "AAA")
    b = write(tmp_path / "b.txt", "BBB")
    code, out, _ = run_cli(capsys, "t", "-f", a, "-f", b, "--config", config)
    assert code == 0 and json.loads(out) == {"a.txt": "AAA", "b.txt": "BBB"}


def test_directory_context(capsys, config, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    write(folder / "one.md", "1")
    code, out, _ = run_cli(capsys, "t", "-f", folder, "--config", config)
    assert code == 0 and json.loads(out) == {"one.md": "1"}


def test_piped_stdin_is_the_context(capsys, config, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("from the pipe\n"))
    code, out, _ = run_cli(capsys, "t", "--config", config)
    assert code == 0 and out == "from the pipe\n\n"


def test_empty_stdin_means_no_context(capsys, make_config, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, out, _ = run_cli(capsys, "t", "--config", make_config('reply("submit(repr(context))")'))
    assert code == 0 and out == "None\n"


def test_files_take_priority_over_stdin(capsys, config, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("ignored"))
    data = write(tmp_path / "d.txt", "file wins")
    code, out, _ = run_cli(capsys, "t", "-f", data, "--config", config)
    assert out == "file wins\n"


def test_missing_input_file_is_an_error_exit_1(capsys, config, tmp_path):
    code, out, err = run_cli(capsys, "t", "-f", tmp_path / "nope.txt", "--config", config)
    assert code == 1 and out == "" and err.startswith("rlm: no such file or directory")


# ------------------------------------------------------------------ json and -o


def test_json_output_has_the_full_result_shape(capsys, config, tmp_path):
    data = write(tmp_path / "d.txt", "payload")
    code, out, _ = run_cli(capsys, "t", "-f", data, "--config", config, "--json")
    result = json.loads(out)
    assert code == 0
    assert set(result) == {
        "answer", "status", "note", "error", "usage", "usage_by_model", "steps", "agents",
        "seconds", "trace", "model", "sub_model",
    }  # fmt: skip
    assert result["answer"] == "payload" and result["status"] == "ok"
    assert result["steps"] == 1 and result["agents"] == 1
    assert result["model"] == "fake:m" and result["note"] is None and result["error"] is None
    assert result["usage"]["calls"] == 1 and "input_tokens" in result["usage"]
    assert result["trace"].endswith(".jsonl")


def test_json_output_keeps_structured_answers_structured(capsys, make_config):
    config = make_config("reply(\"submit({'n': 1, 'tags': ['a', 'b']})\")")
    _, out, _ = run_cli(capsys, "t", "--config", config, "--json")
    assert json.loads(out)["answer"] == {"n": 1, "tags": ["a", "b"]}


def test_non_string_answers_are_printed_as_json(capsys, make_config):
    config = make_config("reply(\"submit({'n': 1, 'tags': ['a', 'b']})\")")
    code, out, _ = run_cli(capsys, "t", "--config", config)
    assert code == 0 and json.loads(out) == {"n": 1, "tags": ["a", "b"]}


def test_non_ascii_answers_are_not_escaped(capsys, make_config):
    config = make_config("reply(\"submit({'name': 'café ✓'})\")")
    _, out, _ = run_cli(capsys, "t", "--config", config)
    assert "café ✓" in out


def test_output_file_receives_the_answer_and_stdout_still_prints_it(capsys, config, tmp_path):
    data = write(tmp_path / "d.txt", "save me")
    target = tmp_path / "out" / "answer.txt"
    target.parent.mkdir()
    code, out, _ = run_cli(capsys, "t", "-f", data, "--config", config, "-o", target)
    assert code == 0 and out == "save me\n"
    assert target.read_text() == "save me\n"


def test_output_file_with_json_holds_the_json(capsys, config, tmp_path):
    data = write(tmp_path / "d.txt", "save me")
    target = tmp_path / "result.json"
    run_cli(capsys, "t", "-f", data, "--config", config, "--json", "-o", target)
    assert json.loads(target.read_text())["answer"] == "save me"


def test_output_file_is_not_written_for_a_failed_run(capsys, make_config, tmp_path):
    config = make_config('reply("x = 1")')
    target = tmp_path / "answer.txt"
    code, _, _ = run_cli(capsys, "t", "--config", config, "--max-steps", 1, "-o", target)
    assert code == 1 and not target.exists()


# ----------------------------------------------------------------------- schema

SCHEMA_PROGRAM = """
if turn == 1:
    reply("submit('seven')")
else:
    reply("submit(7)")
"""


def test_schema_given_inline_is_enforced(capsys, make_config):
    config = make_config(SCHEMA_PROGRAM)
    code, out, _ = run_cli(capsys, "t", "--config", config, "--schema", '{"type": "integer"}', "--json")
    result = json.loads(out)
    assert code == 0 and result["answer"] == 7 and result["steps"] == 2  # the first answer was rejected


def test_schema_given_as_a_file_is_enforced(capsys, make_config, tmp_path):
    schema = write(tmp_path / "schema.json", '{"type": "integer"}')
    code, out, _ = run_cli(capsys, "t", "--config", make_config(SCHEMA_PROGRAM), "--schema", schema)
    assert code == 0 and out == "7\n"


def test_schema_is_shown_to_the_model(capsys, make_config):
    config = make_config('reply("submit(" + repr("## Answer format" in prompt) + ")")')
    code, out, _ = run_cli(capsys, "t", "--config", config, "--schema", '{"type": "boolean"}')
    assert code == 0 and out == "true\n"


def test_bad_schema_exits_1_with_a_message_on_stderr(capsys, config):
    code, out, err = run_cli(capsys, "t", "--config", config, "--schema", "{not json")
    assert code == 1 and out == ""
    assert err.startswith("rlm: --schema is neither a readable file nor valid JSON")


def test_schema_file_with_invalid_json_exits_1(capsys, config, tmp_path):
    schema = write(tmp_path / "schema.json", "{oops")
    code, _, err = run_cli(capsys, "t", "--config", config, "--schema", schema)
    assert code == 1 and "--schema" in err


# --------------------------------------------------------------------- verbosity


def test_default_verbosity_shows_progress_on_stderr_and_only_the_answer_on_stdout(capsys, config, tmp_path):
    data = write(tmp_path / "d.txt", "ans")
    code, out, err = run_cli(capsys, "t", "-f", data, "--config", config)
    assert out == "ans\n"
    assert err.startswith("rlm  fake:m") and "turn 1" in err and "submitted" in err
    assert "trace:" in err


def test_quiet_prints_only_the_answer_and_nothing_on_stderr(capsys, config, tmp_path):
    data = write(tmp_path / "d.txt", "ans")
    code, out, err = run_cli(capsys, "t", "-f", data, "--config", config, "-q")
    assert code == 0 and out == "ans\n" and err == ""


def test_quiet_still_reports_a_failure_on_stderr(capsys, make_config):
    config = make_config('reply("x = 1")')
    code, out, err = run_cli(capsys, "t", "--config", config, "-q", "--max-steps", 1)
    assert code == 1 and out == ""
    assert err.startswith("rlm: ") and "did not submit" in err


def test_verbose_shows_code_and_output_of_each_step(capsys, make_config):
    config = make_config("reply(\"print('visible output')\\nsubmit(1)\")")
    code, out, err = run_cli(capsys, "t", "--config", config, "-v")
    assert out == "1\n"
    assert "print('visible output')" in err and "visible output" in err


def test_quiet_and_verbose_cannot_be_combined(capsys, config):
    with pytest.raises(SystemExit) as info:
        main(["t", "--config", config, "-q", "-v"])
    assert info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


# -------------------------------------------------------------------- exit codes

PARTIAL = """
if turn == 1:
    reply("x = 'from the wrap-up'")
else:
    reply("submit(x)")
"""


def test_exit_code_0_for_a_clean_run(capsys, config):
    assert run_cli(capsys, "t", "--config", config)[0] == 0


def test_exit_code_2_for_a_partial_run_that_still_prints_the_answer(capsys, make_config):
    code, out, err = run_cli(capsys, "t", "--config", make_config(PARTIAL), "--max-steps", 1)
    assert code == 2 and out == "from the wrap-up\n"
    assert "partial" in err


def test_partial_run_json_says_why(capsys, make_config):
    code, out, _ = run_cli(capsys, "t", "--config", make_config(PARTIAL), "--max-steps", 1, "--json")
    result = json.loads(out)
    assert code == 2 and result["status"] == "partial" and "out of turns" in result["note"]


def test_partial_run_writes_the_output_file(capsys, make_config, tmp_path):
    target = tmp_path / "a.txt"
    run_cli(capsys, "t", "--config", make_config(PARTIAL), "--max-steps", 1, "-o", target)
    assert target.read_text() == "from the wrap-up\n"


def test_exit_code_1_for_a_failed_run_with_no_stdout(capsys, make_config):
    code, out, err = run_cli(capsys, "t", "--config", make_config('reply("x = 1")'), "--max-steps", 1)
    assert code == 1 and out == "" and "did not submit" in err


def test_failed_run_with_json_prints_the_result_and_the_error(capsys, make_config):
    code, out, err = run_cli(capsys, "t", "--config", make_config('reply("x = 1")'), "--max-steps", 1, "--json")
    result = json.loads(out)
    assert code == 1 and result["status"] == "failed" and result["answer"] is None
    assert "did not submit" in result["error"] and "did not submit" in err


def test_exit_code_1_when_the_model_program_crashes(capsys, make_config):
    config = make_config("sys.stderr.write('kaput')\nsys.exit(3)")
    code, out, err = run_cli(capsys, "t", "--config", config, "-q")
    assert code == 1 and out == "" and "exited with code 3" in err and "kaput" in err


def test_empty_task_is_rejected_with_exit_1(capsys, config):
    code, _, err = run_cli(capsys, "   ", "--config", config)
    assert code == 1 and "task must be a non-empty string" in err


def test_unknown_provider_exits_1_naming_it(capsys, make_config):
    code, _, err = run_cli(capsys, "t", "--config", make_config(), "-m", "nosuch:model")
    assert code == 1 and "unknown model 'nosuch:model'" in err


def test_missing_config_file_exits_1(capsys, tmp_path):
    code, _, err = run_cli(capsys, "t", "--config", tmp_path / "missing.toml")
    assert code == 1 and "config file not found" in err


def test_no_model_anywhere_exits_1_with_guidance(capsys, make_config, tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    code, out, err = run_cli(capsys, "t", "--config", make_config(model=None))
    assert code == 1 and out == "" and "No model is configured" in err


def test_model_option_overrides_the_config(capsys, make_config):
    config = make_config(model="nosuch:model")
    code, out, _ = run_cli(capsys, "the task", "--config", config, "-m", "fake:other", "--json")
    assert code == 0 and json.loads(out)["model"] == "fake:other"


def test_limit_options_reach_the_run(capsys, make_config):
    config = make_config('reply("x = 1")')
    code, _, err = run_cli(capsys, "t", "--config", config, "-q", "--max-calls", 1)
    assert code == 1 and "call limit reached (1 of 1 model calls)" in err


# ------------------------------------------------------------------ trace files


def test_a_run_writes_a_trace_in_the_configured_directory(capsys, config, traces):
    run_cli(capsys, "t", "--config", config, "-q")
    files = list(traces.glob("*.jsonl"))
    assert len(files) == 1 and files[0].stat().st_size > 0


def test_no_trace_writes_nothing(capsys, config, traces):
    code, out, err = run_cli(capsys, "t", "--config", config, "--no-trace", "--json")
    assert code == 0 and json.loads(out)["trace"] is None
    assert not traces.exists()


# --------------------------------------------------------------------- sessions


SESSION_PROGRAM = """
if "remember" in task:
    reply("counter = 41\\nsubmit('stored')")
else:
    reply("counter += 1\\nsubmit(counter)")
"""


def test_session_creates_a_state_file_under_the_working_directory(capsys, make_config, tmp_path):
    code, out, _ = run_cli(capsys, "remember it", "--config", make_config(SESSION_PROGRAM), "--session", "demo", "-q")
    assert code == 0 and out == "stored\n"
    state = tmp_path / "work" / ".rlm" / "sessions" / "demo.json"
    assert state.is_file()
    assert json.loads(state.read_text())["history"][0]["task"] == "remember it"


def test_a_second_invocation_reuses_the_variables_of_the_first(capsys, make_config):
    config = make_config(SESSION_PROGRAM)
    run_cli(capsys, "remember it", "--config", config, "--session", "demo", "-q")
    code, out, _ = run_cli(capsys, "bump it", "--config", config, "--session", "demo", "-q")
    assert code == 0 and out == "42\n"
    code, out, _ = run_cli(capsys, "bump it", "--config", config, "--session", "demo", "-q")
    assert out == "43\n"


def test_session_directory_comes_from_rlm_session_dir(capsys, make_config, tmp_path, monkeypatch):
    folder = tmp_path / "state" / "sessions"
    monkeypatch.setenv("RLM_SESSION_DIR", str(folder))
    code, out, _ = run_cli(capsys, "remember it", "--config", make_config(SESSION_PROGRAM), "--session", "demo", "-q")
    assert code == 0 and out == "stored\n"
    assert (folder / "demo.json").is_file()
    assert not (tmp_path / "work" / ".rlm").exists()


def test_an_os_error_is_reported_without_a_traceback(capsys, make_config, tmp_path):
    write(tmp_path / "work" / "blocker", "a file where a directory should be")
    code, _, err = run_cli(
        capsys, "remember it", "--config", make_config(SESSION_PROGRAM), "--session", "blocker/s.json"
    )
    assert code == 1 and "Traceback" not in err
    assert err.splitlines()[-1] == "rlm: [Errno 17] File exists: 'blocker'"


def test_run_sandbox_flag_reaches_the_config(capsys, config, monkeypatch):
    seen = {}
    real = __import__("rlm_ig.cli", fromlist=["Engine"]).Engine

    def spy(cfg, *a, **k):
        seen["sandbox"] = cfg.sandbox
        return real(cfg, *a, **k)

    monkeypatch.setattr("rlm_ig.cli.Engine", spy)
    run_cli(capsys, "t", "--config", config, "--sandbox", "no-such-runtime")
    assert "sandbox" not in seen  # an unknown preset is refused before anything runs
    run_cli(capsys, "t", "--config", config, "--sandbox", "podman:img")
    assert seen["sandbox"].prefix()[0] == "podman" and seen["sandbox"].prefix()[-1] == "img"


def test_run_with_an_unknown_sandbox_preset_exits_1(capsys, config):
    code, _, err = run_cli(capsys, "t", "--config", config, "--sandbox", "lxc")
    assert code == 1 and "unknown sandbox preset 'lxc'" in err


def test_sessions_with_different_names_are_independent(capsys, make_config):
    config = make_config(SESSION_PROGRAM)
    run_cli(capsys, "remember it", "--config", config, "--session", "one", "-q")
    code, out, err = run_cli(capsys, "bump it", "--config", config, "--session", "two", "-q", "--max-steps", 1)
    assert code == 1  # `counter` is undefined in the other session, so the run cannot finish


def test_the_second_run_is_told_about_the_session(capsys, make_config):
    config = make_config('reply("submit(" + repr("## Earlier in this session" in prompt) + ")")')
    _, first, _ = run_cli(capsys, "first", "--config", config, "--session", "s", "-q")
    _, second, _ = run_cli(capsys, "second", "--config", config, "--session", "s", "-q")
    assert (first, second) == ("false\n", "true\n")


def test_session_name_with_a_path_separator_is_used_as_the_path(capsys, config, tmp_path):
    target = tmp_path / "elsewhere" / "state.json"
    run_cli(capsys, "t", "--config", config, "--session", target, "-q")
    assert target.is_file()


def test_session_name_ending_in_json_is_used_as_the_path(capsys, config, tmp_path):
    run_cli(capsys, "t", "--config", config, "--session", "mine.json", "-q")
    assert (tmp_path / "work" / "mine.json").is_file()


def test_a_corrupt_session_file_exits_1(capsys, config, tmp_path):
    state = tmp_path / "work" / ".rlm" / "sessions" / "bad.json"
    state.parent.mkdir(parents=True)
    state.write_text("not json")
    code, _, err = run_cli(capsys, "t", "--config", config, "--session", "bad", "-q")
    assert code == 1 and "could not read the session file" in err


# ------------------------------------------------------------------- rlm trace


@pytest.fixture
def one_run(capsys, make_config, tmp_path):
    """A finished run that left a trace behind; returns (config path, trace path)."""
    config = make_config("reply(\"print('step output')\\nsubmit('the answer')\", note=\"Looking now.\")")
    run_cli(capsys, "Find the thing", "--config", config, "-q")
    (trace,) = (tmp_path / "traces").glob("*.jsonl")
    return config, trace


def test_trace_shows_the_latest_run(capsys, one_run):
    config, trace = one_run
    code, out, _ = run_cli(capsys, "trace", "--config", config)
    assert code == 0 and out.startswith(f"trace   {trace}\n")
    assert "task    Find the thing" in out and "model   fake:m" in out
    assert "turn 1" in out and "Looking now." in out and out.rstrip().endswith("answer\nthe answer")


def test_trace_without_full_hides_code_and_output(capsys, one_run):
    config, _ = one_run
    _, out, _ = run_cli(capsys, "trace", "--config", config)
    assert "step output" not in out and "> print" not in out


def test_trace_full_shows_code_and_output(capsys, one_run):
    config, _ = one_run
    code, out, _ = run_cli(capsys, "trace", "--config", config, "--full")
    assert code == 0 and "> print('step output')" in out and "> submit('the answer')" in out


def test_trace_with_an_explicit_path(capsys, one_run, tmp_path):
    config, trace = one_run
    other = tmp_path / "copy.jsonl"
    other.write_text(trace.read_text())
    code, out, _ = run_cli(capsys, "trace", other)  # no config needed: the path says everything
    assert code == 0 and out.startswith(f"trace   {other}\n") and "task    Find the thing" in out


def test_trace_picks_the_most_recent_of_several(capsys, one_run, make_config):
    import os

    config, first = one_run
    os.utime(first, (1_000, 1_000))
    run_cli(capsys, "A newer task", "--config", config, "-q")
    _, out, _ = run_cli(capsys, "trace", "--config", config)
    assert "task    A newer task" in out


def test_trace_list_shows_status_and_path(capsys, one_run):
    config, trace = one_run
    code, out, _ = run_cli(capsys, "trace", "--list", "--config", config)
    assert code == 0 and "ok" in out and str(trace) in out
    assert len(out.strip().splitlines()) == 1


def test_trace_list_marks_unfinished_runs(capsys, one_run, tmp_path):
    config, trace = one_run
    cut = tmp_path / "traces" / "cut.jsonl"
    cut.write_text("".join(trace.read_text().splitlines(keepends=True)[:3]))
    _, out, _ = run_cli(capsys, "trace", "--list", "--config", config)
    assert any("unfinished" in line and str(cut) in line for line in out.splitlines())


def test_trace_list_with_no_traces_says_so_and_succeeds(capsys, config, traces):
    code, out, _ = run_cli(capsys, "trace", "--list", "--config", config)
    assert code == 0 and out == f"no traces in {traces}\n"


def test_trace_html_writes_a_report_file(capsys, one_run, tmp_path):
    config, _ = one_run
    target = tmp_path / "report.html"
    code, out, _ = run_cli(capsys, "trace", "--config", config, "--html", target)
    assert code == 0 and out == f"wrote {target}\n"
    page = target.read_text()
    assert page.startswith("<!doctype html>") and "Find the thing" in page


def test_trace_with_no_traces_is_an_error(capsys, config, traces):
    code, out, err = run_cli(capsys, "trace", "--config", config)
    assert code == 1 and out == "" and err == f"rlm: no traces found in {traces}\n"


def test_trace_of_a_missing_file_is_an_error(capsys, config, tmp_path):
    code, _, err = run_cli(capsys, "trace", tmp_path / "gone.jsonl", "--config", config)
    assert code == 1 and "no such trace file" in err


def test_trace_of_an_empty_file_is_an_error(capsys, config, tmp_path):
    empty = write(tmp_path / "empty.jsonl", "")
    code, _, err = run_cli(capsys, "trace", empty, "--config", config)
    assert code == 1 and "contains no events" in err


# -------------------------------------------------------------------- rlm demo


def test_demo_runs_offline_and_prints_the_planted_answer(capsys):
    code, out, err = run_cli(capsys, "demo")
    answer = json.loads(out)
    assert code == 0
    assert answer["service"] == "billing" and answer["top_reason"] == "gateway_timeout"
    assert answer["failures"] > 0 and answer["top_reason_count"] > 0
    assert "works offline" in err and "sub-agent" in err and "turn 4" in err
    assert "Replay it with: rlm trace" in err


def test_demo_does_not_show_code_unless_verbose(capsys):
    _, _, err = run_cli(capsys, "demo")
    assert "per_service = collections.Counter" not in err


def test_demo_verbose_shows_the_code(capsys):
    code, out, err = run_cli(capsys, "demo", "-v")
    assert code == 0 and "per_service = collections.Counter" in err
    assert json.loads(out)["service"] == "billing"


def test_demo_trace_goes_to_the_state_directory_and_can_be_replayed(capsys, tmp_path):
    _, _, err = run_cli(capsys, "demo")
    path = err.split("Replay it with: rlm trace ")[1].split(" --full")[0]
    assert path.startswith(str(tmp_path / "home" / "state"))
    code, out, _ = run_cli(capsys, "trace", path, "--full")
    assert code == 0 and "sub-agent 1" in out and "service" in out


# ------------------------------------------------------------------ rlm doctor


@pytest.fixture
def bare_path(tmp_path, monkeypatch):
    """A PATH with no `claude` or `opencode`, and no API keys, so doctor output is deterministic."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_doctor_succeeds_and_reports_the_sandbox(capsys, config, bare_path):
    code, out, _ = run_cli(capsys, "doctor", "-m", "fake:m", "--config", config)
    assert code == 0
    assert out.startswith(f"rlm-ig {__version__}")
    sandbox = next(line for line in out.splitlines() if "sandbox" in line)
    assert sandbox.strip().startswith("+") and "REPL worker starts and runs code" in sandbox


def system_tool(name):
    import shutil

    path = shutil.which(name, path="/usr/bin:/bin")
    if path is None:
        pytest.skip(f"needs {name}")
    return path


def sandbox_row(out):
    return next(line for line in out.splitlines() if " sandbox " in line)


def test_doctor_uses_the_configured_sandbox_launcher(capsys, make_config, bare_path):
    launcher = json.dumps([system_tool("env"), "RLM_WRAPPED=yes"])
    config = make_config(extra=f"[sandbox]\nlauncher = {launcher}\npython = {json.dumps(sys.executable)}")
    code, out, _ = run_cli(capsys, "doctor", "--config", config)
    row = sandbox_row(out)
    assert code == 0 and row.strip().startswith("+")
    assert "REPL worker starts and runs code" in row and "(launcher: " in row and "RLM_WRAPPED=yes" in row


def test_doctor_reports_a_broken_sandbox_launcher_and_exits_1(capsys, make_config, bare_path):
    config = make_config(extra=f"[sandbox]\nlauncher = [{json.dumps(system_tool('false'))}]")
    code, out, _ = run_cli(capsys, "doctor", "--config", config)
    row = sandbox_row(out)
    # `false` exits at once, so the host sees either a closed pipe or no ready message, depending on timing
    assert code == 1 and row.strip().startswith("x")
    assert "sandbox failed to start" in row or "sandbox pipe closed" in row
    assert "launcher: " in row
    assert "default model  fake:m" in out  # the rest of the report still runs


def test_doctor_reports_a_missing_launcher_program(capsys, make_config, bare_path):
    config = make_config(extra='[sandbox]\nlauncher = ["no-such-launcher-xyz"]')
    code, out, _ = run_cli(capsys, "doctor", "--config", config)
    assert code == 1 and "could not start the sandbox ('no-such-launcher-xyz')" in sandbox_row(out)


def test_doctor_sandbox_flag_selects_the_preset(capsys, config, bare_path):
    # the empty PATH has no docker, so the preset fails, and the row says which command it tried
    code, out, _ = run_cli(capsys, "doctor", "--config", config, "--sandbox", "docker:img")
    row = sandbox_row(out)
    assert code == 1 and row.strip().startswith("x")
    assert "(launcher: docker run --rm -i --network none" in row and row.endswith("img)")


def test_doctor_honours_the_config_flag_for_the_sandbox(capsys, make_config, bare_path):
    config = make_config(extra='[sandbox]\npreset = "lxc"')
    code, out, _ = run_cli(capsys, "doctor", "--config", config)
    assert code == 1 and "unknown sandbox preset 'lxc'" in out


def test_doctor_reports_the_model_limits_and_traces(capsys, config, bare_path, traces):
    _, out, _ = run_cli(capsys, "doctor", "-m", "fake:m", "--config", config)
    assert "default model  fake:m" in out
    assert "max_usd=5.0 max_calls=300 max_steps=20 max_depth=2" in out
    assert f"traces         {traces}" in out


def test_doctor_uses_the_configured_model_when_none_is_given(capsys, config, bare_path):
    code, out, _ = run_cli(capsys, "doctor", "--config", config)
    assert code == 0 and "default model  fake:m" in out


def test_doctor_lists_the_backends_it_did_not_find(capsys, config, bare_path):
    _, out, _ = run_cli(capsys, "doctor", "--config", config)
    assert "`claude` not found on PATH" in out and "`opencode` not found on PATH" in out
    assert "$OPENAI_API_KEY not set" in out


def test_doctor_shows_the_sub_model_when_it_differs(capsys, make_config, bare_path):
    config = make_config(extra='sub_model = "fake:small"')
    _, out, _ = run_cli(capsys, "doctor", "--config", config)
    assert "fake:m  (sub-model: fake:small)" in out


def test_doctor_without_a_model_exits_1(capsys, make_config, bare_path):
    code, out, _ = run_cli(capsys, "doctor", "--config", make_config(model=None))
    assert code == 1 and "default model" in out and "none: install Claude Code or opencode" in out


def test_doctor_with_an_unknown_model_exits_1(capsys, config, bare_path):
    code, out, _ = run_cli(capsys, "doctor", "-m", "nosuch:x", "--config", config)
    assert code == 1 and "unknown model 'nosuch:x'" in out


def test_doctor_with_a_broken_config_exits_1(capsys, tmp_path, bare_path):
    broken = write(tmp_path / "broken.toml", "max_steps = = 1")
    code, out, _ = run_cli(capsys, "doctor", "--config", broken)
    assert code == 1 and "broken.toml" in out


def test_doctor_live_sends_one_request_to_the_model(capsys, config, bare_path):
    code, out, _ = run_cli(capsys, "doctor", "-m", "fake:m", "--config", config, "--live")
    assert code == 0 and "live check" in out and "answered" in out


def test_doctor_live_failure_exits_1(capsys, make_config, bare_path):
    config = make_config("sys.exit(1)")
    code, out, _ = run_cli(capsys, "doctor", "-m", "fake:m", "--config", config, "--live")
    assert code == 1 and "exited with code 1" in out


def test_doctor_without_live_points_at_the_flag(capsys, config, bare_path):
    _, out, _ = run_cli(capsys, "doctor", "--config", config)
    assert "rlm doctor --live" in out


# ------------------------------------------------------------- entry point


def test_entry_exits_with_mains_code(monkeypatch):
    from rlm_ig import cli

    monkeypatch.setattr(cli, "main", lambda: 2)
    with pytest.raises(SystemExit) as info:
        cli.entry()
    assert info.value.code == 2


def test_entry_leaves_at_once_after_an_interrupt(monkeypatch):
    from rlm_ig import cli

    exits = []
    monkeypatch.setattr(cli, "main", lambda: 130)
    monkeypatch.setattr(cli.os, "_exit", exits.append)
    cli.entry()
    assert exits == [130]


def test_sigterm_during_a_hung_request_exits_promptly(tmp_path):
    """A model request that never answers must not keep the process alive after SIGTERM."""
    import os
    import signal
    import socket
    import subprocess
    import time

    if os.name != "posix":
        pytest.skip("POSIX signals")
    hang = socket.socket()
    hang.bind(("127.0.0.1", 0))
    hang.listen(8)  # accepts connections, never answers
    env = {k: v for k, v in os.environ.items() if not k.startswith("RLM_")}
    env.update(
        OPENAI_API_KEY="x",
        OPENAI_BASE_URL=f"http://127.0.0.1:{hang.getsockname()[1]}/v1",
        HOME=str(tmp_path),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
    )
    code = "from rlm_ig.cli import entry; entry()"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "t", "-m", "openai:x", "--no-trace", "-q"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(3)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
        assert time.monotonic() - started < 5
        assert proc.returncode == 130 and b"interrupted" in err
    finally:
        proc.kill()
        hang.close()


# ------------------------------------------------------------- version and help


def test_version_command(capsys):
    code, out, _ = run_cli(capsys, "version")
    assert code == 0 and out == f"rlm-ig {__version__}\n"


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out == f"rlm-ig {__version__}\n"


def test_no_arguments_prints_help_and_succeeds(capsys):
    code, out, _ = run_cli(capsys)
    assert code == 0
    assert out.startswith("usage: rlm") and "examples:" in out
    for command in ("run", "doctor", "trace", "demo", "version"):
        assert command in out


def test_help_flag_prints_help_and_exits_0(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0 and "usage: rlm" in capsys.readouterr().out


def test_run_help_lists_the_run_options(capsys):
    with pytest.raises(SystemExit):
        main(["run", "--help"])
    out = capsys.readouterr().out
    for option in ("--file", "--schema", "--session", "--json", "--output", "--max-steps", "--quiet", "--no-trace"):
        assert option in out


def test_run_without_a_task_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as info:
        main(["run"])
    assert info.value.code == 2 and "the following arguments are required: task" in capsys.readouterr().err
