from __future__ import annotations

import json
import re
import subprocess

import pytest

import rlm_ig as rlm
from rlm_ig.models.scripted import Scripted
from rlm_ig.session import STATE_VERSION

from helpers import block, run_async


def task_of(turn) -> str:
    return re.search(r"## Task\n\n(.*?)(?:\n\n|$)", turn.first, re.S).group(1).strip()


def by_task(programs: dict[str, str]) -> Scripted:
    """A model that runs the program registered for the task it was given, then stops."""

    def script(turn):
        return block(programs[task_of(turn)])

    return Scripted(script)


@pytest.fixture
def make(cfg):
    """Build sessions over a task-keyed scripted model, closing them at the end."""
    opened = []

    def _make(programs, **kwargs):
        model = by_task(programs)
        session = rlm.Session(model=model, config=cfg, **kwargs)
        opened.append(session)
        return session, model

    yield _make
    for session in opened:
        session.close()


def worker_count() -> int:
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    return sum("rlm-worker" in line for line in out.splitlines())


# ---------------------------------------------------------------- persistence


def test_variables_persist_across_runs(make):
    session, _ = make({"set": "counter = 41\nsubmit('set')", "read": "submit(counter + 1)"})
    session.run("set")
    assert session.run("read").answer == 42


def test_functions_persist_across_runs(make):
    session, _ = make(
        {
            "define": "def double(x):\n    return x * 2\n\nsubmit('ok')",
            "use": "submit(double(21))",
        }
    )
    session.run("define")
    assert session.run("use").answer == 42


def test_each_run_starts_a_fresh_conversation(make):
    session, model = make({"a": "submit(1)", "b": "submit(2)"})
    session.run("a")
    session.run("b")
    assert [t.index for t in model.turns] == [0, 0]


def test_first_run_has_no_session_notes(make):
    session, model = make({"a": "submit(1)"})
    session.run("a")
    assert "Earlier in this session" not in model.turns[0].first


def test_second_run_gets_earlier_task_answer_and_variable_inventory(make):
    session, model = make({"load data": "rows = [1, 2, 3]\nsubmit('loaded 3 rows')", "use data": "submit(sum(rows))"})
    session.run("load data")
    session.run("use data")
    first = model.turns[1].first
    assert "## Earlier in this session" in first
    assert "Earlier tasks and their answers:" in first
    assert "1. load data" in first and "-> loaded 3 rows" in first
    assert "Variables and functions defined so far:" in first
    assert re.search(r"rows \(list\): \[1, 2, 3\]", first)
    assert "## Task\n\nuse data" in first


def test_notes_number_every_earlier_task(make):
    session, model = make({"one": "n = 1\nsubmit('a1')", "two": "submit('a2')", "three": "submit('a3')"})
    for task in ("one", "two", "three"):
        session.run(task)
    first = model.turns[2].first
    assert "1. one" in first and "-> a1" in first and "2. two" in first and "-> a2" in first


def test_history_records_task_status_and_answer(make):
    session, _ = make({"a": "submit({'k': 1})"})
    session.run("a")
    assert session.history == [{"task": "a", "status": "ok", "answer": '{"k": 1}'}]


def test_a_failed_run_is_recorded_without_an_answer(make, cfg):
    session = rlm.Session(model=Scripted([block("x = 1")] * 3), config=cfg.replace(max_steps=2))
    try:
        result = session.run("never submits")
    finally:
        session.close()
    assert result.status == "failed"
    assert session.history == [{"task": "never submits", "status": "failed", "answer": None}]


def test_notes_show_a_marker_for_an_earlier_failed_task(cfg):
    def script(turn):
        if task_of(turn) == "fails":
            return block("x = 1")
        return block("submit('fine')")

    model = Scripted(script)
    with rlm.Session(model=model, config=cfg.replace(max_steps=1)) as session:
        session.run("fails")
        session.run("fine")
    assert "-> (failed)" in model.turns[-1].first


def test_older_history_is_trimmed_in_the_notes(make, monkeypatch):
    from rlm_ig import session as session_module

    monkeypatch.setattr(session_module, "HISTORY_SHOWN", 2)
    session, model = make({f"task {i}": f"submit({i})" for i in range(4)})
    for i in range(4):
        session.run(f"task {i}")
    first = model.turns[3].first
    assert "(1 older task(s) not shown)" in first
    assert "1. task 0" not in first and "2. task 1" in first and "3. task 2" in first


def test_variables_lists_name_type_and_preview(make):
    session, _ = make({"set": "name = 'hello'\nitems = [1, 2]\nsubmit(0)"})
    session.run("set")
    rows = {row["name"]: row for row in session.variables()}
    assert rows["name"]["type"] == "str" and "hello" in rows["name"]["preview"]
    assert rows["items"]["type"] == "list"


