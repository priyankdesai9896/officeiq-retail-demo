"""The one Couchbase connection every part of the demo shares.

Catalogue, carts, orders, room plans, vectors, the semantic cache, the Agent
Catalog registry and the audit log all live in one cluster, behind one client.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions, QueryOptions

from config import settings

_lock = threading.Lock()
_cluster: Cluster | None = None


def get_cluster() -> Cluster:
    global _cluster
    with _lock:
        if _cluster is not None:
            return _cluster
        opts = ClusterOptions(PasswordAuthenticator(settings.username, settings.password))
        if settings.use_wan_profile:
            opts.apply_profile("wan_development")
        cluster = Cluster(settings.conn_str, opts)
        cluster.wait_until_ready(timedelta(seconds=30))
        _cluster = cluster
        return _cluster


def get_bucket():
    return get_cluster().bucket(settings.bucket)


def get_scope(scope_name: str):
    return get_bucket().scope(scope_name)


def get_collection(scope_name: str, collection_name: str):
    return get_scope(scope_name).collection(collection_name)


def kv_get(scope: str, collection: str, key: str) -> dict[str, Any] | None:
    from couchbase.exceptions import DocumentNotFoundException

    try:
        return get_collection(scope, collection).get(key).content_as[dict]
    except DocumentNotFoundException:
        return None


def kv_upsert(scope: str, collection: str, key: str, doc: dict[str, Any]) -> Any:
    return get_collection(scope, collection).upsert(key, doc)


def kv_remove(scope: str, collection: str, key: str) -> bool:
    from couchbase.exceptions import DocumentNotFoundException

    try:
        get_collection(scope, collection).remove(key)
        return True
    except DocumentNotFoundException:
        return False


def query(statement: str, *, consistent: bool = False, **named) -> list[Any]:
    """SQL++ at cluster level; statements use fully qualified keyspaces.

    `consistent=True` waits for the index to include every earlier write
    (REQUEST_PLUS) — needed to read back something just written on a local
    single node, where the read lands milliseconds after the write.
    """
    from couchbase.n1ql import QueryScanConsistency

    kwargs: dict[str, Any] = {}
    if named:
        kwargs["named_parameters"] = named
    if consistent:
        kwargs["scan_consistency"] = QueryScanConsistency.REQUEST_PLUS
    return list(get_cluster().query(statement, QueryOptions(**kwargs)))


def fq(scope: str, collection: str) -> str:
    return f"`{settings.bucket}`.`{scope}`.`{collection}`"


def ping() -> dict[str, Any]:
    return {"ok": query("SELECT RAW 1") == [1], "bucket": settings.bucket}
