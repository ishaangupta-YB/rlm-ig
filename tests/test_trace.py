from __future__ import annotations

import io
import json
import os
import re

import pytest

import rlm_ig as rlm
from rlm_ig.engine.events import Event
from rlm_ig.models.scripted import Scripted
from rlm_ig.trace import TraceWriter, latest_trace, new_trace_path, read_trace, report
from rlm_ig.trace.console import Console, agent_label

from helpers import block

ANSI = re.compile(r"\x1b\[")


def ev(kind, agent="0", depth=0, t=0.0, **data):
    return Event(kind, agent, depth, t, data)


def evd(kind, **data):
    """The same event as the dict a trace file holds."""
    return ev(kind, **data).to_dict()


# ----------------------------------------------------------- a real, rich run


def rich_script(turn):
    """Root: ask_map, a failing cell, a delegate, then submit. The child sums its context."""
    if turn.oneshot:
        return "unused"
    if "Sum the numbers" in turn.first:
        return block("submit(sum(context))", note="Adding them up.")
    return [
        block("rows = ask_map('Upper-case this.', ['a', 'b', 'c'])\nprint(rows)", note="Fan out first."),
        block("print('before boom')\nundefined_name + 1", note="Try something that breaks."),
        block("total = delegate('Sum the numbers.', [1, 2, 3])\nprint('child said', total)", note="Hand off."),
        block("submit({'total': total})", note="Done."),
    ][turn.index]


def shout(prompt):
    """The scripted sub-model: upper-case the ``<data>`` slice it was handed."""
    match = re.search(r"<data>\n(.*)\n</data>", prompt, re.S)
    return match.group(1).upper() if match else "x"


def run_rich(tmp_path, script=rich_script, task="Do the rich thing."):
    config = rlm.Config(verbosity=0, trace_dir=str(tmp_path / "traces"))
    model = Scripted(script, asks=shout)
    result = rlm.run(task, model=model, config=config)
    return result, read_trace(result.trace)


@pytest.fixture(scope="module")
def rich(tmp_path_factory):
    result, events = run_rich(tmp_path_factory.mktemp("rich"))
    assert result.ok, result.error
    return result, events


# ----------------------------------------------------------- writer and reader


def test_writer_and_reader_round_trip(tmp_path):
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path)
    writer(ev("run.start", task="héllo → wörld", model="m"))
    writer(ev("cell", output="line1\nline2", t=1.5))
    writer.close()
    events = read_trace(path)
    assert [e["kind"] for e in events] == ["run.start", "cell"]
    assert events[0]["task"] == "héllo → wörld"  # not escaped to \uXXXX
    assert events[1]["output"] == "line1\nline2" and events[1]["t"] == 1.5
    assert events[0]["agent"] == "0" and events[0]["depth"] == 0


def test_writer_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "a" / "b" / "t.jsonl"
    writer = TraceWriter(path)
    writer(ev("note", message="hi"))
    writer.close()
    assert path.is_file()


def test_writer_writes_nothing_until_the_first_event(tmp_path):
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path)
    assert writer.written is False and not path.exists()
    writer(ev("note", message="hi"))
    assert writer.written is True
    writer.close()


def test_writer_flushes_every_line_so_a_live_reader_sees_events(tmp_path):
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path)
    writer(ev("note", message="one"))
    assert len(read_trace(path)) == 1  # read before close
    writer(ev("note", message="two"))
    assert len(read_trace(path)) == 2
    writer.close()


def test_writer_appends_to_an_existing_file(tmp_path):
    path = tmp_path / "t.jsonl"
    for message in ("first", "second"):
        writer = TraceWriter(path)
        writer(ev("note", message=message))
        writer.close()
    assert [e["message"] for e in read_trace(path)] == ["first", "second"]


def test_writer_serialises_unjsonable_values_as_text(tmp_path):
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path)
    writer(ev("note", message=object))
    writer.close()
    assert "object" in read_trace(path)[0]["message"]


def test_an_unwritable_trace_does_not_raise_and_is_not_reported_written(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("i am a file, not a directory")
    writer = TraceWriter(blocker / "t.jsonl")
    writer(ev("note", message="hi"))
    writer(ev("note", message="again"))
    assert writer.written is False


def test_reader_skips_a_truncated_last_line(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"kind": "run.start", "task": "x"})
        + "\n"
        + json.dumps({"kind": "note"})
        + "\n"
        + '{"kind": "cell", "out'
    )
    assert [e["kind"] for e in read_trace(path)] == ["run.start", "note"]


