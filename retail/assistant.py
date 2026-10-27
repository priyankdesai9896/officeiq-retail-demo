"""Ask the Assistant: the chat model with real tool calling, and a semantic cache.

The chat model is given the `store_assistant` prompt and its six tools from
the Agent Catalog (help articles, products, add to cart, the cart, orders,
refunds) and decides which to call. Every call passes the policy check in
`retail.governance` first; `issue_refund` isn't granted, so it is refused.

General store questions — delivery, returns, warranty — are kept in the
semantic cache: the first time costs model calls; every later question that
means the same is answered from Couchbase with no model call. Requests about
the customer's own cart or orders never touch the shared cache.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from typing import Any

import httpx
from couchbase.options import SearchOptions
from couchbase.search import SearchRequest
from couchbase.vector_search import VectorQuery, VectorSearch

from config import settings
from retail import (agent_context, agentc_bridge, embeddings, memory, moving, room_request,
                    semantic_cache)
from retail.cb import get_collection, get_scope
from retail.trace import Trace

log = logging.getLogger(__name__)

KB = "support.kb_articles"
KB_FLOOR = 0.55          # below this no article is about the question
SAME_ARTICLE = 0.65      # a looser match counts when it leads to the same article
EXAMPLES = ["How long does delivery take?", "Can I return a desk?",
            "Do chairs come with a warranty?", "What's your return policy?"]
TOOL_EXAMPLES = ["Find a black office chair under $300", "Add a whiteboard to my cart",
                 "What's in my cart?", "Show all my orders", "Where's my latest order?",
                 "Refund my latest order", "Suggest items for what I've bought"]

# The agent: its prompt and tools are read from the Agent Catalog; this is the fallback.
AGENT_PROMPT = (
    "You are the OfficeIQ office store's assistant. Use the tools to answer; never make up "
    "products, prices, orders or policies.\n"
    "- Store policy questions (delivery, returns, warranty, payment, assembly): call "
    "search_kb. \"Can I return a desk?\" asks about the returns policy, not an order: "
    "call search_kb.\n"
    "- Finding products: call search_products. The customer sees what it finds as product "
    "cards, so don't list the products — say what you found and the price range in a "
    "sentence. To add something to the cart, call add_to_cart with what the customer "
    "named — it adds the best match, or one of the products just shown by its colour or "
    "position. Only "
    "when the customer asks to add or buy — never on your own after a search.\n"
    "- If a result has also_suggest, the customer sees it as cards: suggest it in one "
    "sentence, with its reason. It is a suggestion — not included, not in the cart.\n"
    "- When the customer tells you something about themselves to keep — a budget or price "
    "limit, colours, style, their room, a bad back — call save_preference with their "
    "words, also when they change one (\"my budget is now $2,000\"). Don't search unless "
    "they also asked for something.\n"
    "- Suggestions — from their past orders or for what you're talking about: call "
    "suggest_for_me (about = the topic, e.g. \"chair\"). Never search with empty words.\n"
    "- If asked why something was shown, say where it came from — the search words, what "
    "we remember (because_you_told_us) or a past order (from_order). Never make up a "
    "reason.\n"
    "- The customer's cart: call view_cart. Their orders: call list_orders when they ask "
    "about their orders or order history; call track_order for one order (by id, or leave "
    "order_id empty for the latest).\n"
    "- Refunds: call issue_refund (leave order_id empty for the latest order, amount_usd 0 "
    "to refund all of it). Never say a refund was issued unless issue_refund says so.\n"
    "If a tool returns an error or says it is not allowed, tell the customer plainly and "
    "suggest contacting customer care.\n"
    "Answer in two or three friendly sentences, with prices in dollars. No preamble.")
AGENT_TOOLS = ["search_kb", "search_products", "suggest_for_me", "add_to_cart", "view_cart",
               "track_order", "list_orders", "issue_refund", "save_preference"]
MAX_ROUNDS = 4
VIDEO_KINDS = ("video_bar", "camera", "speaker")      # what makes a room ready for calls
# About this customer: never answered from, or saved to, the shared cache
# "the white one", "is it comfy?": about the list just shown, so never a shared answer
FOLLOW_UP = re.compile(
    r"\b(one|ones|it|its|that|this|those|them|these|first|second|third|last|"
    r"yes|yeah|yep|sure|ok|okay|please|no|nope|go ahead|do it|sounds good|"
    r"why|how come|you (?:said|suggested|showed|added|recommended))\b", re.I)
# "why did you suggest…": about the earlier answers — no tool needed
ASKS_WHY = re.compile(r"\b(why|how come)\b", re.I)
# "I'm going to use this chair in my spare bedroom": where the products just shown will go
PLACE = re.compile(r"\b(?:use|using|put|putting|keep|place|placing|set|going|goes|go|be)\b"
                   r"[^.?!]{0,40}?\b(?:in|at|into)\b", re.I)
WHERE = re.compile(r"\b(?:in|at|for|into)\s+(?:my|our|the|a)?\s*((?:spare |guest |small |home |"
                   r"conference |meeting |board|)"
                   r"(?:bed ?room|home office|home|office|study|den|basement|garage|kitchen|"
                   r"living room|corner|room))\b", re.I)
REFERS = re.compile(r"\b(this|it|these|them|that|those|the (?:chair|desk|one|ones)|one of "
                    r"(?:them|these))\b", re.I)
PLACE_WORDS = {"bedroom", "home", "office", "study", "den", "basement", "garage", "kitchen",
               "corner", "living", "spare", "guest", "small", "conference", "meeting"}
# a price or sku in an answer that no tool gave: made up
MADE_UP = re.compile(r"\$\s?\d[\d,]*(?:\.\d\d)?|\b[A-Z]{2,4}-\d{3,4}(?:-[A-Z]{2,5})?\b")
# after a room plan: "yes", "add it all to my cart" — every item of the plan, not one
YES = re.compile(r"^\W*(?:yes|yeah|yep|sure|ok(?:ay)?|please|go ahead|do it|sounds good)\b",
                 re.I)
ADD_PLAN = re.compile(r"\b(?:add|put|buy|order|get)\b.{0,25}?\b(?:it|them|all|everything|"
                      r"these|the (?:whole |entire )?(?:plan|room|lot|list|set ?up|items))\b",
                      re.I)


def follow_up(question: str) -> bool:
    """Does this refer back — "the white one", "yes please", "why that?" — or is it
    too short to stand alone?"""
    return bool(FOLLOW_UP.search(question)) or len(question.split()) <= 2
PERSONAL = re.compile(r"\b(my|mine|cart|basket|order|orders|add|buy|refund|ord-[\w-]+)\b",
                      re.I)


def _articles(vec: list[float], trace: Trace | None) -> list[tuple[dict[str, Any], float]]:
    t0 = time.perf_counter()
    res = get_scope("support").search(
        "kb_vec_idx", SearchRequest.create(VectorSearch.from_vector_query(
            VectorQuery("embedding", vec, num_candidates=20))), SearchOptions(limit=3))
    hits = [(r.id, float(r.score)) for r in res.rows()]
    hits = [h for h in hits if h[1] >= max(KB_FLOOR, hits[0][1] - 0.08)] if hits else []
    docs = []
    for key, score in hits:
        try:
            docs.append((get_collection("support", "kb_articles").get(key).content_as[dict],
                         score))
        except Exception:
            pass
    if trace:
        trace.add("Vector Search", "Found the help articles closest in meaning",
                  ", ".join(f"{d['title']} ({s:.0%})" for d, s in docs) or "none close enough",
                  "KNN on kb_vec_idx", (time.perf_counter() - t0) * 1000,
                  [(KB, "vector search + KV get")],
                  indexes=["kb_vec_idx"])
    return docs


def kb_articles(question: str, trace: Trace | None = None) -> list[dict[str, str]]:
    """The help articles closest in meaning to a question — the search_kb tool."""
    t0 = time.perf_counter()
    vec = embeddings.embed_query(question)
    if trace:
        trace.add("Model", "Turned the tool's question into a meaning vector",
                  "768 numbers", "nomic-embed-text via Ollama",
                  (time.perf_counter() - t0) * 1000, model=settings.embedding_model)
    return [{"title": d["title"], "text": d["answer"]} for d, _ in _articles(vec, trace)]


TOOL_CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|$)", re.S)


def _qwen_prompt(messages: list[dict], tools: list[dict]) -> str:
    """The conversation in Qwen 2.5's own chat format, tools included — the format
    the model was trained to call tools in."""
    out = []
    for i, m in enumerate(messages):
        if m["role"] == "system":
            text = m["content"]
            if tools and i == 0:
                text += ("\n\n# Tools\n\nYou may call one or more functions to assist with "
                         "the user query.\n\nYou are provided with function signatures within "
                         "<tools></tools> XML tags:\n<tools>\n"
                         + "\n".join(json.dumps(t) for t in tools)
                         + "\n</tools>\n\nFor each function call, return a json object with "
                         "function name and arguments within <tool_call></tool_call> XML tags:"
                         "\n<tool_call>\n{\"name\": <function-name>, \"arguments\": "
                         "<args-json-object>}\n</tool_call>")
            out.append(f"<|im_start|>system\n{text}<|im_end|>")
        elif m["role"] == "user":
            out.append(f"<|im_start|>user\n{m['content']}<|im_end|>")
        elif m["role"] == "assistant":
            calls = "".join(f"<tool_call>\n{json.dumps({'name': c['name'], 'arguments': c['args']})}"
                            f"\n</tool_call>" for c in m.get("tool_calls") or [])
            out.append(f"<|im_start|>assistant\n{m.get('content') or ''}{calls}<|im_end|>")
        elif m["role"] == "tool":
            out.append(f"<|im_start|>user\n<tool_response>\n{m['content']}\n"
                       f"</tool_response><|im_end|>")
    return "\n".join(out) + "\n<|im_start|>assistant\n"


def _chat(messages: list[dict], tools: list[dict]) -> tuple[str, list[dict], int]:
    """One model turn -> (text, tool calls [{name, args}], tokens).

    The tool calls are read from the model's own <tool_call> blocks: Ollama
    0.40's parser drops some valid calls (a refund request came back empty) and
    rewrites others, so the raw output is the reliable record of what the
    model chose."""
    base = re.sub(r"/v1/?$", "", settings.model_endpoint)
    r = httpx.post(f"{base}/api/generate", timeout=settings.model_timeout,
                   json={"model": settings.chat_model, "raw": True, "stream": False,
                         "prompt": _qwen_prompt(messages, tools),
                         "options": {"temperature": 0, "num_ctx": 8192, "num_predict": settings.chat_max_tokens,
                                     "stop": ["<|im_end|>", "<|im_start|>"]}})
    r.raise_for_status()
    body = r.json()
    raw = body.get("response") or ""
    calls = []
    for block in TOOL_CALL.findall(raw) if tools else []:
        for line in [ln for ln in block.strip().splitlines() if ln.strip()] or [block]:
            try:
                c = json.loads(line)
            except ValueError:
                continue
            if isinstance(c, dict) and c.get("name"):
                args = c.get("arguments") or {}
                calls.append({"name": c["name"], "args": args if isinstance(args, dict) else {}})
    text = TOOL_CALL.sub("", raw).strip()
    return text, calls, int(body.get("prompt_eval_count", 0)) + int(body.get("eval_count", 0))


def _brief(v: Any, n: int = 160) -> str:
    text = v if isinstance(v, str) else json.dumps(v, default=str)
    return text if len(text) <= n else text[:n - 1] + "…"


def _history_messages(history: list[dict] | None) -> list[dict]:
    """The last few turns, oldest first, each answer with the products it showed —
    so "add the white one" can mean one of them."""
    out: list[dict] = []
    for h in history or []:
        shown = "".join(f"\n{i}. {p['name']} (sku {p['sku']}; colours: "
                        f"{', '.join(p.get('colours') or []) or p.get('color', '')})"
                        for i, p in enumerate(h.get("shown") or [], 1))
        also = "".join(f"\n- {g['name']}: {g['why']}"
                       + (f" (because they told us: “{g['memory']}”)" if g.get("memory") else "")
                       for g in h.get("suggested") or [])
        out += [{"role": "user", "content": h["q"]},
                {"role": "assistant", "content": h["answer"]}]
        if shown or also:               # a note, so the model doesn't copy it as an answer
            out.append({"role": "system", "content":
                        (f"Products shown to the customer with that answer:{shown}"
                         if shown else "")
                        + (f"\nAlso suggested, and why:{also}" if also else "")})
    return out


def run_agent(question: str, trace: Trace | None, *, customer_id: str = "GUEST",
              session_id: str | None = None, history: list[dict] | None = None
              ) -> dict[str, Any]:
    """The model decides which tools to call, round by round, then answers.
    -> {answer, tokens, ms, calls: [{name, args, status, result}]}"""
    t0 = time.perf_counter()
    system, where, funcs = agentc_bridge.agent("store_assistant", AGENT_PROMPT, AGENT_TOOLS)
    schemas = [agentc_bridge.tool_schema(f) for f in funcs.values()]
    # The turns themselves only for a follow-up ("the white one"): given them for a
    # new request, a small model repeats an earlier answer instead of calling a tool.
    past = _history_messages(history) if follow_up(question) else []
    messages: list[dict] = [{"role": "system", "content": system}, *past,
                            {"role": "user", "content": question}]
    if trace and past:
        trace.add("App logic", "Gave the model the conversation so far",
                  f"the last {len(past) // 2} question{'s' if len(past) > 2 else ''}, their "
                  "answers and the products shown",
                  "chat history from this session, newest last", 0)
    how = (f"{settings.chat_model} via Ollama · tool calling (Qwen <tool_call> format) · "
           f"prompt store_assistant · {where}")
    calls_made: list[dict] = []
    tokens, answer = 0, ""
    last_shown = next((h["shown"] for h in reversed(history or []) if h.get("shown")), [])
    ctx = agent_context.CURRENT.set({"customer_id": customer_id, "session_id": session_id,
                                     "trace": trace, "question": question,
                                     "shown": [p["sku"] for p in last_shown]})
    try:
        for rnd in range(MAX_ROUNDS + 1):
            t1 = time.perf_counter()
            last = rnd == MAX_ROUNDS                 # out of rounds: answer with what it has
            text, calls, used = _chat(messages, [] if last else schemas)
            tokens += used
            parsed = [{"id": f"call_{len(calls_made) + i + 1}", **c} for i, c in enumerate(calls)]
            if trace:
                trace.add("Model", "Chose the tools to call" if parsed else "Wrote the answer",
                          "; ".join(f"{c['name']}({', '.join(f'{k}={v!r}' for k, v in c['args'].items())})"
                                    for c in parsed) if parsed else
                          _brief(text, 300),
                          how, (time.perf_counter() - t1) * 1000, model=settings.chat_model,
                          tokens_spent=used, tool_calls=parsed)
            if not parsed and rnd == 0 and not last and not (past and ASKS_WHY.search(question)):
                # With the conversation in view, a small model may just repeat an
                # earlier answer — even "added it" without adding. Ask once more.
                messages[-1] = {"role": "user", "content": question + "\n(Use the tools "
                                "for this request" + (" — don't answer from the earlier "
                                                      "turns" if past else "") + ".)"}
                if trace:
                    trace.add("App logic", "Asked the model to use the tools",
                              "it answered without calling a tool — "
                              "it may only answer from what the tools return",
                              "one retry", 0)
                continue
            if not parsed:
                answer = re.sub(r"\s*\[Products shown[^\]]*\]", "", text).strip()
                answer = answer.replace('\\"', '"')     # JSON-escaped quotes: 36\" x 48\"
                # field names the model sometimes quotes: (because_you_told_us: "…")
                answer = re.sub(r"\(\s*because_you_told_us:?\s*([^)]*)\)", r"— you told us \1",
                                answer)
                answer = re.sub(r"\bbecause_you_told_us\b:?\s*", "you told us ", answer)
                answer = re.sub(r"\bfrom_order\b:?\s*", "from order ", answer)
                break
            messages.append({"role": "assistant", "content": text, "tool_calls": parsed})
            for c in parsed:
                t2 = time.perf_counter()
                fn = funcs.get(c["name"])
                try:
                    result = fn(**c["args"]) if fn else {"error": f"no tool {c['name']}"}
                except Exception as exc:              # bad arguments from the model, etc.
                    result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
                status = "error" if isinstance(result, dict) and "error" in result else "success"
                calls_made.append({**c, "status": status, "result": result})
                if trace:
                    trace.add("App logic", f"Ran the tool {c['name']} and gave the model its "
                              f"result", _brief(result),
                              "tool function from the Agent Catalog "
                              "(agent_catalog/tools/store_tools.py)",
                              (time.perf_counter() - t2) * 1000,
                              tool_result={"id": c["id"], "name": c["name"],
                                           "status": status, "result": result})
                messages.append({"role": "tool",
                                 "content": json.dumps(result, default=str)[:4000]})
    finally:
        agent_context.CURRENT.reset(ctx)
    if answer and not calls_made:
        # no tool ran, so any product, sku or price in it that the conversation hasn't
        # had came from the model alone — a small model invents them
        said = " ".join(f"{h.get('answer', '')} " + " ".join(p.get("sku", "") for p in
                                                            h.get("shown") or [])
                        for h in history or [])
        made_up = [x for x in MADE_UP.findall(answer) if x not in said]
        if made_up:
            if trace:
                trace.add("App logic", "Held back an answer the tools didn't back",
                          f"it named {', '.join(made_up[:3])} without looking anything up",
                          "retail/assistant.py: no product, sku or price without a tool", 0)
            answer = ("I didn't look anything up for that, so I won't guess at products or "
                      "prices. Tell me what you'd like — for example “show me chair mats” or "
                      "“what goes with this chair?”")
    return {"answer": answer or "Sorry — I couldn't work that out. Please contact customer "
                                "care.", "tokens": tokens,
            "ms": (time.perf_counter() - t0) * 1000, "calls": calls_made}


def _room_turn(question: str, history: list[dict] | None, trace: Trace | None,
               customer_id: str, session_id: str | None) -> dict[str, Any] | None:
    """A whole room to set up: ask its size first, then plan it with the Room
    Planner — what fits depends on the size. None for anything else."""
    last = (history or [{}])[-1]
    plan = last.get("room_plan") or {}
    if plan.get("items") and "?" not in question and (
            ADD_PLAN.search(question) or (YES.search(question)
                                          and len(question.split()) <= 6)):
        return _add_plan(plan, trace, customer_id)
    req = room_request.parse(question)
    fresh = req is not None                        # a new room, not a change to the last one
    pending = last.get("room_ask")
    listed = bool(moving.said_owned(question))     # "I have 2 chairs and a TV"
    if req is None and pending and pending.get("stage") == "bring":
        # the answer to "what are you bringing?": a list, or nothing at all
        got = {} if moving.NONE.search(question) else moving.owned_in(question)
        if got or moving.NONE.search(question):
            size = pending.get("size") or room_request.size_in(question)
            req = {**pending, "bring": got, "bring_asked": True,
                   "size": tuple(size) if size else None}
            listed = bool(got)
        elif len(question.split()) <= 8 and "?" not in question:
            req = {**pending, "again": True}       # not a list we can read: ask again
    elif req is None and pending and not (pending.get("stage") == "resize"
                                          and len(question.split()) > 8):
        # the answer to "how big is it?", or another size for the room just planned
        size = room_request.size_in(question)
        if size is None and room_request.UNSURE.search(question):
            size = room_request.typical(pending["room_type"], pending["people"])[1]
        if size is not None:
            more = moving.said_owned(question)     # "20 x 15 — and I have a TV too"
            req = {**pending, "size": size, "bring": {**pending.get("bring", {}), **more}}
        elif (pending.get("current") and "?" not in question
              and room_request.video_in(question, toggle=True) is not None):
            # "no video conference needed": the same room, re-planned with or without it
            req = {**pending, "size": tuple(pending["current"])}
    if req is None:
        return None
    said_video = room_request.video_in(question, toggle=not fresh)
    video = said_video if said_video is not None else req.get("video")
    t0 = time.perf_counter()
    kind, people, size = req["room_type"], req["people"], req["size"]
    bring = dict(req.get("bring") or {})
    if listed:                                     # what's in the current room: remember it
        _remember_current_room(customer_id, session_id, bring, trace)
    if req.get("moving") and not bring and not req.get("bring_asked"):
        # moving, and no list yet: ask what's coming before planning anything
        known = _current_room(customer_id)
        if trace:
            trace.add("App logic", "Asked what they're bringing from the current room",
                      "moving to a new room — what to buy depends on what comes with them"
                      + (f"; offered the room we remember: {known[1]}" if known else ""),
                      "retail/assistant.py: no model call", 0)
        semantic_cache.tally(hit=False, tokens=0, ms=(time.perf_counter() - t0) * 1000)
        words = moving.words(known[0]) if known else None
        answer = room_request.ask_bring(kind, words)
        if req.get("again"):
            answer = ("I couldn't make out a list there — try something like \"2 chairs, "
                      "a desk and a TV\", or pick \"Nothing — all new\". " + answer)
        return {"answer": answer, "cached": False, "score": None, "matched": None,
                "tokens": 0, "ms": (time.perf_counter() - t0) * 1000, "saved_ms": 0,
                "sources": [],
                "room_ask": {"stage": "bring", "room_type": kind, "people": people,
                             "size": list(size) if size else None, "bring": {},
                             "moving": True, "remembered": words}}
    typical = None
    if size is None and room_request.needs_size(kind):
        # no size given: plan a typical one now; buttons under the plan re-plan it
        size = typical = tuple(room_request.typical(kind, people)[1])
        if trace:
            trace.add("App logic", "Planned at a typical size — no size given",
                      f"{room_request.label(kind)} for {people}: about {size[0]} × "
                      f"{size[1]} ft; other sizes offered under the plan",
                      "retail/room_request.py: typical sizes, no model call", 0)
    from retail import planner, search
    if size is None:                               # cubicles: sized by headcount
        length, width = planner.cubicle_room(people)
    else:
        length, width = size[0] * 12, size[1] * 12
    h = ({"used": []} if customer_id in (None, "GUEST")
         else planner.room_hints(customer_id, search.colors(), trace=trace))
    h = planner.for_room(h, kind)                  # at work: colours and workplace memories
    told_video = None
    if video is None and kind in planner.MEETING_ROOMS and "video_calls" in h:
        video, told_video = h["video_calls"], h["from"].get("video_calls")
    # the same settings the Room Planner takes from memory, so "Open it" shows this plan
    spec = planner.RoomSpec(kind, length, width, people, color=h.get("color"),
                            chair_color=h.get("chair_color"), carpet=bool(h.get("carpet")),
                            monitors=int(h.get("monitors", 0)),
                            standing=bool(h.get("standing")),
                            ergonomic=bool(h.get("ergonomic")),
                            min_desk_w=h.get("min_desk_w", 0), max_desk_w=h.get("max_desk_w", 0),
                            printer=h.get("printer"), budget=h.get("budget"),
                            max_item=h.get("max_item"), video_calls=video)
    args = {"room_type": kind, "length_ft": round(length / 12), "width_ft": round(width / 12),
            "people": people}
    if video is not None:
        args["video_calls"] = video
    if bring:
        args["bring"] = moving.words(bring)
    try:
        p = planner.plan(spec, customer_id=customer_id, session_id=session_id, trace=trace)
    except PermissionError as exc:
        return {"answer": f"Sorry — I can't plan rooms for you right now ({exc}).",
                "cached": False, "score": None, "matched": None, "tokens": 0,
                "ms": (time.perf_counter() - t0) * 1000, "saved_ms": 0, "sources": [],
                "tools": [{"name": "plan_room", "args": args, "status": "error",
                           "refused": True, "by_app": True}]}
    resize = ({"stage": "resize", "room_type": kind, "people": people,
               "sizes": room_request.typical(kind, people),
               "current": [args["length_ft"], args["width_ft"]], "bring": bring,
               "moving": bool(req.get("moving")), "bring_asked": True, "video": video}
              if room_request.needs_size(kind) else None)
    lead = (f"You didn't give the size, so I planned a typical {typical[0]} × {typical[1]} ft "
            f"{room_request.label(kind)} — pick another size below if yours is different. "
            if typical else "")
    if said_video is not None and not fresh and req.get("video") != said_video:
        lead = f"Re-planned {'with' if video else 'without'} video calls. " + lead
        if trace:
            trace.add("App logic", f"Re-planned the same room {'with' if video else 'without'} "
                      "video calls", f"“{question}”", "retail/room_request.py: no model call", 0)
    if bring:
        out = _moving_answer(p, h, bring, args, t0, trace)
        out["answer"] = lead + out["answer"]
        if resize:
            out["room_ask"] = resize
        return out
    big = sorted(p.lines, key=lambda ln: -ln.product["price"] * ln.qty)[:4]
    names = [f"{ln.qty} × {ln.product.get('base_name') or ln.product['name']}"
             if ln.qty > 1 else (ln.product.get("base_name") or ln.product["name"])
             for ln in big]
    units = sum(ln.qty for ln in p.lines)
    pr = p.pricing
    answer = lead + p.headline("ft") + " "
    if p.fit_note("ft"):
        answer += p.fit_note("ft") + " "
    answer += (f"Here's what it needs — {units} items, led by {', '.join(names[:-1])} and "
               f"{names[-1]}: ${pr.get('total', 0):,.2f}"
               + (f" with ${pr['discount']:,.2f} in bundle savings" if pr.get("discount") else "")
               + ".")
    av = [ln.product.get("base_name") or ln.product["name"] for ln in p.lines
          if ln.product["kind"] in VIDEO_KINDS]
    if av:
        answer += (f" It's ready for video calls: {', '.join(av[:-1])} and {av[-1]}."
                   if len(av) > 1 else f" It's ready for video calls: {av[0]}.")
        if told_video:
            answer = answer[:-1] + f" — you told us: “{told_video}”."
    elif video is False:
        answer += " No camera or speakerphone — you don't need video calls."
    liked = [w for w, k in (("colour", "color"), ("chair colour", "chair_color")) if h.get(k)]
    if liked:
        answer += f" I used the {' and '.join(liked)} you told us you like."
    if fitted := planner.budget_words(p):
        answer += f" {fitted}"
    answer += (f" Want me to add all {units} items to your cart? Or open it in the Room "
               "Planner to see the layout, change quantities or swap anything.")
    semantic_cache.tally(hit=False, tokens=0, ms=(time.perf_counter() - t0) * 1000)
    return {"answer": answer, "cached": False, "score": None, "matched": None, "tokens": 0,
            "ms": (time.perf_counter() - t0) * 1000, "saved_ms": 0, "sources": [],
            "products": list(dict.fromkeys(ln.product["sku"] for ln in p.lines)),
            "room_plan": {**args, "plan_id": p.plan_id, "units": units,
                          "items": [[ln.product["sku"], ln.qty] for ln in p.lines]},
            "room_ask": resize,
            "tools": [{"name": "plan_room", "args": args, "status": "success",
                       "by_app": True}]}


def _add_plan(plan: dict[str, Any], trace: Trace | None, customer_id: str) -> dict[str, Any]:
    """"Yes" to the room just planned: every item to buy goes in the cart at once."""
    from retail import commerce
    t0 = time.perf_counter()
    items = [(str(sku), int(qty)) for sku, qty in plan["items"] if int(qty) > 0]
    units = sum(q for _, q in items)
    if trace:
        trace.add("App logic", "Added the whole room plan to the cart",
                  f"{units} items from plan {plan.get('plan_id') or '(this answer)'}",
                  "retail/assistant.py: the plan's own list, no model call", 0)
    commerce.add_many(customer_id, items, trace=trace)
    cart = commerce.view(customer_id, trace=trace)
    in_cart = sum(int(ln["qty"]) for ln in cart["lines"])
    answer = (f"Added all {units} items from the "
              f"{plan['length_ft']} × {plan['width_ft']} ft {room_request.label(plan['room_type'])}"
              f" plan to your cart. Your cart now has {in_cart} item"
              f"{'s' if in_cart != 1 else ''} — ${cart.get('total', 0):,.2f}"
              + (f" with ${cart['discount']:,.2f} in bundle savings" if cart.get("discount")
                 else "") + ". Open the Cart to check out.")
    semantic_cache.tally(hit=False, tokens=0, ms=(time.perf_counter() - t0) * 1000)
    return {"answer": answer, "cached": False, "score": None, "matched": None, "tokens": 0,
            "ms": (time.perf_counter() - t0) * 1000, "saved_ms": 0, "sources": [],
            "tools": [{"name": "add_to_cart", "args": {"plan": plan.get("plan_id"),
                                                       "items": units},
                       "status": "success", "by_app": True}]}


def _place_turn(question: str, history: list[dict] | None, trace: Trace | None,
                customer_id: str) -> dict[str, Any] | None:
    """"I'm going to use this chair in my spare bedroom": what we remember about that
    place decides what goes with it — a mat for a carpeted floor. None for anything else."""
    shown = (history or [{}])[-1].get("shown") or []
    # the most specific place said: "at home in my spare bedroom" -> spare bedroom
    m = max(WHERE.finditer(question), key=lambda x: (x.group(1).lower() not in ("home", "room"),
                                                     x.start()), default=None)
    if not (shown and m and PLACE.search(question) and REFERS.search(question) and "?" not in question
            and customer_id not in (None, "GUEST")):
        return None
    from retail import companions, search
    t0 = time.perf_counter()
    place = " ".join(m.group(1).lower().split())
    docs = search.get_products([f"PROD::{p['sku']}" for p in shown])
    focus = [docs[f"PROD::{p['sku']}"] for p in shown if f"PROD::{p['sku']}" in docs]
    named = next((k for k in ("chair", "desk") if re.search(rf"\b{k}\b", question, re.I)), None)
    focus = [p for p in focus if not named or p.get("kind") == named][:1] or focus[:1]
    if not focus:
        return None
    h = companions.hints(customer_id, trace=trace)
    if re.search(r"conference|meeting|board", place):   # at work: not their home's carpet
        from retail import planner
        h = planner.for_room(h, "conference room")
    words = set(re.findall(r"[a-z]+", place)) & PLACE_WORDS
    about = [t for t in h.get("used", []) if words & set(re.findall(r"[a-z]+", t.lower()))]
    gw = companions.related(focus, h, customer_id, trace=trace, from_orders=False)
    if trace:
        trace.add("App logic", f"Where it'll go: your {place}",
                  (f"what we remember about it: {'; '.join(about)}" if about
                   else "nothing remembered about that place")
                  + (f" → {', '.join(g['product']['name'] for g in gw)}" if gw else ""),
                  "retail/assistant.py: Agent Memory + goes-with rules, no model call",
                  (time.perf_counter() - t0) * 1000)
    name = focus[0].get("base_name") or focus[0]["name"]
    answer = f"Got it — the {name} is for your {place}."
    if gw:
        answer += " From what you've told us: " + "; ".join(
            g["why"][0].lower() + g["why"][1:] + (f" (you told us: “{g['memory']}”)"
                                                  if g.get("memory") else "")
            for g in gw) + ". It's below — add it with its button if you'd like it."
    elif about:
        answer += (f" You told us: “{about[0]}” — nothing more it needs for that. "
                   "Want me to add it to your cart?")
    else:
        answer += " Nothing else it needs from what you've told us. Want me to add it to your cart?"
    semantic_cache.tally(hit=False, tokens=0, ms=(time.perf_counter() - t0) * 1000)
    return {"answer": answer, "cached": False, "score": None, "matched": None, "tokens": 0,
            "ms": (time.perf_counter() - t0) * 1000, "saved_ms": 0, "sources": [],
            "products": [focus[0]["sku"]],
            "goes_with": [{"sku": g["product"]["sku"], "why": g["why"], "memory": g.get("memory"),
                           "from_order": g.get("from_order")} for g in gw],
            "tools": [{"name": "suggest_for_me", "args": {"about": place}, "status": "success",
                       "by_app": True}]}


def _current_room(customer_id: str) -> tuple[dict[str, int], str] | None:
    """The current room Agent Memory holds for them — offered, never assumed."""
    if customer_id in (None, "GUEST"):
        return None
    try:
        return moving.from_memories([str(r.get("text") or "")
                                     for r in memory.list_for(customer_id, 50)])
    except Exception as exc:                       # ask without it
        log.warning("could not read the current room for %s: %s", customer_id, exc)
        return None


def _remember_current_room(customer_id: str, session_id: str | None, bring: dict[str, int],
                           trace: Trace | None) -> None:
    """They told us what's in their current room: Agent Memory keeps one such
    sentence, so the new one replaces the old."""
    if customer_id in (None, "GUEST"):
        return
    t0 = time.perf_counter()
    text = moving.memory_text(bring)
    try:
        old = [r["memory_id"] for r in memory.list_for(customer_id, 50)
               if moving.CURRENT.search(str(r.get("text") or ""))]
        row = memory.remember(customer_id, text, kind="fact", source="conversation",
                              session_id=session_id or "default")
        old = [m for m in old if m != row.get("memory_id")]
        if old:
            memory.forget(customer_id, old)
    except Exception as exc:                       # the plan still works without it
        log.warning("could not remember the current room for %s: %s", customer_id, exc)
        return
    if trace:
        trace.add("Agent Memory", "Remembered your current room", text,
                  "remember() on the Agent Memory server" + (", replacing the old one"
                                                              if old else ""),
                  (time.perf_counter() - t0) * 1000, writes=["agentmemory.memory"],
                  indexes=["AgentMemory_BlockEmbeddingIndex"])


def _moving_answer(p: Any, h: dict[str, Any], bring: dict[str, int], args: dict[str, Any],
                   t0: float, trace: Trace | None) -> dict[str, Any]:
    """Moving rooms: what to bring from the current one, and only the rest to buy."""
    from retail import planner
    d = moving.delta(p.lines, bring)
    buy = [dataclasses.replace(ln, qty=d.buy(ln.product["sku"], ln.qty)) for ln in p.lines]
    buy = [ln for ln in buy if ln.qty]
    full = p.pricing.get("total", 0)
    pr = planner.reprice(p, lines=buy)
    if trace:
        trace.add("App logic", "Worked out what you can bring",
                  f"bring {d.pieces} of {sum(bring.values())} pieces · buy "
                  f"{sum(ln.qty for ln in buy)} · ${pr.get('total', 0):,.2f} instead of "
                  f"${full:,.2f}", "retail/moving.py: the plan's lines, kind by kind, less "
                  "what you have", 0)
    brought, buying = moving.summary(p.lines, d)
    name = room_request.label(p.spec.room_type)
    answer = p.headline("ft") + " "
    if p.fit_note("ft"):
        answer += p.fit_note("ft") + " "
    answer += f"Bring your {brought}. " if d.pieces else "Nothing you have is needed there. "
    if buy:
        answer += (f"For the new {args['length_ft']} × {args['width_ft']} ft {name} you'll "
                   f"also need {buying}"
                   + (f" and {len(buy) - 4} more" if len(buy) > 4 else "")
                   + f" — ${pr.get('total', 0):,.2f} instead of ${full:,.2f} for everything new"
                   + (f", about ${d.value:,.0f} saved by bringing yours" if d.value else "")
                   + ".")
    else:
        answer += "You have everything this room needs — nothing to buy."
    if d.spare:
        answer += f" Not needed in the new room: your {moving.words(d.spare, yours=True)}."
    if fitted := planner.budget_words(p):
        answer += f" {fitted}"
    units = sum(ln.qty for ln in buy)
    answer += ((f" Want me to add the {units} item{'s' if units != 1 else ''} to buy to your "
                "cart? Or open" if buy else " Open")
               + " it in the Room Planner to see the layout and change what you bring or buy.")
    semantic_cache.tally(hit=False, tokens=0, ms=(time.perf_counter() - t0) * 1000)
    return {"answer": answer, "cached": False, "score": None, "matched": None, "tokens": 0,
            "ms": (time.perf_counter() - t0) * 1000, "saved_ms": 0, "sources": [],
            "products": list(dict.fromkeys(ln.product["sku"] for ln in buy)),
            "room_plan": {**{k: v for k, v in args.items() if k != "bring"},
                          "plan_id": p.plan_id, "bring": bring, "units": units,
                          "items": [[ln.product["sku"], ln.qty] for ln in buy]},
            "tools": [{"name": "plan_room", "args": args, "status": "success",
                       "by_app": True}]}


def ask(question: str, trace: Trace | None = None, *, customer_id: str = "GUEST",
        session_id: str | None = None, history: list[dict] | None = None
        ) -> dict[str, Any]:
    """-> {answer, cached, score, matched, tokens, ms, saved_ms, sources, tools}"""
    room = _room_turn(question, history, trace, customer_id, session_id)
    if room is not None:
        return room
    place = _place_turn(question, history, trace, customer_id)
    if place is not None:
        return place
    t0 = time.perf_counter()
    follow = bool(history) and follow_up(question)
    personal = PERSONAL.search(question) or (
        (FOLLOW_UP.search(question) or re.search(r"\w+", question)) if follow else None)
    hit, vec, docs = None, None, []
    if personal:
        if trace:
            trace.add("App logic", "A request about this customer — skipped the shared cache",
                      f"it mentions “{personal.group(0)}”",
                      "answers about a customer's cart or orders are never read from, or "
                      "saved to, the semantic cache", 0)
    else:
        near, vec = semantic_cache.lookup(question, trace=trace)
        docs = _articles(vec, trace)       # no tokens: a vector search
        if near:
            score, same = near["score"], bool(docs) and docs[0][0]["title"] in near.get(
                "sources", [])[:1]
            if score >= settings.cache_threshold or (same and score >= SAME_ARTICLE):
                hit = near
            if trace:
                trace.add("App logic", "Same question as one already answered?",
                          ("Cache HIT — " if hit else "Cache MISS — ")
                          + (f"{score:.0%} similar" + (", and it leads to the same help "
                                                       "article" if same else ""))
                          + f" (a hit needs {settings.cache_threshold:.0%}, or "
                            f"{SAME_ARTICLE:.0%} and the same article)",
                          "semantic cache policy", 0,
                          tokens_saved=sum((hit.get("tokens") or {}).values()) if hit else 0)
    if hit:
        tokens = sum((hit.get("tokens") or {}).values())
        ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        semantic_cache.count_hit(hit)
        saved_ms = max(0.0, hit.get("generate_ms", 0) - ms)
        semantic_cache.tally(hit=True, tokens=tokens, ms=ms, saved_ms=saved_ms)
        if trace:
            trace.add("Data (KV)", "Counted the reuse and the tokens it saved",
                      f"+1 hit on the cached answer, +{tokens:,} tokens saved in the tally",
                      f"SQL++ UPDATE … USE KEYS {hit['id']}; KV get + upsert, key "
                      f"{semantic_cache.TALLY}", (time.perf_counter() - t1) * 1000,
                      [semantic_cache.TALLY_COLLECTION],
                      writes=[(semantic_cache.COLLECTION, "SQL++ UPDATE"),
                              semantic_cache.TALLY_COLLECTION])
        return {"answer": hit["answer"], "cached": True, "score": hit["score"],
                "matched": hit["canonical_question"], "tokens": tokens, "ms": ms,
                "saved_ms": saved_ms, "sources": hit.get("sources", [])}
    r = run_agent(question, trace, customer_id=customer_id, session_id=session_id,
                  history=history)
    answer, n, gen_ms, calls = r["answer"], r["tokens"], r["ms"], r["calls"]
    used = {c["name"] for c in calls}
    sources = [d["title"] for d, _ in docs]
    if vec is not None and used == {"search_kb"} and all(c["status"] == "success"
                                                        for c in calls):
        semantic_cache.store(question, vec, answer, tokens={"prompt": n, "completion": 0},
                             ms=gen_ms, sources=sources, trace=trace)
    elif trace:
        trace.add("App logic", "Not saved to the semantic cache",
                  "it used " + (", ".join(sorted(used)) if used else "no tools")
                  + " — only answers from the help articles alone are shared",
                  "semantic cache policy", 0)
    t1 = time.perf_counter()
    semantic_cache.tally(hit=False, tokens=n, ms=gen_ms)
    if trace:
        trace.add("Data (KV)", "Added the tokens spent to the running tally",
                  f"+{n:,} tokens spent", f"KV get + upsert, key {semantic_cache.TALLY}",
                  (time.perf_counter() - t1) * 1000, [semantic_cache.TALLY_COLLECTION],
                  writes=[semantic_cache.TALLY_COLLECTION])
    return {"answer": answer, "cached": False, "score": None, "matched": None, "tokens": n,
            "ms": (time.perf_counter() - t0) * 1000, "saved_ms": 0, "sources": sources,
            "orders": next((c["result"]["orders"] for c in reversed(calls)
                            if c["name"] == "list_orders" and isinstance(c["result"], dict)
                            and c["result"].get("orders")), []),
            "products": [x["sku"] for c in calls if c["name"] == "search_products"
                         and isinstance(c["result"], dict)
                         for x in c["result"].get("products", [])],
            "goes_with": list({g["sku"]: {"sku": g["sku"], "why": g["why"],
                                          "memory": g.get("because_you_told_us"),
                                          "from_order": g.get("from_order")}
                               for c in calls if isinstance(c["result"], dict)
                               for g in (c["result"].get("also_suggest", [])
                                         + (c["result"].get("products", [])
                                            if c["name"] == "suggest_for_me" else []))
                               }.values()),
            "tools": [{"name": c["name"], "args": c["args"], "status": c["status"],
                       "refused": isinstance(c["result"], dict) and "policy" in c["result"],
                       "skipped": isinstance(c["result"], dict) and (
                           "not_added" in c["result"] or "not_saved" in c["result"]),
                       "saved": isinstance(c["result"], dict) and bool(
                           c["result"].get("saved") or c["result"].get("updated")),
                       "updated": isinstance(c["result"], dict) and bool(
                           c["result"].get("updated"))}
                      for c in calls]}
