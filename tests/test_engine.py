from __future__ import annotations

import asyncio
import json
import re

import pytest

import rlm_ig as rlm
from rlm_ig.models.scripted import Scripted
from rlm_ig.trace import read_trace

from helpers import block


def data_of(prompt: str) -> str:
    return re.search(r"<data>\n(.*)\n</data>", prompt, re.S).group(1)


# ------------------------------------------------------------------- basics


def test_single_turn_answer(solve):
    result = solve([block("submit(len(context))")], context="abcdef")
    assert result.ok and result.answer == 6
    assert result.steps == 1 and result.agents == 1 and result.usage.calls == 1


def test_first_message_carries_task_data_summary_and_limits(solve):
    model = Scripted([block("submit(1)")])
    solve(model, task="Count the widgets.", context={"widgets": [1, 2, 3]})
    first = model.turns[0].first
    assert "## Task\n\nCount the widgets." in first
    assert "context: dict, 1 keys" in first and "['widgets']" in first
    assert "20 turns at most" in first
    assert model.turns[0].system.startswith("You solve tasks by driving a Python REPL")


def test_output_and_errors_flow_back_to_the_model(solve):
    model = Scripted([block("print('seen-by-model')\n1 / 0"), block("submit('recovered')")])
    result = solve(model)
    assert result.answer == "recovered"
    feedback = model.turns[1].last
    assert "seen-by-model" in feedback and "ZeroDivisionError" in feedback and "[turn 1 of 20" in feedback


def test_long_output_is_clipped_for_the_model(solve, cfg):
    model = Scripted([block("print('x' * 50000)"), block("submit(1)")])
    solve(model, config=cfg.replace(output_limit=1000))
    assert len(model.turns[1].last) < 1400 and "characters omitted" in model.turns[1].last


def test_no_context_is_fine(solve):
    model = Scripted([block("submit(2 + 2)")])
    assert solve(model).answer == 4
    assert "context is None" in model.turns[0].first


# ------------------------------------------------------------- answer format


def test_schema_violation_is_fed_back_then_accepted(solve):
    model = Scripted([block("submit({'n': 'three'})"), block("submit({'n': 3})")])
    result = solve(model, schema={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})
    assert result.answer == {"n": 3} and result.steps == 2
    assert "$.n: expected integer, got string" in model.turns[1].last


def test_schema_from_type_hint_and_parsed_dataclass(solve):
    import dataclasses

    @dataclasses.dataclass
    class Verdict:
        title: str
        score: float

    result = solve([block("submit({'title': 'Up', 'score': 8.5})")], schema=Verdict)
    assert result.answer == {"title": "Up", "score": 8.5}
    assert result.parsed == Verdict("Up", 8.5)
    assert solve([block("submit(['a', 'b'])")], schema=list[str]).answer == ["a", "b"]


def test_prose_reply_gets_one_reminder_then_counts_as_the_answer(solve):
    model = Scripted(["The answer is 42.", "As I said, the answer is 42."])
    result = solve(model)
    assert result.ok and result.answer == "As I said, the answer is 42."
    assert "No code block was found" in model.turns[1].last


def test_prose_is_never_accepted_when_a_schema_is_required(solve, cfg):
    model = Scripted(["It is 42."] * 4)
    result = solve(model, schema=int, config=cfg.replace(max_steps=3))
    assert result.status == "failed" and "StepsExhausted" in result.error


# ------------------------------------------------------------------- limits


def test_out_of_turns_forces_a_final_submit(solve, cfg):
    model = Scripted([block("x = 1"), block("x += 1"), block("submit(x)")])
    result = solve(model, config=cfg.replace(max_steps=2))
    assert result.status == "partial" and result.answer == 2 and "out of turns" in result.note
    assert "You are out of turns" in model.turns[2].last


def test_out_of_turns_without_submit_fails(solve, cfg):
    result = solve([block("x = 1")] * 3, config=cfg.replace(max_steps=2))
    assert result.status == "failed" and "did not submit" in result.error
    with pytest.raises(rlm.RunFailed):
        result.raise_for_status()


def test_call_budget_stops_sub_calls_and_root_wraps_up(solve, cfg):
    script = [
        block(
            "got = []\nfor i in range(10):\n    try:\n        got.append(ask(f'q{i}'))\n    except BudgetExceeded as exc:\n        print('stopped:', exc)\n        break"
        ),
        block("submit(got)"),
    ]
    result = solve(script, asks=lambda p: p.upper(), config=cfg.replace(max_calls=4))
    assert result.status == "partial" and result.answer == ["Q0", "Q1", "Q2"]
    assert "budget spent" in result.note and result.usage.calls == 5


