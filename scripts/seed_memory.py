#!/usr/bin/env python3
"""Seed each demo customer's profile into the Agent Memory server.

Profiles are written under the session "profile", so `--reset` replaces them
without touching anything a customer said in conversation.

    python scripts/seed_memory.py --reset
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DATA_DIR  # noqa: E402
from retail import memory_server  # noqa: E402

SESSION = "profile"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    seeds = json.loads((DATA_DIR / "memories.json").read_text())
    for customer, rows in seeds.items():
        if args.reset:
            old = [r for r in memory_server.list_for_account(customer, limit=200)
                   if r.get("session_id") == SESSION]
            if old:
                memory_server.forget(customer, [r["memory_id"] for r in old],
                                     session_id=SESSION)
        ids = [memory_server.remember(customer, text, kind=kind, importance=imp,
                                      source="profile", session_id=SESSION)["memory_id"]
               for kind, text, imp in rows]
        ok = memory_server.wait_ready(customer, ids, timeout=120.0, session_id=SESSION)
        print(f"  {customer}: {len(ids)} memories{'' if ok else ' (still indexing)'}")


if __name__ == "__main__":
    main()
