"""Finding products: keyword (Full-Text Search), meaning (vector) and both.

* **Keyword** — `products_text_idx`: words, typos, facets (category, colour,
  price). "walnut desk 60" finds what has those words.
* **Meaning** — `products_vec_idx`: KNN over nomic-embed-text vectors. "my back
  hurts after a long day" finds ergonomic chairs without sharing a word.
* **Hybrid** — both, merged with reciprocal-rank fusion, so a product that is
  strong on either side surfaces, and one strong on both rises to the top.
* **Browse** — plain SQL++ on GSI for category pages, filters and sorting.
"""

from __future__ import annotations

import time
from typing import Any

from couchbase.options import SearchOptions
from couchbase.search import (
    BooleanQuery,
    ConjunctionQuery,
    DisjunctionQuery,
    MatchQuery,
    NumericRangeQuery,
    QueryStringQuery,
    SearchRequest,
    TermFacet,
    TermQuery,
)
from couchbase.vector_search import VectorQuery, VectorSearch

from config import PRODUCT_FTS_INDEX
from retail import embeddings
from retail.cb import fq, get_collection, get_scope, query
from retail.trace import Trace

PRODUCTS = "catalog.products"
_FIELDS_OUT = ("embedding",)


def _strip(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in _FIELDS_OUT}


SORT_WORDS = {"rating": "rating", "price_low": "lowest price", "price_high": "highest price",
              "name": "name"}


def _count(items) -> str:
    """'4 products (12 colour options)' from hits, keys or documents."""
    skus = set()
    for x in items:
        if isinstance(x, dict):
            skus.add(x.get("sku", ""))
        else:
            skus.add(str(x[0] if isinstance(x, tuple) else x).replace("PROD::", ""))
    bases = {s.rsplit("-", 1)[0] for s in skus if s}
    if not bases:
        return "nothing found"
    n = len(bases)
    word = "product" if n == 1 else "products"
    return f"{n} {word}" + (f" ({len(skus)} colour options)" if len(skus) > n else "")


def get_products(ids: list[str]) -> dict[str, dict[str, Any]]:
    """KV multi-get by document key (PROD::sku)."""
    if not ids:
        return {}
    res = get_collection("catalog", "products").get_multi(ids)
    out = {}
    for key, r in res.results.items():
        try:
            out[key] = _strip(r.content_as[dict])
        except Exception:
            continue
    return out


def get_product(sku: str, trace: Trace | None = None) -> dict[str, Any] | None:
    t0 = time.perf_counter()
    doc = get_products([f"PROD::{sku}"]).get(f"PROD::{sku}")
    if trace:
        trace.add("Data (KV)", "Fetched the product's details",
                  doc["base_name"] if doc else "not found",
                  f"KV get, key PROD::{sku}", (time.perf_counter() - t0) * 1000, [PRODUCTS])
    return doc