def test_cost_budget_is_enforced(solve, cfg):
    model = Scripted(
        [block("print(ask('a'), ask('b'), ask('c'))"), block("submit('done')")], asks="x", cost_per_call=0.4
    )
    result = solve(model, config=cfg.replace(max_usd=1.0))
    assert result.status == "partial" and "cost limit" in result.note
    assert result.usage.cost_usd == pytest.approx(1.6)


def test_wall_clock_limit(solve, cfg):
    model = Scripted([block("import time\ntime.sleep(0.4)"), block("submit('late')")])
    result = solve(model, config=cfg.replace(timeout=0.2))
    assert result.status == "partial" and "time limit" in result.note


# ---------------------------------------------------------------- sub-calls


def test_ask_returns_text_and_builds_prompt_with_data(solve):
    seen = []
    script = [block("submit(ask('Summarise.', context[:5]))")]
    result = solve(script, context="hello world", asks=lambda p: seen.append(p) or "  a summary  ")
    assert result.answer == "a summary"
    assert seen == ["Summarise.\n\n<data>\nhello\n</data>"]


def test_ask_with_schema_parses_validates_and_repairs(solve):
    replies = iter(["not json", '```json\n{"n": 7}\n```'])
    script = [block("submit(ask('How many?', schema={'type': 'object', 'required': ['n']}))")]
    result = solve(script, asks=lambda p: next(replies))
    assert result.answer == {"n": 7} and result.usage.calls == 3


def test_ask_that_stays_invalid_raises_in_the_repl(solve):
    script = [
        block("try:\n    ask('x', schema={'type': 'integer'})\nexcept SubcallError as exc:\n    print('caught', exc)"),
        block("submit('handled')"),
    ]
    model = Scripted(script, asks="still not json")
    assert solve(model).answer == "handled"
    assert "did not return valid JSON" in model.turns[-1].last


def test_ask_refuses_oversized_input(solve, cfg):
    script = [
        block("try:\n    ask('q', context)\nexcept ValueError as exc:\n    print(exc)"),
        block("submit('ok')"),
    ]
    model = Scripted(script, asks="unused")
    solve(model, context="z" * 5000, config=cfg.replace(ask_limit=1000))
    assert "chunk()" in model.turns[1].last and "5,0" in model.turns[1].last


def test_ask_map_runs_in_parallel_and_keeps_order(solve):
    script = [block("parts = chunk(context, 10)\nsubmit(ask_map('Upper-case this.', parts))")]
    model = Scripted(script, asks=lambda p: data_of(p).upper(), delay=0.1)
    result = solve(model, context="aaaaaaaaaabbbbbbbbbbccccccccccdddddddddd")
    assert result.answer == ["AAAAAAAAAA", "BBBBBBBBBB", "CCCCCCCCCC", "DDDDDDDDDD"]
    assert result.seconds < 0.1 * 4 + 0.15  # one root turn, then four asks at once


def test_ask_map_isolates_failures(solve):
    def asks(prompt):
        if "bad" in prompt:
            raise rlm.ModelError("upstream 500")
        return "fine"

    script = [
        block("out = ask_map(['good one', 'bad one', 'good two'])\nsubmit([str(x) if x else x.error for x in out])")
    ]
    result = solve(script, asks=asks)
    assert result.answer[0] == "fine" and result.answer[2] == "fine"
    assert "upstream 500" in result.answer[1]


def test_ask_map_raises_when_everything_fails(solve):
    def asks(prompt):
        raise rlm.ModelError("backend down")

    script = [
        block("try:\n    ask_map(['a', 'b'])\nexcept SubcallError as exc:\n    print('all failed:', exc)"),
        block("submit('noted')"),
    ]
    model = Scripted(script, asks=asks)
    assert solve(model).answer == "noted"
    assert "all failed: ModelError: backend down" in model.turns[-1].last


def test_parallelism_is_capped(solve, cfg):
    active = peak = 0

    async def slow(prompt):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return "x"

    script = [block("submit(len(ask_map([str(i) for i in range(12)])))")]
    result = solve(Scripted(script, asks=slow), config=cfg.replace(max_parallel=3))
    assert result.answer == 12 and peak == 3


# --------------------------------------------------------------- sub-agents