def test_reader_skips_blank_and_garbage_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('\n{"kind": "a"}\n   \nnot json\n{"kind": "b"}\n')
    assert [e["kind"] for e in read_trace(path)] == ["a", "b"]


def test_reader_accepts_a_string_path(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"kind": "a"}\n')
    assert read_trace(str(path)) == [{"kind": "a"}]


# ------------------------------------------------------------ latest and names


def test_latest_trace_is_none_for_a_missing_or_empty_directory(tmp_path):
    assert latest_trace(str(tmp_path / "nope")) is None
    assert latest_trace(str(tmp_path)) is None


def test_latest_trace_picks_the_newest_by_modification_time(tmp_path):
    older, newer = tmp_path / "zzz-older.jsonl", tmp_path / "aaa-newer.jsonl"
    older.write_text("{}\n")
    newer.write_text("{}\n")
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    assert latest_trace(str(tmp_path)) == newer


def test_latest_trace_ignores_other_files(tmp_path):
    (tmp_path / "notes.txt").write_text("x")
    trace = tmp_path / "a.jsonl"
    trace.write_text("{}\n")
    os.utime(trace, (1_000, 1_000))
    assert latest_trace(str(tmp_path)) == trace


def test_new_trace_path_has_a_slug_of_the_task(tmp_path):
    path = new_trace_path(str(tmp_path), "Which service failed most?!  (March)")
    assert path.parent == tmp_path and path.suffix == ".jsonl"
    assert re.fullmatch(r"\d{8}-\d{6}-which-service-failed-most-march-[0-9a-f]{4}\.jsonl", path.name)


def test_new_trace_path_slug_is_cut_to_40_characters_without_trailing_dash(tmp_path):
    path = new_trace_path(str(tmp_path), "word " * 30)
    slug = path.name.split("-", 2)[2].rsplit("-", 1)[0]
    assert len(slug) <= 40 and not slug.endswith("-") and slug.startswith("word-word")


def test_new_trace_path_falls_back_to_run_for_a_task_with_no_letters(tmp_path):
    assert new_trace_path(str(tmp_path), "???").name.split("-")[2] == "run"


def test_new_trace_paths_do_not_collide_within_a_second(tmp_path):
    assert len({new_trace_path(str(tmp_path), "same task") for _ in range(20)}) > 1


def test_new_trace_path_expands_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert new_trace_path("~/traces", "t").parent == tmp_path / "traces"


# ------------------------------------------------------------------- build


def test_a_run_writes_a_trace_that_starts_and_ends_correctly(rich):
    result, events = rich
    assert events[0]["kind"] == "run.start" and events[-1]["kind"] == "run.end"
    assert events[-1]["status"] == "ok" and events[0]["task"] == "Do the rich thing."


def test_build_finds_run_start_end_and_both_agents(rich):
    _, events = rich
    trace = report.build(events)
    assert trace.start["task"] == "Do the rich thing." and trace.end["status"] == "ok"
    assert set(trace.agents) == {"0", "0.1"}
    assert trace.root is trace.agents["0"]


def test_build_root_has_four_steps_with_code_and_output(rich):
    _, events = rich
    root = report.build(events).root
    assert [s.number for s in root.steps] == [1, 2, 3, 4]
    assert "ask_map" in root.steps[0].code
    assert root.steps[0].output.strip() == "['A', 'B', 'C']"
    assert root.status == "ok" and root.answer is not None


def test_build_records_the_ask_map_as_a_call_on_the_right_step(rich):
    _, events = rich
    root = report.build(events).root
    calls = root.steps[0].calls
    assert len(calls) == 1
    assert calls[0]["op"] == "ask_map" and calls[0]["n"] == 3 and calls[0]["ok"] is True
    assert isinstance(calls[0]["seconds"], float)
    assert root.steps[1].calls == []


def test_build_marks_the_failing_cell(rich):
    _, events = rich
    step = report.build(events).root.steps[1]
    assert step.error is True and step.submitted is False
    assert "NameError" in step.output and "before boom" in step.output


def test_build_nests_the_sub_agent_under_the_step_that_delegated(rich):
    _, events = rich
    trace = report.build(events)
    assert trace.root.steps[2].children == ["0.1"]
    assert [s.children for s in trace.root.steps if s is not trace.root.steps[2]] == [[], [], []]
    child = trace.agents["0.1"]
    assert child.parent == "0" and child.depth == 1 and child.task == "Sum the numbers."
    assert child.status == "ok" and child.answer == "6" and child.context_size > 0
    assert child.steps[0].submitted is True


