"""A per-request record of what the system did — the "Show what happened" panel.

Every step says which service did the work (Data/KV, Query, Search, Vector,
Agent Memory, Agent Catalog, Model), against which collections, and how long
it took. The audience sees Couchbase doing the work, step by step.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

SERVICES = ("Data (KV)", "Query (SQL++)", "Full-Text Search", "Vector Search",
            "Agent Memory", "Agent Catalog", "Semantic Cache", "Model", "App logic")


@dataclass
class Step:
    service: str          # which Couchbase service (or the model, or app code)
    what: str             # plain sentence: what the step did
    result: str = ""      # plain outcome: "4 chairs found"
    how: str = ""         # the exact operation, for the engineers in the room
    ms: float = 0.0
    collections: tuple[str, ...] = ()     # collections it read
    model: str | None = None
    tokens_spent: int = 0     # model tokens this step used (prompt + answer)
    tokens_saved: int = 0     # tokens a model call would have used, had nothing been reused
    writes: tuple[str, ...] = ()          # collections it wrote (upsert, update, delete)
    indexes: tuple[str, ...] = ()         # indexes it used: vector, full-text or SQL++
    ops: dict[str, str] = field(default_factory=dict)  # "r:coll" / "w:coll" -> operation
    tool_calls: tuple[dict[str, Any], ...] = ()   # tools the model chose: {id, name, args}
    tool_result: dict[str, Any] | None = None     # a tool's answer: {id, name, status, result}

    def op(self, rw: str, coll: str) -> str:
        """The operation a read ("r") or write ("w") of a collection was."""
        return self.ops.get(f"{rw}:{coll}") or (READ_OPS if rw == "r" else WRITE_OPS).get(
            self.service, "read" if rw == "r" else "write")


READ_OPS = {"Data (KV)": "KV get", "Query (SQL++)": "SQL++ SELECT",
            "Vector Search": "vector search", "Semantic Cache": "vector search",
            "Full-Text Search": "full-text search", "Agent Memory": "memory lookup",
            "Agent Catalog": "KV get + SQL++ SELECT", "App logic": "SQL++ SELECT"}
WRITE_OPS = {"Data (KV)": "KV upsert", "Semantic Cache": "KV upsert",
             "Agent Memory": "memory insert", "Agent Catalog": "KV upsert"}


def _split(items, rw: str, ops: dict[str, str]) -> tuple[str, ...]:
    """Collections given as "scope.coll" or ("scope.coll", "operation")."""
    out = []
    for it in items:
        if isinstance(it, (tuple, list)):
            ops[f"{rw}:{it[0]}"] = it[1]
            it = it[0]
        out.append(it)
    return tuple(out)


def index_kind(name: str) -> str:
    """vector, full-text or SQL++ (GSI), from the index's name."""
    n = name.lower()
    return ("vector" if "vec" in n or "embedding" in n else
            "full-text" if "text" in n or "fts" in n else "SQL++")


@dataclass
class Trace:
    steps: list[Step] = field(default_factory=list)
    run: str | None = None        # its run in the Agent Tracer, once logged there

    def add(self, service: str, what: str, result: str = "", how: str = "",
            ms: float = 0.0, collections: tuple[str, ...] | list[str] = (),
            model: str | None = None, *, tokens_spent: int = 0, tokens_saved: int = 0,
            writes: tuple[str, ...] | list[str] = (),
            indexes: tuple[str, ...] | list[str] = (),
            tool_calls: tuple[dict, ...] | list[dict] = (),
            tool_result: dict | None = None) -> Step:
        """`collections` are the ones read, `writes` the ones written — each a name,
        or (name, operation) when it isn't the service's usual one."""
        ops: dict[str, str] = {}
        s = Step(service, what, result, how, round(ms, 1), _split(collections, "r", ops),
                 model, int(tokens_spent), int(tokens_saved), _split(writes, "w", ops),
                 tuple(indexes), ops, tuple(tool_calls), tool_result)
        self.steps.append(s)
        return s

    @contextmanager
    def step(self, service: str, what: str, collections=(), model: str | None = None
             ) -> Iterator[Step]:
        s = Step(service, what, "", "", 0.0, tuple(collections), model)
        t0 = time.perf_counter()
        try:
            yield s
        finally:
            s.ms = round((time.perf_counter() - t0) * 1000, 1)
            self.steps.append(s)

    def by_service(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.steps:
            out[s.service] = out.get(s.service, 0) + 1
        return out

    def total_ms(self) -> float:
        return round(sum(s.ms for s in self.steps), 1)

    def tokens_spent(self) -> int:
        return sum(s.tokens_spent for s in self.steps)

    def tokens_saved(self) -> int:
        return sum(s.tokens_saved for s in self.steps)

    def as_rows(self) -> list[dict[str, Any]]:
        return [{"#": i, "What happened": s.what, "Result": s.result,
                 "Done by": s.service, "How (technical)": s.how,
                 "Reads": ", ".join(f"{c} ({s.op('r', c)})" for c in s.collections),
                 "Writes": ", ".join(f"{c} ({s.op('w', c)})" for c in s.writes),
                 "Indexes": ", ".join(s.indexes), "ms": s.ms}
                for i, s in enumerate(self.steps, 1)]


# ---- stages: the few things a request did, each holding its steps -----------
LEAD = ("Semantic Cache", "Agent Catalog", "Agent Memory", "Vector Search",
        "Full-Text Search", "Query (SQL++)", "Data (KV)", "Model", "App logic")


@dataclass
class Stage:
    kind: str                 # "model": a model call; "tool": a tool it ran; "system": the rest
    title: str
    result: str
    steps: list[Step]

    @property
    def ms(self) -> float:
        return round(sum(s.ms for s in self.steps), 1)

    @property
    def tokens_spent(self) -> int:
        return sum(s.tokens_spent for s in self.steps)

    @property
    def tokens_saved(self) -> int:
        return sum(s.tokens_saved for s in self.steps)

    @property
    def services(self) -> list[str]:
        return list(dict.fromkeys(s.service for s in self.steps))


def is_model_call(s: Step) -> bool:
    """A chat or vision model call: it spent tokens or chose tools (an embedding doesn't)."""
    return s.service == "Model" and bool(s.tokens_spent or s.tool_calls)


def _system(steps: list[Step]) -> Stage:
    lead = min(steps, key=lambda s: LEAD.index(s.service) if s.service in LEAD else 99)
    result = next((s.result for s in reversed(steps) if s.result), "")
    return Stage("system", lead.what, result, steps)


def stages(steps: list[Step]) -> list[Stage]:
    """Group a request's steps: each model call alone; each tool the model chose,
    with the steps it ran, ending at its result; the steps between them as one."""
    out: list[Stage] = []
    buf: list[Step] = []
    tool: Stage | None = None
    for s in steps:
        if tool is not None:
            tool.steps.append(s)
            if s.tool_result is not None:
                r = s.tool_result
                tool.result = s.result or str(r.get("result", ""))[:300]
                out.append(tool)
                tool = None
            continue
        if is_model_call(s):
            if buf:
                out.append(_system(buf))
                buf = []
            out.append(Stage("model", s.what, s.result, [s]))
            if s.tool_calls:
                names = ", ".join(c["name"] for c in s.tool_calls)
                tool = Stage("tool", f"Ran the tool {names}", "", [])
            continue
        buf.append(s)
    if tool is not None and tool.steps:
        out.append(tool)
    if buf:
        out.append(_system(buf))
    return out


def new() -> Trace:
    return Trace()
