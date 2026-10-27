#!/usr/bin/env python3
"""Load data/*.json into the `retail` bucket, embedding what is searched by meaning,
then write the tool registry and policy (`aidp.agent_catalog`).

Products and knowledge-base articles get a 768-d `embedding` (nomic-embed-text,
document side). Everything else is plain JSON. Idempotent: keys are stable.

    python scripts/load_data.py
    python scripts/load_data.py --only products
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DATA_DIR  # noqa: E402
from retail import embeddings  # noqa: E402
from retail.cb import fq, get_collection, query  # noqa: E402

# name -> (scope, collection, key template, text to embed or None)
SPECS = {
    "products": ("catalog", "products", "PROD::{sku}", lambda d: d["search_text"]),
    "categories": ("catalog", "categories", "CAT::{category}", None),
    "offers": ("catalog", "offers", "OFFER::{offer_id}", None),
    "room_rules": ("planning", "room_rules", "RULE::{rule_id}", None),
    "kb_articles": ("support", "kb_articles", "KB::{article_id}",
                    lambda d: f"{d['title']}. {' '.join(d['question_variants'])}. {d['answer']}"),
    "customers": ("commerce", "customers", "CUST::{customer_id}", None),
}


def load(name: str) -> int:
    scope, coll, key_tpl, text_of = SPECS[name]
    docs = json.loads((DATA_DIR / f"{name}.json").read_text())
    if text_of:
        vecs = embeddings.embed_documents([text_of(d) for d in docs])
        for d, v in zip(docs, vecs, strict=True):
            d["embedding"] = v
    c = get_collection(scope, coll)
    keys = {key_tpl.format(**d) for d in docs}
    for d in docs:
        c.upsert(key_tpl.format(**d), d)
    # anything no longer in the data (a product dropped from the catalogue) goes too
    prefix = key_tpl.split("{")[0]
    stale = [k for k in query(f"SELECT RAW META(d).id FROM {fq(scope, coll)} AS d "
                              f"WHERE META(d).id LIKE $p", consistent=True, p=prefix + "%")
             if k not in keys]
    for k in stale:
        c.remove(k)
    if stale:
        print(f"  removed {len(stale)} from {scope}.{coll}: {', '.join(sorted(stale))}")
    return len(docs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=list(SPECS))
    args = ap.parse_args()
    print(f"Embeddings: {embeddings.describe()}")
    for name in args.only or list(SPECS):
        t = time.time()
        n = load(name)
        print(f"  {name:<12} {n:>4} docs  {time.time() - t:5.1f}s")
    if not args.only:
        from retail import governance
        t = time.time()
        n = governance.seed()
        print(f"  {'tool policy':<12} {n + 1:>4} docs  {time.time() - t:5.1f}s")


if __name__ == "__main__":
    main()