def test_build_marks_the_final_cell_as_submitted(rich):
    _, events = rich
    last = report.build(events).root.steps[3]
    assert last.submitted is True and last.error is False


def test_build_collects_notes():
    trace = report.build([evd("note", message="heads up"), evd("note", message="again")])
    assert trace.notes == ["heads up", "again"]


def test_build_ignores_events_for_unknown_agents():
    trace = report.build([evd("model.reply", agent="9", step=1, text="x", seconds=0.1)])
    assert trace.agents == {} and trace.root is None


def test_build_marks_a_failed_call():
    events = [
        evd("agent.start", task="t", context_size=0),
        evd("model.reply", step=1, text="x", seconds=0.1),
        evd("call.start", call=1, op="ask", n=1, chars=10),
        evd("call.end", call=1, op="ask", ok=False, error="boom", seconds=0.2, failed=1),
    ]
    call = report.build(events).root.steps[0].calls[0]
    assert call["ok"] is False and call["error"] == "boom" and call["failed"] == 1


# ----------------------------------------------------------------- outline


def test_outline_summarises_the_run(rich):
    _, events = rich
    text = report.outline(events)
    assert "task    Do the rich thing." in text
    assert "model   scripted:scripted" in text
    assert re.search(r"result  ok in [\d.]+s", text)
    assert "2 agent(s), 5 turn(s)" in text


def test_outline_lists_turns_calls_errors_and_the_nested_sub_agent(rich):
    _, events = rich
    lines = report.outline(events).splitlines()
    text = "\n".join(lines)
    assert "turn 1" in text and "Fan out first." in text
    assert "call  ask_map x3" in text
    assert "error: NameError" in text
    sub = next(i for i, line in enumerate(lines) if "sub-agent 1: Sum the numbers." in line)
    turn3 = next(i for i, line in enumerate(lines) if "turn 3" in line)
    turn4 = next(i for i, line in enumerate(lines) if "turn 4" in line)
    assert turn3 < sub < turn4  # nested under step 3, before step 4
    assert lines[sub].startswith("      sub-agent")  # indented below its parent
    assert any(line.strip() == "submitted" for line in lines)


def test_outline_ends_with_the_answer(rich):
    _, events = rich
    assert report.outline(events).rstrip().endswith('answer\n{"total": 6}')


def test_outline_without_full_hides_code_and_output(rich):
    _, events = rich
    text = report.outline(events)
    assert "ask_map('Upper-case" not in text and "before boom" not in text and "['A', 'B', 'C']" not in text


def test_outline_full_includes_code_lines_and_output(rich):
    _, events = rich
    text = report.outline(events, full=True)
    assert "> rows = ask_map('Upper-case this.', ['a', 'b', 'c'])" in text
    assert "> print(rows)" in text
    assert "| ['A', 'B', 'C']" in text
    assert "| before boom" in text and "NameError" in text


def test_outline_full_does_not_print_the_submit_cell_output(rich):
    _, events = rich
    text = report.outline(events, full=True)
    assert "> submit({'total': total})" in text
    assert "| submitted" not in text


def test_outline_of_an_unfinished_trace_says_so(rich):
    _, events = rich
    unfinished = [e for e in events if e["kind"] not in ("run.end", "agent.end")]
    text = report.outline(unfinished)
    assert "trace ends without a result" in text
    assert "turn 1" in text and "answer" not in text.splitlines()[-1]


def test_outline_of_a_trace_cut_mid_run_still_lists_the_steps_so_far(rich):
    _, events = rich
    cut = events[: next(i for i, e in enumerate(events) if e["kind"] == "cell" and e["step"] == 1) + 1]
    text = report.outline(cut)
    assert "turn 1" in text and "turn 2" not in text and "trace ends without a result" in text


def test_outline_of_an_empty_trace_does_not_crash():
    text = report.outline([])
    assert "task    ?" in text and "trace ends without a result" in text


def test_outline_reports_a_failed_run(tmp_path):
    config = rlm.Config(verbosity=0, trace_dir=str(tmp_path), max_steps=1)
    result = rlm.run("t", model=Scripted([block("x = 1")] * 2), config=config)
    assert result.status == "failed"
    text = report.outline(read_trace(result.trace))
    assert "result  failed" in text and "error   " in text and "did not submit" in text


def test_outline_shows_a_reply_without_code(tmp_path):
    config = rlm.Config(verbosity=0, trace_dir=str(tmp_path))
    result = rlm.run("t", model=Scripted(["Just prose.", "More prose."]), config=config)
    assert "(no code block in this reply)" in report.outline(read_trace(result.trace))