# Product-type words -> catalogue kinds. Longest phrase first, and a matched
# phrase is consumed, so "desk lamp" is a lamp and "chair mat" is a mat — not
# a desk or a chair. A search that names a product type is limited to it:
# "chairs" used to return a rug (its copy says "chair-caster friendly").
KIND_WORDS: list[tuple[str, tuple[str, ...]]] = [
    (r"monitor arms?|monitor mounts?", ("monitor_arm",)),
    (r"monitor risers?|monitor stands?|risers?", ("monitor_riser",)),
    (r"cubicles?|cubes?|workstations?", ("desk",)),
    (r"laptop stands?|laptop risers?", ("laptop_stand",)),
    (r"keyboards?(?: and mouse| & mouse)?|mouse|mice", ("keyboard_mouse",)),
    (r"headsets?|headphones?", ("headset",)),
    (r"webcams?", ("webcam", "camera")),
    (r"legal pads?|writing pads?|notepads?", ("legal_pad",)),
    (r"notebooks?", ("notebook",)),
    (r"binders?", ("binder",)),
    (r"highlighters?", ("highlighter",)),
    (r"envelopes?", ("envelopes",)),
    (r"index cards?", ("index_cards",)),
    (r"file folders?|folders?", ("folders",)),
    (r"scissors", ("scissors",)),
    (r"tape(?: dispensers?)?", ("tape_dispenser",)),
    (r"calculators?", ("calculator",)),
    (r"binder clips?|paper clips?|clips?", ("clips",)),
    (r"hole punch(?:es)?|punch(?:es)?", ("hole_punch",)),
    (r"letter trays?|paper trays?|in-?trays?", ("letter_tray",)),
    (r"coat racks?|coat stands?|hat stands?", ("coat_rack",)),
    (r"plants?|greenery", ("plant",)),
    (r"coffee makers?|coffee machines?", ("coffee_maker",)),
    (r"chair ?mats?|floor mats?", ("chair_mat",)),
    (r"desk lamps?|floor lamps?|lamps?|lights?|lighting", ("lamp",)),
    (r"desk organi[sz]ers?|organi[sz]ers?|pen holders?", ("organizer",)),
    (r"dry[- ]erase markers?|whiteboard markers?", ("dry_erase_marker",)),
    (r"permanent markers?|sharpies?", ("permanent_marker",)),
    (r"markers?", ("dry_erase_marker", "permanent_marker")),
    (r"whiteboard cleaners?|cleaning sprays?|board cleaners?", ("whiteboard_cleaner",)),
    (r"whiteboard erasers?|erasers?", ("whiteboard_eraser",)),
    (r"whiteboards?|white boards?|dry[- ]erase boards?", ("whiteboard",)),
    (r"ink bottles?|refill ink|ink|toner|cartridges?|drum units?|drums?", ("cartridge",)),
    (r"(?:printer|copier|copy|photo) paper", ("paper",)),   # before "printer"
    (r"printers?|copiers?|scanners?", ("printer",)),
    (r"hdmi|cables?|cords?", ("hdmi_cable",)),
    (r"surge protectors?|power strips?|outlets?", ("power_strip",)),
    (r"monitors?", ("monitor",)),
    (r"tvs?|televisions?|displays?|screens?", ("tv",)),
    (r"video bars?", ("video_bar",)),
    (r"conference cameras?|cameras?", ("camera", "webcam")),
    (r"speakerphones?|soundbars?|speakers?", ("speaker",)),
    (r"side tables?|end tables?", ("side_table",)),
    (r"conference tables?|meeting tables?|boardroom tables?|tables?", ("conference_table",)),
    (r"standing desks?|desks?|workstations?", ("desk",)),
    (r"chairs?|seating|seats?|stools?", ("chair",)),
    (r"credenzas?|bookcases?|bookshel(?:f|ves)|shel(?:f|ves)|file cabinets?|filing cabinets?|"
     r"cabinets?|storage", ("storage",)),
    (r"copy paper|printer paper|photo paper|cardstock|card stock|paper|reams?", ("paper",)),
    (r"sticky notes?|post[- ]?its?", ("sticky_notes",)),
    (r"staplers?", ("stapler",)),
    (r"pens?", ("pens",)),
    (r"recycling(?: bins?)?", ("recycling_bin",)),
    (r"wastebaskets?|trash(?: cans?)?|garbage(?: cans?)?|bins?",
     ("wastebasket", "recycling_bin")),
    (r"rugs?|carpets?", ("rug",)),
]


def kinds_named(text: str) -> list[str]:
    """The product types a query names, if any."""
    import re
    low = f" {(text or '').lower()} "
    found: list[str] = []
    for pat, kinds in KIND_WORDS:              # listed most specific first
        for m in re.finditer(rf"(?<![a-z]){pat}(?![a-z])", low):
            before = low[:m.start()]
            low = low[:m.start()] + " " * (m.end() - m.start()) + low[m.end():]
            # "...at my desk", "toner for a laser printer": the place or the
            # thing it is for, not what is being shopped for.
            if re.search(r"\b(?:at|in|on|for|by|under|near|beside|with|from|into)"
                         r"(?:\s+(?:my|the|our|a|an|your|this|that))?\s+(?:[a-z-]+\s+)?$",
                         before):
                continue
            found += [k for k in kinds if k not in found]
            break
    return found


def _filters(kind: str | list[str] | None, category: str | None, color: str | None,
             max_price: float | None) -> list:
    qs = []
    kinds = [kind] if isinstance(kind, str) else list(kind or [])
    if len(kinds) == 1:
        qs.append(TermQuery(kinds[0], field="kind"))
    elif kinds:
        qs.append(DisjunctionQuery(*[TermQuery(k, field="kind") for k in kinds]))
    if category:
        qs.append(TermQuery(category, field="category"))
    if color:
        qs.append(TermQuery(color, field="color"))
    if max_price:
        qs.append(NumericRangeQuery(max=max_price, inclusive_max=True, field="price"))
    return qs


