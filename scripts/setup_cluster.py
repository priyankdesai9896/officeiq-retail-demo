#!/usr/bin/env python3
"""Provision the local cluster for the retail demo. Idempotent.

Creates the `retail` bucket, its scopes and collections, the GSI indexes behind
every SQL++ query the app runs, one full-text index over product text (with
facets), and four vector indexes.

    python scripts/setup_cluster.py            # create everything
    python scripts/setup_cluster.py --verify   # report only
    python scripts/setup_cluster.py --drop-search   # rebuild the search indexes
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from couchbase.exceptions import (  # noqa: E402
    CollectionAlreadyExistsException,
    CouchbaseException,
    QueryIndexAlreadyExistsException,
    ScopeAlreadyExistsException,
)
from couchbase.management.search import SearchIndex  # noqa: E402

from config import COLLECTIONS, PRODUCT_FTS_INDEX, VECTOR_INDEXES, settings  # noqa: E402
from retail.cb import fq, get_bucket, get_cluster, get_scope, query  # noqa: E402

MGMT = os.getenv("CB_MGMT_URL", "http://127.0.0.1:8091")


# --------------------------------------------------------------------------- #
# bucket, scopes, collections
# --------------------------------------------------------------------------- #
def ensure_bucket() -> None:
    auth = (settings.username, settings.password)
    r = httpx.get(f"{MGMT}/pools/default/buckets/{settings.bucket}", auth=auth)
    if r.status_code == 200:
        print(f"  = bucket  {settings.bucket} exists")
        return
    r = httpx.post(f"{MGMT}/pools/default/buckets", auth=auth, data={
        "name": settings.bucket, "ramQuota": settings.bucket_ram_mb,
        "bucketType": "couchbase", "flushEnabled": 0})
    r.raise_for_status()
    print(f"  + bucket  {settings.bucket} ({settings.bucket_ram_mb} MB)")
    for _ in range(60):
        if httpx.get(f"{MGMT}/pools/default/buckets/{settings.bucket}",
                     auth=auth).status_code == 200:
            break
        time.sleep(1)
    time.sleep(5)


def ensure_collections() -> None:
    mgr = get_bucket().collections()
    for scope, colls in COLLECTIONS.items():
        try:
            mgr.create_scope(scope)
            print(f"  + scope   {scope}")
        except ScopeAlreadyExistsException:
            pass
        for coll in colls:
            try:
                mgr.create_collection(scope, coll)
                print(f"  + coll    {scope}.{coll}")
            except CollectionAlreadyExistsException:
                pass
    time.sleep(3)


# --------------------------------------------------------------------------- #
# GSI
# --------------------------------------------------------------------------- #
def gsi_statements() -> list[str]:
    p, o = fq("catalog", "products"), fq("catalog", "offers")
    c = fq("commerce", "carts")
    out = []
    # A primary index on every collection keeps ad-hoc SQL++ (and the MCP
    # server's queries) working; the named ones serve the app's own queries.
    for scope, colls in COLLECTIONS.items():
        for coll in colls:
            out.append(f"CREATE PRIMARY INDEX IF NOT EXISTS ON {fq(scope, coll)}")
    out += [
        f"CREATE INDEX IF NOT EXISTS idx_prod_cat ON {p}(category, subcategory, price)",
        f"CREATE INDEX IF NOT EXISTS idx_prod_kind ON {p}(kind, price, color)",
        f"CREATE INDEX IF NOT EXISTS idx_prod_base ON {p}(base_id, color)",
        f"CREATE INDEX IF NOT EXISTS idx_prod_cartridge ON {p}"
        f"(DISTINCT ARRAY s FOR s IN compatible_printers END) WHERE kind = 'cartridge'",
        f"CREATE INDEX IF NOT EXISTS idx_offer_active ON {o}(active, kind)",
        f"CREATE INDEX IF NOT EXISTS idx_cart_customer ON {c}(customer_id, status)",
        f"CREATE INDEX IF NOT EXISTS idx_order_customer ON {fq('commerce', 'orders')}"
        f"(customer_id, created_at DESC)",
        f"CREATE INDEX IF NOT EXISTS idx_plan_customer ON {fq('planning', 'room_plans')}"
        f"(customer_id, created_at DESC)",
        f"CREATE INDEX IF NOT EXISTS idx_kb_cat ON {fq('support', 'kb_articles')}(category)",
        f"CREATE INDEX IF NOT EXISTS idx_cache_ns ON {fq('aidp', 'semantic_cache')}"
        f"(`namespace`, cache_id)",
        f"CREATE INDEX IF NOT EXISTS idx_catalog_type ON {fq('aidp', 'agent_catalog')}"
        f"(type, tool_name, version)",
        f"CREATE INDEX IF NOT EXISTS idx_audit_session ON {fq('aidp', 'audit_log')}"
        f"(session_id, ts DESC)",
        # the Agent Tracer page: one row per run (agentc creates the logs collection;
        # this runs again after `agentc init`)
        f"CREATE INDEX IF NOT EXISTS idx_logs_run ON {fq('agent_activity', 'logs')}"
        f"(annotations.run, span.session, `timestamp`) WHERE annotations.run IS NOT MISSING",
    ]
    return out


def ensure_gsi() -> None:
    for stmt in gsi_statements():
        name = stmt.split("ON")[0].replace("CREATE", "").replace("IF NOT EXISTS", "").strip()
        try:
            query(stmt)
            print(f"  + gsi     {name[:60]}")
        except QueryIndexAlreadyExistsException:
            pass
        except CouchbaseException as exc:
            print(f"  ! gsi     {name[:60]}: {str(exc)[:120]}")


# --------------------------------------------------------------------------- #
# Search: vector indexes + one full-text product index
# --------------------------------------------------------------------------- #
# Exact-term fields (keyword analyzer) so prefilters and facets match whole
# values: with the English analyzer "private::RET-1001" splits into pieces.
KEYWORD_FIELDS = {"namespace", "type", "kind", "category", "subcategory", "color",
                  "style", "brand", "tool_name"}

VECTOR_EXTRA_FIELDS = {
    "products_vec_idx": ["name", "kind", "category", "color", "style"],
    "kb_vec_idx": ["title", "category"],
    "cache_vec_idx": ["canonical_question", "namespace"],
    "tools_vec_idx": ["tool_name", "type"],
}


def _text_field(name: str, *, docvalues: bool = False) -> dict:
    return {"enabled": True, "dynamic": False, "fields": [{
        "name": name, "type": "text", "index": True, "store": True,
        "include_in_all": True, "docvalues": docvalues,
        "analyzer": "keyword" if name in KEYWORD_FIELDS else "en"}]}


def _params(scope: str, collection: str, properties: dict) -> dict:
    return {
        "doc_config": {"docid_prefix_delim": "", "docid_regexp": "",
                       "mode": "scope.collection.type_field", "type_field": "type"},
        "mapping": {
            "default_analyzer": "en",   # same as the text fields, so _all queries stem too
            "default_datetime_parser": "dateTimeOptional",
            "default_field": "_all",
            "default_mapping": {"dynamic": False, "enabled": False},
            "default_type": "_default",
            "docvalues_dynamic": False, "index_dynamic": False, "store_dynamic": False,
            "type_field": "_type",
            "types": {f"{scope}.{collection}": {
                "dynamic": False, "enabled": True, "properties": properties}},
        },
        "store": {"indexType": "scorch", "segmentVersion": 16},
    }


def vector_params(index_name: str, scope: str, collection: str) -> dict:
    props = {"embedding": {"enabled": True, "dynamic": False, "fields": [{
        "name": "embedding", "type": "vector", "index": True, "store": False,
        "dims": settings.embedding_dims, "similarity": "dot_product",
        "vector_index_optimized_for": "recall"}]}}
    for f in VECTOR_EXTRA_FIELDS[index_name]:
        props[f] = _text_field(f)
    return _params(scope, collection, props)


def product_text_params() -> dict:
    """Keyword search over the catalogue, with facets for the shop's filters."""
    props = {f: _text_field(f) for f in ("name", "description", "brand", "features",
                                         "material")}
    for f in ("category", "subcategory", "color", "style", "kind", "brand"):
        props[f] = _text_field(f, docvalues=True)
    props["price"] = {"enabled": True, "dynamic": False, "fields": [{
        "name": "price", "type": "number", "index": True, "store": True,
        "docvalues": True}]}
    props["rating"] = {"enabled": True, "dynamic": False, "fields": [{
        "name": "rating", "type": "number", "index": True, "store": True,
        "docvalues": True}]}
    return _params("catalog", "products", props)