# -------------------------------------------------------------------- html


def data_element(page: str) -> str:
    match = re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)
    assert match, "no data element"
    return match.group(1)


def test_html_embeds_parseable_json_with_the_run(rich):
    _, events = rich
    data = json.loads(data_element(report.render_html(events)))
    assert data["start"]["task"] == "Do the rich thing." and data["end"]["status"] == "ok"
    assert set(data["agents"]) == {"0", "0.1"}
    assert data["agents"]["0"]["steps"][2]["children"] == ["0.1"]
    assert data["agents"]["0"]["steps"][1]["error"] is True


def test_html_is_self_contained(rich):
    _, events = rich
    page = report.render_html(events)
    outside_data = page.replace(data_element(page), "")
    assert "http://" not in outside_data and "https://" not in outside_data
    assert not re.search(r"<(link|img|iframe)\b", outside_data)
    assert not re.search(r"<script[^>]+src=", outside_data)
    assert not re.search(r"@import|url\(", outside_data)


def test_html_is_a_complete_document_with_a_title(rich):
    _, events = rich
    page = report.render_html(events)
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</html>")
    assert "<title>rlm trace: Do the rich thing.</title>" in page


def test_html_escapes_the_title(tmp_path):
    _, events = run_rich(tmp_path, task="<b>bold</b> & more")
    page = report.render_html(events)
    assert "<title>rlm trace: &lt;b&gt;bold&lt;/b&gt; &amp; more</title>" in page


def evil_script(turn):
    if turn.oneshot:
        return "unused"
    return [
        block("print('</script><img src=x onerror=alert(1)>')", note="</script> in prose too"),
        block("submit('done </SCRIPT> <!-- <script>')"),
    ][turn.index]


def test_html_escapes_a_closing_script_tag_in_model_output(tmp_path):
    _, events = run_rich(tmp_path, evil_script)
    page = report.render_html(events)
    assert page.lower().count("</script>") == 2  # the data element and the page script, nothing injected
    data = json.loads(data_element(page))  # and it still decodes to the original text
    assert "</script><img src=x onerror=alert(1)>" in data["agents"]["0"]["steps"][0]["output"]
    assert "</script> in prose too" in data["agents"]["0"]["steps"][0]["reply"]
    assert "</SCRIPT>" in data["end"]["answer"]


def test_html_cannot_open_an_html_comment_inside_the_data_element(tmp_path):
    # "<!--" followed by "<script" switches the HTML parser into a state in which the real
    # closing tag no longer ends the element, so the page would break.
    _, events = run_rich(tmp_path, evil_script)
    assert "<!--" not in data_element(report.render_html(events))


def test_html_keeps_non_ascii_text_readable(tmp_path):
    def script(turn):
        return block("submit('naïve → ✓')")

    _, events = run_rich(tmp_path, script)
    assert "naïve → ✓" in data_element(report.render_html(events))


def test_html_of_an_unfinished_trace_still_renders(rich):
    _, events = rich
    unfinished = [e for e in events if e["kind"] not in ("run.end", "agent.end")]
    data = json.loads(data_element(report.render_html(unfinished)))
    assert data["end"] == {} and data["agents"]["0"]["status"] == "running"


def test_html_of_an_empty_trace_still_renders():
    page = report.render_html([])
    assert json.loads(data_element(page))["agents"] == {}


# ----------------------------------------------------------------- console


def run_with_console(tmp_path, verbosity, *, stream=None, color=False, script=rich_script):
    stream = stream if stream is not None else io.StringIO()
    console = Console(verbosity, stream, color=color)
    config = rlm.Config(verbosity=0, trace=False)
    model = Scripted(script, asks=shout)
    rlm.run("Do the rich thing.", model=model, config=config, on_event=console)
    return stream.getvalue() if isinstance(stream, io.StringIO) else None


def utf8_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)


def test_console_verbosity_zero_prints_nothing(tmp_path):
    assert run_with_console(tmp_path, 0) == ""


def test_console_verbosity_one_prints_a_line_per_step_and_a_summary(tmp_path):
    out = run_with_console(tmp_path, 1)
    assert out.startswith("rlm  scripted:scripted")
    assert "turn 1" in out and "Fan out first." in out and "turn 4" in out
    assert "sub-agent" in out and "Sum the numbers." in out
    assert "ask_map x3" in out
    assert "NameError" in out  # the failed cell is summarised
    assert "submitted" in out
    assert out.splitlines()[-1].startswith("* ok  ")