def sub_agent_script(turn):
    """Root delegates; children answer from their own context."""
    if turn.oneshot:
        return "unused"
    if "Sum the numbers" in turn.first:
        return block("submit(sum(context))")
    return [block("submit(delegate_map('Sum the numbers in context.', [[1, 2], [3, 4], [5, 6]]))")][turn.index]


def test_delegate_map_runs_children_with_their_own_context(solve):
    result = solve(Scripted(sub_agent_script))
    assert result.answer == [3, 7, 11] and result.agents == 4


def test_children_use_the_sub_model(cfg):
    root = Scripted([block("submit(delegate('Report your context.', 'payload'))")], name="root")
    sub = Scripted([block("submit(context.upper())")], name="sub")
    result = rlm.run("t", model=root, sub_model=sub, config=cfg)
    assert result.answer == "PAYLOAD"
    assert result.usage_by_model["scripted:root"].calls == 1 and result.usage_by_model["scripted:sub"].calls == 1
    assert len(root.turns) == 1 and len(sub.turns) == 1


def test_child_schema_is_enforced_inside_the_child(solve):
    def script(turn):
        if "Give a number" in turn.first:
            return [block("submit('seven')"), block("submit(7)")][turn.index]
        return block("submit(delegate('Give a number.', 'x', schema={'type': 'integer'}))")

    model = Scripted(script)
    assert solve(model).answer == 7
    assert any("expected integer, got string" in t.last for t in model.turns)


def test_depth_limit_refuses_further_delegation(solve, cfg):
    def script(turn):
        if "Go deeper" in turn.first:
            return [
                block("try:\n    delegate('Go deeper.', 'x')\nexcept DelegationRefused as exc:\n    print(exc)"),
                block("submit('bottom')"),
            ][turn.index]
        return block("submit(delegate('Go deeper.', 'x'))")

    model = Scripted(script)
    result = solve(model, config=cfg.replace(max_depth=1))
    assert result.answer == "bottom"
    child_first = next(t for t in model.turns if "Go deeper" in t.first).first
    assert "`delegate` is not available here" in child_first
    assert any("not available at this level" in t.last for t in model.turns)


def test_passthrough_guard_blocks_forwarding_the_whole_context(solve):
    def script(turn):
        if "Child task" in turn.first:
            return block("submit(len(context))")
        return [
            block(
                "try:\n    delegate('Child task.', context)\nexcept DelegationRefused as exc:\n    print('refused:', exc)"
            ),
            block("submit([delegate('Child task.', context[:1000]), delegate('Child task.', context, whole=True)])"),
        ][turn.index]

    model = Scripted(script)
    result = solve(model, context="y" * 30000)
    assert result.answer == [1000, 30000]
    assert "refused:" in model.turns[1].last and "100% of your own context" in model.turns[1].last


def test_failing_child_surfaces_as_an_exception_in_the_parent(solve, cfg):
    def script(turn):
        if "Never finishes" in turn.first:
            return block("x = 1")
        return [
            block(
                "try:\n    delegate('Never finishes.', 'x')\nexcept SubcallError as exc:\n    print('child failed:', exc)"
            ),
            block("submit('parent survived')"),
        ][turn.index]

    model = Scripted(script)
    result = solve(model, config=cfg.replace(max_steps=2))
    assert result.answer == "parent survived"
    assert "child failed: StepsExhausted" in model.turns[-1].last


def test_nested_delegation_does_not_deadlock_on_agent_slots(solve, cfg):
    def script(turn):
        if "Level two" in turn.first:
            return block("submit(context * 2)")
        if "Level one" in turn.first:
            return block("submit(sum(delegate_map('Level two.', [context, context + 1])))")
        return block("submit(delegate_map('Level one.', [1, 2, 3, 4]))")

    result = solve(Scripted(script, delay=0.01), config=cfg.replace(max_parallel=2, max_depth=2))
    assert result.answer == [6, 10, 14, 18] and result.agents == 13


# -------------------------------------------------------------------- tools


def test_tools_run_in_the_host_and_are_listed_for_the_model(solve):
    calls = []

    def lookup(key: str, default: int = 0) -> int:
        """Look up a key in the price table."""
        calls.append(key)
        return {"apple": 3}.get(key, default)

    model = Scripted([block("submit([lookup('apple'), lookup('pear', default=9)])")])
    result = solve(model, tools=[lookup])
    assert result.answer == [3, 9] and calls == ["apple", "pear"]
    assert "def lookup(key: str, default: int = 0) -> int" in model.turns[0].first
    assert "Look up a key in the price table." in model.turns[0].first


