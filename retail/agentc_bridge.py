"""The app's link to the Couchbase Agent Catalog (`agentc`): Tools Hub, Prompts
Hub and Agent Tracer, all in bucket `retail`.

* Tools Hub   — `retail.agent_catalog.tools`: the tools in agent_catalog/tools,
  published on every commit (the post-commit hook runs `agentc index` and
  `agentc publish`).
* Prompts Hub — `retail.agent_catalog.prompts`: the prompts the app sends its
  models, read from here at run time (`prompt()`), with the copy in the code as
  a fallback.
* Agent Tracer — `retail.agent_activity.logs`: every request the app handles
  (a question, a search, a plan, a photo read, a cart change, a checkout) is
  logged as a span: what the user asked, each step it took, what came back.

Which tools the assistant may use is decided elsewhere, by
`retail.governance.authorize()` — registering a tool doesn't grant it.

Everything here falls back to doing nothing when agentc or Couchbase can't be
reached, so the store keeps working. Log writes go through a background thread:
a request never waits on its trace.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from typing import Any

from config import settings
from retail.trace import Stage, Step, Trace, index_kind, stages

log = logging.getLogger(__name__)

LOGS = f"`{settings.bucket}`.`agent_activity`.`logs`"
TOOLS = f"`{settings.bucket}`.`agent_catalog`.`tools`"
PROMPTS = f"`{settings.bucket}`.`agent_catalog`.`prompts`"
ROOT = "store_assistant"

_lock = threading.Lock()
_catalog: Any = None
_state: dict[str, Any] = {"ok": None, "error": None}


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
def catalog():
    """The `agentc.Catalog`, built once. None when it can't be reached."""
    global _catalog
    if _catalog is not None or _state["ok"] is False:
        return _catalog
    with _lock:
        if _catalog is None and _state["ok"] is None:
            try:
                from agentc import Catalog
                _catalog = Catalog()
                _state.update(ok=True, error=None)
            except Exception as exc:
                _state.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:240])
                log.warning("Agent Catalog unavailable: %s", exc)
    return _catalog


def status() -> tuple[bool, str]:
    catalog()
    return bool(_state["ok"]), _state.get("error") or ""


def version() -> dict[str, Any]:
    """The published catalog's version: the git commit it was built from — the
    newest of the tools and the prompts, since a commit may change only one."""
    try:
        from retail.cb import query
        rows = [r for coll in (TOOLS, PROMPTS) for r in query(
            f"SELECT t.version.identifier AS id, MAX(t.version.timestamp) AS ts "
            f"FROM {coll} AS t GROUP BY t.version.identifier "
            f"ORDER BY ts DESC LIMIT 1")]
        return max(rows, key=lambda r: str(r.get("ts") or "")) if rows else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Prompts Hub
# --------------------------------------------------------------------------- #
_prompts: dict[str, tuple[float, str, str]] = {}
PROMPT_TTL = 60.0


def prompt(name: str, fallback: str) -> tuple[str, str]:
    """A prompt's text from the Prompts Hub -> (text, where it came from).
    Where is "hub @ <commit>", or "code" when the hub can't be read."""
    now = time.monotonic()
    hit = _prompts.get(name)
    if hit and now - hit[0] < PROMPT_TTL:
        return hit[1], hit[2]
    text, where = fallback, "code"
    cat = catalog()
    if cat is not None:
        try:
            rec = cat.find_prompts(name=name)
            rec = rec[0] if isinstance(rec, list) and rec else rec
            if rec is not None and str(getattr(rec, "content", "") or "").strip():
                text = str(rec.content).rstrip("\n")
                ver = getattr(getattr(rec.meta, "version", None), "identifier", "") or ""
                where = "hub" + (f" @ {ver[:7]}" if ver else "")
        except Exception as exc:
            log.warning("prompt %s not read from the hub: %s", name, exc)
    _prompts[name] = (now, text, where)
    return text, where


_offered: dict[str, dict[str, Any]] = {}     # tool name -> its schema, as sent to a model
_JSON = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object",
         list: "array"}