def _upsert_search(scope: str, name: str, params: dict, drop_first: bool) -> None:
    mgr = get_scope(scope).search_indexes()
    if drop_first:
        try:
            mgr.drop_index(name)
            print(f"  - search  {scope}/{name} dropped")
            time.sleep(2)
        except CouchbaseException:
            pass
    idx = SearchIndex(name=name, source_type="gocbcore", idx_type="fulltext-index",
                      source_name=settings.bucket, params=params,
                      plan_params={"indexPartitions": 1, "numReplicas": 0})
    try:
        mgr.upsert_index(idx)
        print(f"  + search  {scope}/{name}")
    except CouchbaseException as exc:
        if "exists" in str(exc).lower():
            print(f"  = search  {scope}/{name} exists")
        else:
            print(f"  ! search  {scope}/{name}: {str(exc)[:160]}")


def ensure_search(drop_first: bool = False) -> None:
    for name, (scope, coll) in VECTOR_INDEXES.items():
        _upsert_search(scope, name, vector_params(name, scope, coll), drop_first)
    _upsert_search("catalog", PRODUCT_FTS_INDEX, product_text_params(), drop_first)


# --------------------------------------------------------------------------- #
def verify() -> None:
    print("\nCollections")
    scopes = {s.name: {c.name for c in s.collections}
              for s in get_bucket().collections().get_all_scopes()}
    for scope, colls in COLLECTIONS.items():
        for coll in colls:
            ok = coll in scopes.get(scope, set())
            n = query(f"SELECT RAW COUNT(1) FROM {fq(scope, coll)}")[0] if ok else "-"
            print(f"  {'OK ' if ok else 'MISSING'} {scope}.{coll:<16} docs={n}")
    print("\nSearch indexes")
    for name, (scope, _c) in list(VECTOR_INDEXES.items()) + [(PRODUCT_FTS_INDEX,
                                                               ("catalog", ""))]:
        try:
            n = get_scope(scope).search_indexes().get_indexed_documents_count(name)
            print(f"  OK      {scope}/{name:<20} indexed={n}")
        except CouchbaseException as exc:
            print(f"  MISSING {scope}/{name} ({type(exc).__name__})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--drop-search", action="store_true")
    args = ap.parse_args()
    print(f"Cluster {settings.conn_str} · bucket {settings.bucket}")
    if not args.verify:
        ensure_bucket()
    get_cluster()
    if not args.verify:
        print("\nScopes and collections")
        ensure_collections()
        print("\nGSI")
        ensure_gsi()
        print("\nSearch")
        ensure_search(args.drop_search)
    verify()


if __name__ == "__main__":
    main()
