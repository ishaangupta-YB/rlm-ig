"""Read a trace back: a text outline for the terminal, or a standalone HTML page."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from typing import Any

from ..text import clip_middle, first_line
from .console import _prose, agent_label


@dataclass
class Step:
    number: int
    reply: str = ""
    reply_seconds: float = 0.0
    usage: dict = field(default_factory=dict)
    code: str | None = None
    output: str = ""
    error: bool = False
    seconds: float = 0.0
    submitted: bool = False
    calls: list[dict] = field(default_factory=list)
    children: list[str] = field(default_factory=list)


@dataclass
class AgentNode:
    id: str
    depth: int = 0
    parent: str | None = None
    task: str = ""
    context_size: int = 0
    steps: list[Step] = field(default_factory=list)
    status: str = "running"
    answer: str | None = None
    error: str | None = None
    started: float = 0.0
    ended: float | None = None


@dataclass
class Trace:
    start: dict = field(default_factory=dict)
    end: dict = field(default_factory=dict)
    agents: dict[str, AgentNode] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def root(self) -> AgentNode | None:
        return self.agents.get("0")


def build(events: list[dict[str, Any]]) -> Trace:
    """Fold a flat event list into agents, steps and calls."""
    trace = Trace()
    open_calls: dict[int, dict] = {}
    for e in events:
        kind = e.get("kind")
        agent = trace.agents.get(e.get("agent", ""))
        if kind == "run.start":
            trace.start = e
        elif kind == "run.end":
            trace.end = e
        elif kind == "note":
            trace.notes.append(e.get("message", ""))
        elif kind == "agent.start":
            node = AgentNode(
                id=e["agent"],
                depth=e.get("depth", 0),
                parent=e.get("parent"),
                task=e.get("task", ""),
                context_size=e.get("context_size", 0),
                started=e.get("t", 0.0),
            )
            trace.agents[node.id] = node
            parent = trace.agents.get(node.parent or "")
            if parent and parent.steps:
                parent.steps[-1].children.append(node.id)
        elif agent is None:
            continue
        elif kind == "model.reply":
            agent.steps.append(
                Step(
                    e.get("step", len(agent.steps) + 1), e.get("text", ""), e.get("seconds", 0.0), e.get("usage") or {}
                )
            )
        elif kind == "cell.start" and agent.steps:
            agent.steps[-1].code = e.get("code")
        elif kind == "cell" and agent.steps:
            step = agent.steps[-1]
            step.code = e.get("code", step.code)
            step.output, step.error = e.get("output", ""), bool(e.get("error"))
            step.seconds, step.submitted = e.get("seconds", 0.0), bool(e.get("submitted"))
        elif kind == "call.start" and agent.steps:
            call = {"op": e.get("op"), "n": e.get("n", 1), "name": e.get("name", ""), "chars": e.get("chars", 0)}
            agent.steps[-1].calls.append(call)
            open_calls[e.get("call", -1)] = call
        elif kind == "call.end":
            call = open_calls.pop(e.get("call", -1), None)
            if call is not None:
                call.update(ok=e.get("ok"), seconds=e.get("seconds"), error=e.get("error"), failed=e.get("failed", 0))
        elif kind == "agent.end":
            agent.status, agent.answer, agent.error = e.get("status", ""), e.get("answer"), e.get("error")
            agent.ended = e.get("t")
    return trace


# ------------------------------------------------------------------- text


def _call_line(call: dict) -> str:
    count = f" x{call['n']}" if call.get("n", 1) != 1 else ""
    took = f" {call['seconds']:.1f}s" if isinstance(call.get("seconds"), (int, float)) else ""
    detail = call.get("name") or (f"{call['chars']:,} chars" if call.get("chars") else "")
    state = "" if call.get("ok", True) else f"  FAILED: {first_line(call.get('error') or '', 80)}"
    failed = f"  ({call['failed']} failed)" if call.get("failed") else ""
    return f"{call['op']}{count}{took}  {detail}{failed}{state}".rstrip()


def outline(events: list[dict[str, Any]], *, full: bool = False, width: int = 100) -> str:
    """A readable account of a run. ``full`` includes code and output."""
    trace = build(events)
    start, end = trace.start, trace.end
    lines = [f"task    {first_line(start.get('task', '?'), width)}"]
    models = start.get("model", "?")
    if start.get("sub_model") not in (None, models):
        models += f"  (sub: {start['sub_model']})"
    lines.append(f"model   {models}")
    lines.append(f"data    {start.get('summary', '')}")
    if end:
        lines.append(f"result  {end.get('status')} in {end.get('seconds', 0):.1f}s, {end.get('usage_text', '')}")
        lines.append(f"        {end.get('agents', 0)} agent(s), {end.get('steps', 0)} turn(s)")
        if end.get("error"):
            lines.append(f"error   {end['error']}")
        if end.get("note"):
            lines.append(f"note    {end['note']}")
    else:
        lines.append("result  (trace ends without a result: the run was interrupted or is still going)")
    for note in trace.notes:
        lines.append(f"note    {note}")
    lines.append("")

    def walk(agent_id: str, pad: str) -> None:
        agent = trace.agents[agent_id]
        head = "root" if agent.depth == 0 else f"sub-agent {agent_label(agent.id)}: {first_line(agent.task, 60)}"
        lines.append(f"{pad}{head}  [{agent.context_size:,} chars of context]")
        for step in agent.steps:
            note = first_line(_prose(step.reply), width - len(pad) - 24)
            lines.append(f"{pad}  turn {step.number} ({step.reply_seconds:.1f}s)  {note}")
            if full and step.code:
                lines.extend(f"{pad}      > {row}" for row in step.code.splitlines())
            for call in step.calls:
                lines.append(f"{pad}      call  {_call_line(call)}")
            for child in step.children:
                walk(child, pad + "      ")
            if step.code is None:
                lines.append(f"{pad}      (no code block in this reply)")
            elif step.submitted:
                lines.append(f"{pad}      submitted")
            elif step.error:
                last = (step.output.strip().splitlines() or ["error"])[-1]
                lines.append(f"{pad}      error: {first_line(last, 90)}")
            if full and step.output.strip() and not step.submitted:
                lines.extend(f"{pad}      | {row}" for row in clip_middle(step.output, 3000).splitlines())
        if agent.status in ("ok", "partial"):
            lines.append(f"{pad}  -> {first_line(agent.answer or '', 100)}")
        elif agent.error:
            lines.append(f"{pad}  -> {agent.status}: {first_line(agent.error, 100)}")

    if trace.root:
        walk("0", "")
    if end.get("answer") is not None:
        lines += ["", "answer", str(end["answer"])]
    return "\n".join(lines)


# ------------------------------------------------------------------- html

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>rlm trace: __TITLE__</title>
<style>
:root{--bg:#fbfaf8;--fg:#1c1b1a;--mut:#6f6b66;--line:#e2ded8;--card:#fff;--acc:#2f6f5e;--bad:#b3261e;--warn:#9a6700;--code:#f4f1ec}
@media(prefers-color-scheme:dark){:root{--bg:#161514;--fg:#ecebe8;--mut:#9d9891;--line:#33302c;--card:#1e1d1b;--acc:#7cc4b0;--bad:#ff8a80;--warn:#e3b341;--code:#242220}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 16px 80px}h1{font-size:20px;margin:0 0 4px}
.task{white-space:pre-wrap;color:var(--mut);margin:0 0 18px;max-height:9em;overflow:auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:0 0 22px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.stat b{display:block;font-size:17px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.stat span{color:var(--mut);font-size:12px}
.ok{color:var(--acc)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.agent{border-left:2px solid var(--line);padding-left:12px;margin:10px 0}
.agent>summary{cursor:pointer;font-weight:600;list-style:none;padding:4px 0}.agent>summary::-webkit-details-marker{display:none}
.agent>summary:before{content:"▸ ";color:var(--mut)}.agent[open]>summary:before{content:"▾ "}
.meta{color:var(--mut);font-weight:400;font-size:13px}
.step{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow:hidden}
.step>summary{cursor:pointer;padding:8px 12px;display:flex;gap:10px;align-items:baseline;list-style:none}
.step>summary::-webkit-details-marker{display:none}.num{font-variant-numeric:tabular-nums;color:var(--mut);white-space:nowrap}
.note{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.body{padding:0 12px 10px}
pre{background:var(--code);border-radius:8px;padding:10px 12px;margin:8px 0;overflow:auto;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word;max-height:420px}
.lab{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:10px 0 -4px}
.call{font-size:13px;color:var(--mut);padding:2px 0}.call b{color:var(--fg);font-weight:600}
.answer{border:1px solid var(--acc);border-radius:10px;padding:12px 14px;margin:22px 0 0;background:var(--card)}
.bar{display:flex;gap:8px;margin:0 0 14px;flex-wrap:wrap}button{font:inherit;font-size:13px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:5px 10px;cursor:pointer}
</style></head><body><main>
<h1>rlm trace</h1><p class="task" id="task"></p>
<div class="stats" id="stats"></div>
<div class="bar"><button id="open">Expand all</button><button id="shut">Collapse all</button></div>
<div id="tree"></div><div id="answer"></div>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
const T=JSON.parse(document.getElementById('data').textContent);
const el=(t,c,x)=>{const n=document.createElement(t);if(c)n.className=c;if(x!==undefined)n.textContent=x;return n};
const first=s=>{s=(s||'').split('```')[0];for(const l of s.split('\\n')){if(l.trim())return l.trim()}return ''};
const num=n=>n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e4?Math.round(n/1e3)+'k':n>=1e3?(n/1e3).toFixed(1)+'k':String(n||0);
document.getElementById('task').textContent=T.start.task||'';
const u=(T.end.usage)||{},st=T.end.status||'unfinished';
const stats=[[st,'status',st==='ok'?'ok':st==='partial'?'warn':'bad'],[(T.end.seconds||0).toFixed(1)+'s','wall time'],
[u.calls||0,'model calls'],[num(u.input_tokens)+' / '+num(u.output_tokens),'tokens in / out'],[num(u.cached_tokens),'cached input'],
[u.cost_usd==null?'n/a':'$'+u.cost_usd.toFixed(4),'cost'],[(T.end.agents||0)+' / '+(T.end.steps||0),'agents / turns'],[T.start.model||'','model']];
for(const [v,k,c] of stats){const d=el('div','stat');const b=el('b',c||'',v);d.append(b,el('span','',k));document.getElementById('stats').append(d)}
function agent(id){const a=T.agents[id];const d=el('details','agent');d.open=a.depth<1;
 const s=el('summary');s.append(a.depth?('sub-agent '+id.slice(2)+': '+first(a.task).slice(0,90)):'root agent');
 const res=a.status==='ok'||a.status==='partial'?' ✔':a.status==='running'?'':' ✗';
 s.append(el('span','meta','  '+num(a.context_size)+' chars · '+a.steps.length+' turn'+(a.steps.length===1?'':'s')+res));d.append(s);
 for(const p of a.steps){const x=el('details','step');const h=el('summary');
  h.append(el('span','num','turn '+p.number),el('span','note',first(p.reply)),
   el('span','num '+(p.error?'bad':p.submitted?'ok':''),p.submitted?'submitted':p.error?'error':p.reply_seconds.toFixed(1)+'s'));
  x.append(h);const b=el('div','body');
  if(p.reply){b.append(el('div','lab','model'));b.append(el('pre','',p.reply))}
  for(const c of p.calls){const r=el('div','call');r.append(el('b','',c.op+(c.n>1?' ×'+c.n:'')),' '+(c.name||num(c.chars)+' chars')+
    (c.seconds!=null?' · '+c.seconds.toFixed(1)+'s':'')+(c.failed?' · '+c.failed+' failed':'')+(c.ok===false?' · FAILED: '+(c.error||''):''));b.append(r)}
  for(const k of p.children)b.append(agent(k));
  if(p.output&&!p.submitted){b.append(el('div','lab',p.error?'output (error)':'output'));b.append(el('pre',p.error?'bad':'',p.output))}
  x.append(b);d.append(x)}
 if(a.error){d.append(el('div','call bad',a.status+': '+a.error))}return d}
if(T.agents['0'])document.getElementById('tree').append(agent('0'));
if(T.end.answer!=null){const a=el('div','answer');a.append(el('div','lab','answer'),el('pre','',String(T.end.answer)));document.getElementById('answer').append(a)}
if(T.end.error){const a=el('div','answer');a.append(el('div','lab bad','error'),el('pre','bad',T.end.error));document.getElementById('answer').append(a)}
const all=o=>document.querySelectorAll('details').forEach(d=>d.open=o);
document.getElementById('open').onclick=()=>all(true);document.getElementById('shut').onclick=()=>all(false);
</script></body></html>
"""


def render_html(events: list[dict[str, Any]]) -> str:
    """A self-contained page (no network, no dependencies) for one trace."""
    trace = build(events)
    payload = {
        "start": trace.start,
        "end": trace.end,
        "notes": trace.notes,
        "agents": {
            agent_id: {
                "depth": a.depth,
                "task": a.task,
                "context_size": a.context_size,
                "status": a.status,
                "error": a.error,
                "steps": [
                    {
                        "number": s.number,
                        "reply": s.reply,
                        "reply_seconds": s.reply_seconds,
                        "output": s.output,
                        "error": s.error,
                        "submitted": s.submitted,
                        "calls": s.calls,
                        "children": s.children,
                    }
                    for s in a.steps
                ],
            }
            for agent_id, a in trace.agents.items()
        },
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\u0021--")
    title = html.escape(first_line(trace.start.get("task", "run"), 60))
    return _PAGE.replace("__TITLE__", title).replace("__DATA__", data)