def tool_schema(func) -> dict[str, Any]:
    """A tool function -> the function schema a chat model is given (OpenAI format)."""
    import inspect
    import typing
    hints = typing.get_type_hints(func)
    props, required = {}, []
    for name, prm in inspect.signature(func).parameters.items():
        props[name] = {"type": _JSON.get(hints.get(name, str), "string")}
        if prm.default is inspect.Parameter.empty:
            required.append(name)
    doc = " ".join((inspect.getdoc(func) or "").split())
    return {"type": "function", "function": {
        "name": func.__name__, "description": doc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


def agent(name: str, fallback: str, tool_names: list[str]):
    """A prompt from the Prompts Hub with the tools it lists, from the Tools Hub ->
    (system text, where it came from, {tool name: function}). Falls back to the
    copy in the code and agent_catalog/tools/store_tools.py."""
    text, where = prompt(name, fallback)
    funcs: dict[str, Any] = {}
    cat = catalog()
    if cat is not None and where != "code":
        try:
            rec = cat.find_prompts(name=name)
            rec = rec[0] if isinstance(rec, list) and rec else rec
            funcs = {t.meta.name: t.func for t in (rec.tools or [])}
        except Exception as exc:
            log.warning("tools for %s not read from the hub: %s", name, exc)
    if not funcs:
        import importlib.util
        from config import ROOT
        spec = importlib.util.spec_from_file_location(
            "store_tools", ROOT / "agent_catalog" / "tools" / "store_tools.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        funcs = {n: getattr(mod, n) for n in tool_names}
    for n, f in funcs.items():
        _offered[n] = tool_schema(f)
    return text, where, funcs


# --------------------------------------------------------------------------- #
# Agent Tracer: writing
# --------------------------------------------------------------------------- #
_writes: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_roots: dict[str, Any] = {}


def _drain() -> None:
    while True:
        job = _writes.get()
        try:
            job()
        except Exception as exc:
            log.warning("agent trace write failed: %s", exc)
        finally:
            _writes.task_done()


def _submit(job) -> None:
    global _worker
    if _worker is None:
        with _lock:
            if _worker is None:
                _worker = threading.Thread(target=_drain, name="agentc-trace", daemon=True)
                _worker.start()
    _writes.put(job)


def flush(timeout: float = 15.0) -> None:
    """Wait until queued trace writes have landed — for tests and the Refresh button."""
    end = time.monotonic() + timeout
    while _writes.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.03)


def _root(session: str):
    """One root span per app session: building one costs ~0.4 s, a child is free."""
    root = _roots.get(session)
    if root is None:
        if len(_roots) > 64:
            _roots.clear()
        root = _roots[session] = catalog().Span(name=ROOT, session=session)
    return root


def _jsonable(v: Any, limit: int = 4000) -> Any:
    try:
        text = json.dumps(v, default=str)
    except Exception:
        return str(v)[:limit]
    return json.loads(text) if len(text) <= limit else text[:limit]


# Which catalog tool each flow is: logged as its Tool Call and Tool Results
FLOW_TOOL = {"search": "search_products", "plan_room": "plan_room",
             "read_photo": "analyze_room_photo", "replicate_photo": "analyze_room_photo",
             "add_to_cart": "add_to_cart", "checkout": "checkout",
             "save_preference": "save_preference"}

# agentc log kinds -> the trace types Capella's Agent Tracer filters by
TRACE_TYPES = {"user": "User", "request-header": "Internal", "chat-completion": "LLM",
               "tool-call": "Tool Call", "tool-result": "Tool Results", "edge": "Hand-off",
               "system": "System", "key-value": "System", "begin": "System",
               "end": "System", "assistant": "Assistant"}
TRACE_TYPE_ORDER = ["User", "Internal", "LLM", "Tool Call", "Tool Results", "Hand-off",
                    "System", "Assistant"]


def _detail(s: Step, n: int) -> dict[str, Any]:
    """One step, for a log: what it did, and the service, operation and data."""
    d = {"step": n, "what": s.what, "service": s.service, "result": s.result, "how": s.how,
         "ms": s.ms, "reads": [f"{c} ({s.op('r', c)})" for c in s.collections],
         "writes": [f"{c} ({s.op('w', c)})" for c in s.writes],
         "indexes": [f"{i} ({index_kind(i)})" for i in s.indexes]}
    if s.tokens_spent:
        d["tokens_spent"] = s.tokens_spent
    if s.tokens_saved:
        d["tokens_saved"] = s.tokens_saved
    if s.model:
        d["model"] = s.model
    return d


def _system_log(g: Stage, steps: list[Step], first: int):
    """Steps the app ran itself -> one System message: the stage, its result, the
    data it touched, and each step underneath."""
    from agentc_core.activity.models.content import KeyValueContent
    rows = [_detail(s, first + k) for k, s in enumerate(steps)]
    uniq = lambda key: list(dict.fromkeys(x for r in rows for x in r[key]))  # noqa: E731
    value = {"result": g.result if g.kind == "system" else "", "ms": round(sum(
        s.ms for s in steps), 1), "services": list(dict.fromkeys(s.service for s in steps)),
        "reads": uniq("reads"), "writes": uniq("writes"), "indexes": uniq("indexes"),
        "steps": rows}
    spent, saved = sum(s.tokens_spent for s in steps), sum(s.tokens_saved for s in steps)
    if spent:
        value["tokens_spent"] = spent
    if saved:
        value["tokens_saved"] = saved
    key = g.title if g.kind == "system" else f"Inside {g.title[len('Ran the tool '):]}" \
        if g.title.startswith("Ran the tool ") else g.title
    return KeyValueContent(key=key, value=_jsonable(value, 12000))


def _stage_logs(g: Stage, first: int):
    """One stage -> its messages, in the Agent Tracer's trace types:
    model call -> Internal (the prompt and tools it was sent) + LLM (its answer)
    + a Tool Call for each tool it chose; tool -> System (the steps it ran) +
    Tool Results; the rest -> one System message."""
    from agentc_core.activity.models.content import (ChatCompletionContent,
                                                      RequestHeaderContent, ToolCallContent,
                                                      ToolResultContent)
    if g.kind == "system":
        return [_system_log(g, g.steps, first)]
    if g.kind == "tool":
        inner = [s for s in g.steps if s.tool_result is None]
        out = [_system_log(g, inner, first)] if inner else []
        for s in g.steps:
            if s.tool_result is not None:
                r = s.tool_result
                out.append(ToolResultContent(tool_call_id=str(r.get("id")),
                                             status=r.get("status"),
                                             tool_result=_jsonable(r.get("result"))))
        return out
    s = g.steps[0]
    used = [name for name in _prompts if name in s.how]
    tools = ([RequestHeaderContent.Tool(name=n, description=sc["function"]["description"],
                                        args_schema=sc["function"]["parameters"])
              for n, sc in _offered.items()] if "store_assistant" in used else [])
    out = [RequestHeaderContent(tools=tools, meta={  # agentc's default for tools isn't a list
        "model": s.model, "what": s.what,
        "prompts": [{"name": name, "source": _prompts[name][2],
                     "content": _prompts[name][1][:4000]} for name in used]})]
    out.append(ChatCompletionContent(output=s.result or "", meta={
        "latency_ms": s.ms, "usage": {"total_tokens": s.tokens_spent}, **_detail(s, first)}))
    return out + [ToolCallContent(tool_name=c["name"], tool_args=_jsonable(c.get("args") or {}),
                                  tool_call_id=str(c["id"]), status="success")
                  for c in s.tool_calls]


def log_run(flow: str, t: Trace | None, *, session: str, user: str | None,
            asked: str = "", answer: str = "") -> str | None:
    """Log one request to the Agent Tracer as a span under the session's root span:
    User (what was asked), Tool Call (the catalog tool it is), each step, Tool
    Results, Assistant (the answer). Queued, so it costs the request nothing.
    -> the run's id (a tag on every message of the span)."""
    if not t or not t.steps or catalog() is None:
        return None
    run = t.run = uuid.uuid4().hex[:10]
    steps = list(t.steps)

    def write() -> None:
        from agentc_core.activity.models.content import (AssistantContent, ToolCallContent,
                                                          ToolResultContent, UserContent)
        span = _root(session).new(name=flow, run=run, user=user or "GUEST",
                                  ms=round(sum(s.ms for s in steps), 1),
                                  tokens_spent=sum(s.tokens_spent for s in steps),
                                  tokens_saved=sum(s.tokens_saved for s in steps))
        span.enter()
        span.log(UserContent(value=asked or flow, user_id=user or "GUEST"))
        tool = FLOW_TOOL.get(flow)
        call_id = uuid.uuid4().hex[:12]
        blocked = next((s for s in steps if s.service == "Agent Catalog"
                        and s.result.startswith("Blocked")), None)
        if tool:
            span.log(ToolCallContent(tool_name=tool, tool_args={"request": asked or flow},
                                     tool_call_id=call_id,
                                     status="error" if blocked else "success"))
        first = 1
        for g in stages(steps):
            for c in _stage_logs(g, first):
                span.log(c)
            first += len(g.steps)
        if tool:
            span.log(ToolResultContent(
                tool_call_id=call_id, status="error" if blocked else "success",
                tool_result=_jsonable(blocked.result if blocked else
                                      (answer or steps[-1].result))))
        if answer:
            span.log(AssistantContent(value=str(answer)[:4000]))
        span.exit()

    _submit(write)
    return run


def clear_activity() -> int:
    """Empty the Agent Tracer. The Tools and Prompts Hubs are left alone."""
    from retail.cb import query
    flush()
    try:
        n = query(f"SELECT RAW COUNT(1) FROM {LOGS}")[0]
        query(f"DELETE FROM {LOGS}")
        _roots.clear()
        return int(n)
    except Exception as exc:
        log.warning("could not clear agent activity: %s", exc)
        return 0


# --------------------------------------------------------------------------- #
# reading back, for the Agent Catalog page
# --------------------------------------------------------------------------- #
def _current(kind: str) -> set[str] | None:
    """The names in the catalog as last indexed (`.agent-catalog/<kind>.json`, written
    by the post-commit hook) — so an item deleted from the source drops out of the
    hub. None when there's no local index."""
    from config import ROOT
    try:
        return {i["name"] for i in json.loads(
            (ROOT / ".agent-catalog" / f"{kind}.json").read_text()).get("items", [])}
    except (OSError, ValueError, KeyError):
        return None


def _latest(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """The newest record of each name. Publishing is incremental: an item that
    didn't change keeps the version it was first published at."""
    keep, seen = _current(kind), set()
    out = []
    for r in rows:                       # rows are newest first within a name
        if r["name"] in seen or (keep is not None and r["name"] not in keep):
            continue
        seen.add(r["name"])
        out.append(r)
    return out


def _with_versions(rows: list[dict[str, Any]], coll: str) -> list[dict[str, Any]]:
    """Add how many versions of each item were published, and its source path
    relative to the repo — the Version and Source columns of the hubs."""
    from config import ROOT
    from retail.cb import query
    try:
        n = {r["name"]: r["n"] for r in query(
            f"SELECT x.name, COUNT(DISTINCT x.version.identifier) AS n FROM {coll} AS x "
            f"GROUP BY x.name")}
    except Exception:
        n = {}
    for r in rows:
        r["versions"] = n.get(r["name"], 1)
        src = str(r.get("source") or "")
        r["source"] = src[len(str(ROOT)) + 1:] if src.startswith(str(ROOT)) else src
    return rows


def tools() -> list[dict[str, Any]]:
    """The tools in the catalog, each at its latest published version."""
    from retail.cb import query
    try:
        return _with_versions(_latest(query(
            f"SELECT t.name, t.description, t.annotations, t.record_kind, t.source, "
            f"t.catalog_identifier, t.content.func_content AS code, t.version.identifier AS "
            f"`commit`, t.version.timestamp AS ts FROM {TOOLS} AS t "
            f"ORDER BY t.name, t.version.timestamp DESC"), "tools"), TOOLS)
    except Exception as exc:
        log.warning("could not read the Tools Hub: %s", exc)
        return []


def prompts() -> list[dict[str, Any]]:
    from retail.cb import query
    try:
        return _with_versions(_latest(query(
            f"SELECT p.name, p.description, p.annotations, p.content, p.tools, p.record_kind, "
            f"p.source, p.catalog_identifier, p.version.identifier AS `commit`, "
            f"p.version.timestamp AS ts FROM {PROMPTS} AS p "
            f"ORDER BY p.name, p.version.timestamp DESC"), "prompts"), PROMPTS)
    except Exception as exc:
        log.warning("could not read the Prompts Hub: %s", exc)
        return []


def apps() -> list[str]:
    """App names: the root span names in the log."""
    from retail.cb import query
    try:
        return query(f"SELECT DISTINCT RAW l.span.name[0] FROM {LOGS} AS l "
                     f"WHERE l.span.name[0] IS NOT MISSING")
    except Exception:
        return []


def sessions(*, app: str | None = None, since_ms: int | None = None,
             session: str = "", tag: str = "", limit: int = 100) -> list[dict[str, Any]]:
    """Sessions, most recently active first — the Agent Tracer's list. Filters:
    app (root span), active since, a Session ID, or a tag ("user=RET-1001", or
    any part of one)."""
    from retail.cb import query
    where = ["l.span.name[0] IS NOT MISSING", "l.span.session IS NOT MISSING",
             "l.content.kind NOT IN ['begin', 'end']"]
    if app:
        where.append("l.span.name[0] = $app")
    if session.strip():
        where.append("CONTAINS(LOWER(l.span.session), $sid)")
    if tag.strip():
        where.append(f"l.span.session IN (SELECT DISTINCT RAW t.span.session FROM {LOGS} "
                     f"AS t WHERE t.span.session IS NOT MISSING AND ANY p IN "
                     f"OBJECT_PAIRS(t.annotations) SATISFIES CONTAINS(LOWER(p.name || '=' "
                     f"|| TOSTRING(p.val)), $tag) END)")
    having = "HAVING MAX(STR_TO_MILLIS(l.timestamp)) >= $since" if since_ms else ""
    try:
        return query(f"SELECT l.span.session AS sid, l.span.name[0] AS root, "
                     f"MIN(STR_TO_MILLIS(l.timestamp)) AS start_ms, "
                     f"MAX(STR_TO_MILLIS(l.timestamp)) AS last_ms, COUNT(1) AS messages, "
                     f"MAX(l.catalog_version.identifier) AS cid, "
                     f"ARRAY_AGG(DISTINCT l.span.name[1]) AS spans, "
                     f"ARRAY_AGG(DISTINCT l.annotations.`user`) AS users "
                     f"FROM {LOGS} AS l WHERE {' AND '.join(where)} "
                     f"GROUP BY l.span.session, l.span.name[0] {having} "
                     f"ORDER BY last_ms DESC LIMIT {int(limit)}",
                     app=app, sid=session.strip().lower(), tag=tag.strip().lower(),
                     since=since_ms)
    except Exception as exc:
        log.warning("could not read the Agent Tracer: %s", exc)
        return []


def session_logs(session: str) -> list[dict[str, Any]]:
    """One session's trace logs, in the order they happened."""
    from retail.cb import query
    try:
        return query(f"SELECT l.identifier, l.`timestamp`, l.span.name AS span, l.content, "
                     f"l.annotations, l.catalog_version.identifier AS cid "
                     f"FROM {LOGS} AS l WHERE l.span.session = $s "
                     f"AND l.content.kind NOT IN ['begin', 'end'] "
                     f"ORDER BY STR_TO_MILLIS(l.`timestamp`), l.`timestamp`", s=session)
    except Exception as exc:
        log.warning("could not read session %s: %s", session, exc)
        return []


def prompt_uses() -> dict[str, int]:
    """How many model calls in the tracer used each prompt."""
    from retail.cb import query
    try:
        hows = query(f"SELECT RAW l.content.meta.how FROM {LOGS} AS l "
                     f"WHERE l.content.kind = 'chat-completion'")
    except Exception:
        return {}
    out: dict[str, int] = {}
    for how in hows:
        tail = str(how or "").split("prompt", 1)[-1] if "prompt" in str(how or "") else ""
        for name in tail.replace("s ", " ", 1).split("·")[0].replace(",", " ").split():
            out[name] = out.get(name, 0) + 1
    return out