def test_async_tools_and_tool_errors(solve):
    async def fetch(n: int) -> dict:
        "Fetch a record."
        await asyncio.sleep(0)
        if n < 0:
            raise KeyError("no such record")
        return {"id": n}

    script = [
        block("print(fetch(5))\ntry:\n    fetch(-1)\nexcept ToolError as exc:\n    print('tool error:', exc)"),
        block("submit('ok')"),
    ]
    model = Scripted(script)
    solve(model, tools=[fetch])
    assert "{'id': 5}" in model.turns[1].last and "tool error: fetch failed: KeyError" in model.turns[1].last


def test_tools_can_return_arbitrary_picklable_objects(solve):
    import datetime

    def today() -> datetime.date:
        "The date."
        return datetime.date(2024, 5, 6)

    assert solve([block("submit(today().isoformat())")], tools=[today]).answer == "2024-05-06"


def test_children_inherit_tools_unless_restricted(solve, cfg):
    def ping() -> str:
        "Ping."
        return "pong"

    def script(turn):
        if "Try the tool" in turn.first:
            return block("try:\n    submit(ping())\nexcept NameError:\n    submit('no tool')")
        return block(
            "submit([delegate('Try the tool.', 'x'), delegate('Try the tool.', 'x', tools=[]), "
            "delegate('Try the tool.', 'x', tools=[ping])])"
        )

    assert solve(Scripted(script), tools=[ping]).answer == ["pong", "no tool", "pong"]
    assert solve(Scripted(script), tools=[ping], config=cfg.replace(inherit_tools=False)).answer == [
        "no tool",
        "no tool",
        "pong",
    ]


def test_repl_defined_functions_can_be_handed_to_children(solve):
    def script(turn):
        if "Use the helper" in turn.first:
            return block("submit(shout(context))")
        return block(
            "def shout(s):\n    return s.upper() + '!'\n\nsubmit(delegate('Use the helper.', 'hey', tools=[shout]))"
        )

    assert solve(Scripted(script)).answer == "HEY!"


def test_reserved_tool_names_are_rejected(cfg):
    def ask():
        "Shadows a builtin."

    with pytest.raises(rlm.ConfigError, match="collides"):
        rlm.run("t", model=Scripted([block("submit(1)")]), tools=[ask], config=cfg)


# ------------------------------------------------------- events and tracing


def test_events_and_trace_file(solve, cfg):
    events = []
    script = [block("r = ask('q')\nprint(r)"), block("submit(r)")]
    result = solve(script, asks="a", on_event=events.append)
    kinds = [e.kind for e in events]
    assert kinds == [
        "run.start", "agent.start", "model.reply", "cell.start", "call.start", "call.end", "cell",
        "model.reply", "cell.start", "cell", "agent.end", "run.end",
    ]  # fmt: skip
    assert events[6]["code"].startswith("r = ask") and events[6]["output"] == "a"
    assert events[-1]["status"] == "ok" and events[-1]["usage"]["calls"] == 3

    trace = read_trace(result.trace)
    assert [row["kind"] for row in trace] == kinds
    assert trace[-1]["answer"] == "a" and json.dumps(trace)


def test_tracing_can_be_disabled(solve, cfg):
    result = solve([block("submit(1)")], config=cfg.replace(trace=False))
    assert result.trace is None


def test_a_broken_event_handler_does_not_break_the_run(solve, capsys):
    def boom(event):
        raise RuntimeError("handler bug")

    assert solve([block("submit(1)")], on_event=boom).answer == 1
    assert "an event handler failed" in capsys.readouterr().err


# ------------------------------------------------------------------ hygiene


def test_backend_errors_become_a_failed_result(solve):
    def script(turn):
        raise rlm.ModelError("credentials rejected")

    result = solve(Scripted(script))
    assert result.status == "failed" and "credentials rejected" in result.error and not result.ok


def test_run_refuses_to_nest_inside_an_event_loop(cfg):
    async def inner():
        return rlm.run("t", model=Scripted([block("submit(1)")]), config=cfg)

    with pytest.raises(rlm.RlmError, match="arun"):
        asyncio.run(inner())