def test_variables_is_empty_for_a_fresh_session(make):
    session, _ = make({})
    assert session.variables() == []


# -------------------------------------------------------------------- context


def test_context_given_on_the_first_run_is_visible(make):
    session, _ = make({"len": "submit(len(context))"})
    assert session.run("len", context="abcde").answer == 5


def test_omitting_context_keeps_the_previous_one(make):
    session, _ = make({"first": "submit(context)", "second": "submit(context)"})
    session.run("first", context=[1, 2, 3])
    assert session.run("second").answer == [1, 2, 3]


def test_passing_a_new_context_replaces_the_old_one(make):
    session, _ = make({"first": "submit(context)", "second": "submit(context)", "third": "submit(context)"})
    session.run("first", context="old")
    assert session.run("second", context="new").answer == "new"
    assert session.run("third").answer == "new"


def test_new_context_is_described_in_the_first_message(make):
    session, model = make({"first": "submit(1)", "second": "submit(2)"})
    session.run("first", context="x" * 10)
    session.run("second", context={"a": 1, "b": 2})
    assert "context: str" in model.turns[0].first
    assert "context: dict, 2 keys" in model.turns[1].first


# -------------------------------------------------------------------- on disk


def test_path_saves_after_each_run(make, tmp_path):
    path = tmp_path / "state" / "s.json"
    session, _ = make({"a": "x = 1\nsubmit('a')", "b": "y = 2\nsubmit('b')"}, path=path)
    session.run("a")
    saved = json.loads(path.read_text())
    assert saved["version"] == STATE_VERSION and [h["task"] for h in saved["history"]] == ["a"]
    session.run("b")
    assert [h["task"] for h in json.loads(path.read_text())["history"]] == ["a", "b"]
    assert not (tmp_path / "state" / "s.json.tmp").exists()


def test_no_file_is_written_without_a_path(make, tmp_path, monkeypatch):
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    session, _ = make({"a": "submit(1)"})
    session.run("a")
    assert list(here.iterdir()) == []


def test_a_new_session_on_the_same_path_restores_variables_functions_and_history(make, tmp_path):
    path = tmp_path / "s.json"
    first, _ = make(
        {
            "build": "table = {'x': 10}\ndef scale(v):\n    return v * table['x']\n\nsubmit('built')",
        },
        path=path,
    )
    first.run("build")
    first.close()

    second, model = make({"reuse": "submit([scale(4), table])"}, path=path)
    assert second.run("reuse").answer == [40, {"x": 10}]
    assert [h["task"] for h in second.history] == ["build", "reuse"]
    notes = model.turns[0].first
    assert "1. build" in notes and "-> built" in notes
    assert "table (dict)" in notes and "scale" in notes


def test_the_restored_context_is_still_there(make, tmp_path):
    path = tmp_path / "s.json"
    first, _ = make({"a": "submit(1)"}, path=path)
    first.run("a", context={"k": "v"})
    first.close()
    second, _ = make({"b": "submit(context)"}, path=path)
    assert second.run("b").answer == {"k": "v"}


def test_save_can_be_called_explicitly(make, tmp_path):
    path = tmp_path / "s.json"
    session, _ = make({"a": "submit(1)"}, path=path)
    session.run("a")
    path.unlink()
    session.save()
    assert path.is_file()


def test_save_without_a_path_is_a_no_op(make):
    session, _ = make({"a": "submit(1)"})
    session.run("a")
    session.save()  # must not raise


UNPICKLABLE = "import threading\nlock = threading.Lock()\nkeep = 5\nsubmit('ok')"


def test_unpicklable_values_are_reported_as_skipped_when_saving(make, tmp_path):
    path = tmp_path / "s.json"
    session, _ = make({"a": UNPICKLABLE}, path=path)
    session.run("a")
    assert "lock" in session.skipped and "keep" not in session.skipped
    assert "lock" in json.loads(path.read_text())["state"]["skipped"]


def test_skipped_values_are_dropped_on_restore_and_mentioned_in_the_notes(make, tmp_path):
    path = tmp_path / "s.json"
    first, _ = make({"a": UNPICKLABLE}, path=path)
    first.run("a")
    first.close()
    second, model = make({"b": "submit(keep)"}, path=path)
    assert [row["name"] for row in second.variables()] == ["keep"]  # opens the session and restores it
    assert "lock" in second.skipped
    assert second.run("b").answer == 5
    assert "Not carried over from the saved session: lock" in model.turns[0].first


def test_a_snapshot_over_the_size_limit_skips_the_big_value(make, tmp_path):
    path = tmp_path / "s.json"
    session, _ = make({"a": "big = 'x' * 100000\nsmall = 1\nsubmit('ok')"}, path=path, max_snapshot_bytes=1000)
    session.run("a")
    assert "big" in session.skipped and "small" not in session.skipped