def test_console_verbosity_one_omits_code_and_output(tmp_path):
    out = run_with_console(tmp_path, 1)
    assert "ask_map('Upper-case" not in out and "before boom" not in out and "['A', 'B', 'C']" not in out


def test_console_verbosity_two_adds_code_and_output_blocks(tmp_path):
    out = run_with_console(tmp_path, 2)
    assert "rows = ask_map('Upper-case this.', ['a', 'b', 'c'])" in out
    assert "['A', 'B', 'C']" in out and "before boom" in out
    assert "ask_map done in" in out


def test_console_hides_plain_ask_completion_unless_verbose():
    quiet, loud = io.StringIO(), io.StringIO()
    for stream, level in ((quiet, 1), (loud, 2)):
        console = Console(level, stream, color=False)
        console(ev("call.start", op="ask", n=1, chars=5))
        console(ev("call.end", op="ask", ok=True, seconds=0.1))
    assert "done in" not in quiet.getvalue() and "ask done in" in loud.getvalue()


def test_console_lines_are_prefixed_with_the_agent_and_indented_by_depth(tmp_path):
    lines = run_with_console(tmp_path, 1).splitlines()
    root_turn = next(line for line in lines if "turn 1" in line)
    child_turn = next(line for line in lines if "turn 1" in line and line.startswith("  "))
    assert root_turn.startswith("root")
    assert child_turn.startswith("  1 ")


def test_console_final_line_reports_status_and_usage(tmp_path):
    last = [line for line in run_with_console(tmp_path, 1).splitlines() if line.strip()][-1]
    assert last.startswith("* ok  ")
    assert "call" in last


def test_console_prints_run_notes_even_at_verbosity_one():
    stream = io.StringIO()
    Console(1, stream, color=False)(ev("note", message="watch out"))
    assert stream.getvalue() == "note: watch out\n"


def test_console_shows_errors_from_failed_sub_calls():
    stream = io.StringIO()
    console = Console(1, stream, color=False)
    console(ev("call.start", op="ask", n=1, chars=5))
    console(ev("call.end", op="ask", ok=False, error="model fell over", seconds=0.1))
    assert "x ask: model fell over" in stream.getvalue()


def test_console_hides_tool_calls_unless_verbose():
    quiet, loud = io.StringIO(), io.StringIO()
    for stream, level in ((quiet, 1), (loud, 2)):
        console = Console(level, stream, color=False)
        console(ev("call.start", op="tool", name="lookup", n=1))
        console(ev("call.end", op="tool", ok=True, seconds=0.1))
    assert quiet.getvalue() == "" and "tool" in loud.getvalue() and "lookup" in loud.getvalue()


def test_console_has_no_ansi_escapes_when_color_is_off(tmp_path):
    assert not ANSI.search(run_with_console(tmp_path, 2, color=False))


def test_console_uses_ansi_escapes_when_color_is_on(tmp_path):
    assert ANSI.search(run_with_console(tmp_path, 2, color=True))


def test_console_auto_color_is_off_for_a_stream_that_is_not_a_terminal(tmp_path):
    assert Console(1, io.StringIO()).color is False


def test_console_auto_color_is_on_for_a_terminal_unless_no_color_is_set(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert Console(1, Tty()).color is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert Console(1, Tty()).color is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert Console(1, Tty()).color is False


def test_console_falls_back_to_ascii_when_the_stream_is_not_utf(tmp_path):
    out = run_with_console(tmp_path, 2)  # a StringIO has no encoding
    assert out.isascii()
    assert ">" in out and "ok" in out


def test_console_falls_back_to_ascii_for_an_explicit_ascii_encoding(tmp_path):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", write_through=True)
    run_with_console(tmp_path, 2, stream=stream)
    text = stream.buffer.getvalue().decode("ascii")  # would raise on any non-ASCII byte
    assert "* ok" in text.splitlines()[-1]  # the summary line was not lost to an encoding error


def test_console_uses_unicode_symbols_for_a_utf8_stream(tmp_path):
    stream = utf8_stream()
    run_with_console(tmp_path, 2, stream=stream)
    text = stream.buffer.getvalue().decode("utf-8")
    assert "▸" in text and "✔" in text and "│" in text and "◆" in text


def test_console_survives_a_closed_stream():
    stream = io.StringIO()
    console = Console(1, stream, color=False)
    stream.close()
    console(ev("note", message="nobody is listening"))  # must not raise


def test_agent_label():
    assert agent_label("0") == "root" and agent_label("") == "root"
    assert agent_label("0.2") == "2" and agent_label("0.2.1") == "2.1"