def keyword(text: str, *, limit: int = 24, kind: str | list[str] | None = None,
            category: str | None = None, color: str | None = None,
            max_price: float | None = None, trace: Trace | None = None
            ) -> tuple[list[tuple[str, float]], dict[str, list[tuple[str, int]]]]:
    """Full-text search. Returns ([(doc_id, score)], facets)."""
    t0 = time.perf_counter()
    opts = SearchOptions(limit=limit, facets={
        "category": TermFacet("category", 10), "color": TermFacet("color", 12),
        "kind": TermFacet("kind", 20)})
    # Every word exactly first ("black ergonomic chair" means all three); only if
    # that finds too little, allow typos, then any word. Typo matches score
    # oddly — with them on, "conference table" ranked file folders first.
    # A product of the kind the words name ("desk") outranks one that only
    # mentions it ("desk lamp").
    boost = [TermQuery(k, field="kind", boost=3) for k in kinds_named(text)]
    for operator, fuzz in (("and", 0), ("and", 1), ("or", 1)):
        main = (QueryStringQuery(text) if any(c in text for c in ':+-"')
                else MatchQuery(text, fuzziness=fuzz, match_operator=operator))
        q = ConjunctionQuery(main, *_filters(kind, category, color, max_price))
        if boost:
            q = BooleanQuery(must=q, should=DisjunctionQuery(*boost))
        res = get_scope("catalog").search(PRODUCT_FTS_INDEX, SearchRequest.create(q), opts)
        hits = [(r.id, float(r.score)) for r in res.rows()]
        if len(hits) >= 3 or operator == "or":
            break
    # Keep what scores near the best match; the tail is where "tables"
    # picked up HDMI cables on the word "table" in their copy.
    if hits:
        floor = hits[0][1] * 0.5
        hits = [h for h in hits if h[1] >= floor]
    facets = {}
    try:
        for name, f in (res.facets() or {}).items():
            facets[name] = [(t.term, t.count) for t in (f.terms or [])]
    except Exception:
        pass
    if trace:
        trace.add("Full-Text Search", f"Matched the words \u201c{text}\u201d in product "
                  f"names and descriptions", _count(hits),
                  f"FTS match query on {PRODUCT_FTS_INDEX} (typo-tolerant)",
                  (time.perf_counter() - t0) * 1000, [PRODUCTS], indexes=[PRODUCT_FTS_INDEX])
    return hits, facets


def semantic(text: str, *, k: int = 24, kind: str | list[str] | None = None,
             category: str | None = None, color: str | None = None,
             max_price: float | None = None, trace: Trace | None = None
             ) -> list[tuple[str, float]]:
    """Vector KNN over product embeddings, with filters pushed into the search."""
    t0 = time.perf_counter()
    vec = embeddings.embed_query(text)
    t_embed = (time.perf_counter() - t0) * 1000
    pre = _filters(kind, category, color, None)
    kwargs: dict[str, Any] = {"num_candidates": max(k * 4, 60)}
    if pre:
        kwargs["prefilter"] = ConjunctionQuery(*pre) if len(pre) > 1 else pre[0]
    vq = VectorQuery("embedding", vec, **kwargs)
    res = get_scope("catalog").search(
        "products_vec_idx", SearchRequest.create(VectorSearch.from_vector_query(vq)),
        SearchOptions(limit=k))
    hits = [(r.id, float(r.score)) for r in res.rows()]
    # KNN always returns k neighbours, however far. Keep those close to the
    # best one, and above an absolute floor.
    if hits:
        floor = max(0.55, hits[0][1] - 0.07)
        hits = [h for h in hits if h[1] >= floor]
    if max_price:
        docs = get_products([h[0] for h in hits])
        hits = [h for h in hits if (docs.get(h[0]) or {}).get("price", 0) <= max_price]
    if trace:
        trace.add("Model", "Turned the search into a meaning vector",
                  "768 numbers that capture what the words mean",
                  "nomic-embed-text via Ollama", t_embed, model="nomic-embed-text")
        trace.add("Vector Search", "Found products closest in meaning",
                  _count(hits) + (f"; closest is {hits[0][1]:.0%} similar" if hits else ""),
                  "KNN on products_vec_idx (cosine similarity)",
                  (time.perf_counter() - t0) * 1000 - t_embed, [PRODUCTS],
                  indexes=["products_vec_idx"])
    return hits