def test_corrupt_session_file_raises_rlm_error(make, tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json at all")
    session, _ = make({"a": "submit(1)"}, path=path)
    with pytest.raises(rlm.RlmError, match="could not read the session file"):
        session.run("a")


def test_wrong_version_session_file_raises_rlm_error(make, tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"version": STATE_VERSION + 1, "history": [], "state": {}}))
    session, _ = make({"a": "submit(1)"}, path=path)
    with pytest.raises(rlm.RlmError, match="incompatible version"):
        session.run("a")


def test_session_file_without_a_version_raises_rlm_error(make, tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{}")
    session, _ = make({"a": "submit(1)"}, path=path)
    with pytest.raises(rlm.RlmError, match="incompatible version"):
        session.run("a")


def test_a_corrupt_file_is_left_untouched(make, tmp_path):
    path = tmp_path / "s.json"
    path.write_text("garbage")
    session, _ = make({"a": "submit(1)"}, path=path)
    with pytest.raises(rlm.RlmError):
        session.run("a")
    assert path.read_text() == "garbage"


# ---------------------------------------------------------------------- async


def test_async_with_and_arun(cfg):
    async def go():
        model = by_task({"set": "v = 3\nsubmit('set')", "read": "submit(v * 2)"})
        async with rlm.Session(model=model, config=cfg) as session:
            await session.arun("set")
            result = await session.arun("read")
            rows = await session.avariables()
        return result.answer, [row["name"] for row in rows]

    answer, names = run_async(go())
    assert answer == 6 and names == ["v"]


def test_async_runs_on_one_session_are_serialised(cfg):
    async def go():
        import asyncio

        model = by_task({"a": "n = 1\nsubmit('a')", "b": "n += 1\nsubmit(n)"})
        async with rlm.Session(model=model, config=cfg) as session:
            first, second = await asyncio.gather(session.arun("a"), session.arun("b"))
        return first.answer, second.answer

    assert run_async(go()) == ("a", 2)


def test_async_save_and_restore(cfg, tmp_path):
    path = tmp_path / "s.json"

    async def go():
        async with rlm.Session(model=by_task({"a": "z = 7\nsubmit(1)"}), config=cfg, path=path) as session:
            await session.arun("a")
        async with rlm.Session(model=by_task({"b": "submit(z)"}), config=cfg, path=path) as session:
            return (await session.arun("b")).answer

    assert run_async(go()) == 7


def test_sync_run_inside_a_running_loop_raises_rlm_error(cfg):
    async def go():
        session = rlm.Session(model=by_task({"a": "submit(1)"}), config=cfg)
        with pytest.raises(rlm.RlmError, match="a-prefixed"):
            session.run("a")
        with pytest.raises(rlm.RlmError, match="blocks"):
            session.variables()
        await session.aclose()

    run_async(go())


def test_rejected_sync_call_leaves_no_unawaited_coroutine_warning(cfg, recwarn):
    async def go():
        session = rlm.Session(model=by_task({"a": "submit(1)"}), config=cfg)
        with pytest.raises(rlm.RlmError):
            session.run("a")

    run_async(go())
    assert not [w for w in recwarn if "never awaited" in str(w.message)]


# -------------------------------------------------------------------- cleanup


def test_no_worker_processes_are_left_after_close(cfg):
    before = worker_count()
    session = rlm.Session(model=by_task({"a": "submit(1)"}), config=cfg)
    session.run("a")
    assert worker_count() == before + 1  # the session's REPL stays alive between runs
    session.close()
    assert worker_count() == before


def test_context_manager_closes_the_worker(cfg):
    before = worker_count()
    with rlm.Session(model=by_task({"a": "submit(1)"}), config=cfg) as session:
        session.run("a")
    assert worker_count() == before


def test_async_context_manager_closes_the_worker(cfg):
    before = worker_count()

    async def go():
        async with rlm.Session(model=by_task({"a": "submit(1)"}), config=cfg) as session:
            await session.arun("a")

    run_async(go())
    assert worker_count() == before


def test_close_before_any_run_is_harmless(cfg):
    session = rlm.Session(model=by_task({}), config=cfg)
    session.close()
    session.close()


def test_closing_twice_after_a_run_is_harmless(cfg):
    session = rlm.Session(model=by_task({"a": "submit(1)"}), config=cfg)
    session.run("a")
    session.close()
    session.close()


def test_a_closed_session_can_be_used_again(cfg, tmp_path):
    path = tmp_path / "s.json"
    session = rlm.Session(model=by_task({"a": "w = 1\nsubmit(1)", "b": "submit(w)"}), config=cfg, path=path)
    session.run("a")
    session.close()
    assert session.run("b").answer == 1  # a fresh REPL, restored from the file
    session.close()