def test_arun_works_inside_an_event_loop_and_runs_concurrently(cfg):
    async def many():
        model = Scripted([block("submit(context * 2)")])
        return await asyncio.gather(*(rlm.arun("t", n, model=model, config=cfg) for n in range(5)))

    assert [r.answer for r in asyncio.run(many())] == [0, 2, 4, 6, 8]


def test_no_worker_processes_are_left_behind(solve):
    import subprocess

    def workers() -> int:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        return sum("rlm-worker" in line for line in out.splitlines())

    before = workers()
    solve(Scripted(sub_agent_script))
    assert workers() == before


def test_empty_task_is_rejected(cfg):
    with pytest.raises(rlm.ConfigError):
        rlm.run("  ", model=Scripted([]), config=cfg)


# ----------------------------------------------------------------- batching


def batch_items(prompt: str) -> list[str]:
    return re.findall(r'<item index="\d+">\n(.*?)\n</item>', prompt, re.S)


def test_ask_map_packs_small_items_into_shared_calls(solve):
    seen: list[int] = []

    def asks(prompt):
        items = batch_items(prompt)
        seen.append(len(items))
        return json.dumps([item.upper() for item in items])

    script = [block("submit(ask_map('Upper-case it.', [f'w{i}' for i in range(40)]))")]
    result = solve(script, asks=asks)
    assert result.answer == [f"W{i}" for i in range(40)]
    assert seen == [16, 16, 8] or sorted(seen) == [8, 16, 16]
    assert result.usage.calls == 4  # one root turn and three batched calls instead of forty


def test_batched_calls_validate_each_item_against_the_schema(solve):
    def asks(prompt):
        return json.dumps([{"n": len(item)} for item in batch_items(prompt)])

    script = [block("submit(ask_map('Measure.', ['a', 'bb', 'ccc'], schema={'type': 'object', 'required': ['n']}))")]
    model = Scripted(script, asks=asks)
    assert solve(model).answer == [{"n": 1}, {"n": 2}, {"n": 3}]
    system = next(t.system for t in model.turns if t.oneshot)
    assert "JSON array of exactly 3 elements" in system and '"required"' in system


def test_unusable_batch_reply_falls_back_to_one_call_per_item(solve):
    def asks(prompt):
        if "<item index=" in prompt:
            return json.dumps(["only one element"])  # wrong length for three items
        return data_of(prompt).upper()

    script = [block("submit(ask_map('Upper-case it.', ['a', 'b', 'c']))")]
    result = solve(script, asks=asks)
    assert result.answer == ["A", "B", "C"]
    assert result.usage.calls == 1 + 2 + 3  # root turn, the batch and its repair attempt, then one call per item


def test_batched_reply_is_repaired_before_falling_back(solve):
    replies = iter(['["just one"]', '["A", "B", "C"]'])
    script = [block("submit(ask_map('Upper-case it.', ['a', 'b', 'c']))")]
    model = Scripted(script, asks=lambda p: next(replies))
    result = solve(model)
    assert result.answer == ["A", "B", "C"] and result.usage.calls == 3
    assert "Your previous reply was rejected" in model.turns[-1].last


def test_bare_values_are_accepted_for_single_field_object_schemas(solve):
    def asks(prompt):
        return json.dumps(["billing" if "charge" in item else "other" for item in batch_items(prompt)])

    schema = {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}
    script = [block(f"submit(ask_map('Classify.', ['a charge', 'a hello', 'charged twice'], schema={schema!r}))")]
    result = solve(script, asks=asks)
    assert result.answer == [{"category": "billing"}, {"category": "other"}, {"category": "billing"}]
    assert result.usage.calls == 2


def test_large_items_and_distinct_prompts_are_never_batched(solve, cfg):
    sizes: list[int] = []

    def asks(prompt):
        sizes.append(len(batch_items(prompt)))
        return "x"

    big = "y" * 20_000
    script = [
        block(
            f"a = ask_map('Read.', ['{big}', '{big}'])\nb = ask_map(['one', 'two', 'three'])\nsubmit(len(a) + len(b))"
        )
    ]
    assert solve(script, asks=asks).answer == 5
    assert sizes == [0, 0, 0, 0, 0]


def test_batching_can_be_switched_off(solve, cfg):
    calls: list[str] = []
    script = [block("submit(ask_map('Echo.', ['a', 'b', 'c']))")]
    result = solve(script, asks=lambda p: calls.append(p) or data_of(p), config=cfg.replace(ask_batch=1))
    assert result.answer == ["a", "b", "c"] and len(calls) == 3