def hybrid(text: str, *, limit: int = 12, kind: str | list[str] | None = None,
           category: str | None = None, color: str | None = None,
           max_price: float | None = None, trace: Trace | None = None
           ) -> list[dict[str, Any]]:
    """Keyword + meaning, merged by reciprocal-rank fusion (k=60).

    If the query names product types ("chairs", "printer ink"), both sides
    are limited to those kinds.
    """
    if not kind:
        named = kinds_named(text)
        if named:
            kind = named
            if trace:
                trace.add("App logic", "Recognised the type of product you asked for",
                          "Showing only " + ", ".join(k.replace("_", " ") + "s"
                                                      for k in named),
                          "word match on the query, then a filter on product kind")
    kw, _facets = keyword(text, limit=30, kind=kind, category=category, color=color,
                          max_price=max_price, trace=trace)
    sem = semantic(text, k=30, kind=kind, category=category, color=color,
                   max_price=max_price, trace=trace)
    score: dict[str, float] = {}
    why: dict[str, set[str]] = {}
    for hits, label in ((kw, "keyword"), (sem, "meaning")):
        for rank, (doc_id, _s) in enumerate(hits):
            score[doc_id] = score.get(doc_id, 0) + 1 / (60 + rank)
            why.setdefault(doc_id, set()).add(label)
    ranked = sorted(score, key=lambda d: -score[d])[:limit]
    t0 = time.perf_counter()
    docs = get_products(ranked)
    if trace:
        trace.add("App logic", "Combined both lists into one ranking",
                  f"{_count(ranked)} in the final list",
                  "reciprocal-rank fusion of the keyword and vector rankings")
        trace.add("Data (KV)", "Fetched the matching products' details",
                  _count(list(docs.values())), "KV multi-get by document key",
                  (time.perf_counter() - t0) * 1000, [(PRODUCTS, "KV multi-get")])
    out = []
    for d in ranked:
        if d in docs:
            p = docs[d]
            p["_matched_by"] = sorted(why[d])
            out.append(p)
    return out


def browse(*, category: str | None = None, subcategory: str | None = None,
           kind: str | None = None, color: str | None = None,
           max_price: float | None = None, sort: str = "rating",
           limit: int = 48, trace: Trace | None = None) -> list[dict[str, Any]]:
    """Category pages: SQL++ over GSI with filters and sorting."""
    where, params = ["p.type = 'product'"], {}
    for col, val in (("category", category), ("subcategory", subcategory),
                     ("kind", kind), ("color", color)):
        if val:
            where.append(f"p.{col} = ${col}")
            params[col] = val
    if max_price:
        where.append("p.price <= $max_price")
        params["max_price"] = max_price
    order = {"rating": "p.rating DESC", "price_low": "p.price ASC",
             "price_high": "p.price DESC", "name": "p.name ASC"}.get(sort, "p.rating DESC")
    stmt = (f"SELECT p.* FROM {fq('catalog', 'products')} AS p WHERE "
            f"{' AND '.join(where)} ORDER BY {order} LIMIT {int(limit)}")
    t0 = time.perf_counter()
    rows = [_strip(r) for r in query(stmt, **params)]
    if trace:
        shown = ", ".join(f"{k} {v}" for k, v in params.items()) or "everything"
        trace.add("Query (SQL++)", f"Listed the catalogue ({shown}), sorted by "
                  f"{SORT_WORDS.get(sort, sort)}", _count(rows),
                  "SQL++ SELECT on catalog.products using the idx_prod_cat index",
                  (time.perf_counter() - t0) * 1000, [PRODUCTS], indexes=["idx_prod_cat"])
    return rows


def categories() -> list[dict[str, Any]]:
    return query(f"SELECT c.* FROM {fq('catalog', 'categories')} AS c ORDER BY c.`order`")


def colors() -> list[str]:
    return query(f"SELECT DISTINCT RAW p.color FROM {fq('catalog', 'products')} AS p "
                 f"WHERE p.type = 'product' ORDER BY p.color")


def compatible_cartridges(printer_base_id: str, trace: Trace | None = None
                          ) -> list[dict[str, Any]]:
    t0 = time.perf_counter()
    rows = query(f"SELECT p.* FROM {fq('catalog', 'products')} AS p "
                 f"WHERE p.kind = 'cartridge' AND ANY s IN p.compatible_printers "
                 f"SATISFIES s = $b END ORDER BY p.price", b=printer_base_id)
    if trace:
        trace.add("Query (SQL++)", "Found the ink or toner that fits this printer",
                  f"{len(rows)} cartridges",
                  "SQL++ SELECT … ANY s IN compatible_printers (array index)",
                  (time.perf_counter() - t0) * 1000, [PRODUCTS], indexes=["idx_prod_cartridge"])
    return [_strip(r) for r in rows]
