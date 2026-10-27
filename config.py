"""Central configuration, read from `.env`.

Local-first: Couchbase Server on this machine, models in Ollama. Pointing the
same code at Capella later is a matter of changing the connection string and
the model endpoint here, not the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
ASSETS_DIR = ROOT / "assets"


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # Couchbase
    conn_str: str = os.getenv("CB_CONNECTION_STRING", "couchbase://127.0.0.1")
    username: str = os.getenv("CB_USERNAME", "Administrator")
    password: str = os.getenv("CB_PASSWORD", "password")
    bucket: str = os.getenv("CB_BUCKET", "retail")
    bucket_ram_mb: int = _i("CB_BUCKET_RAM_MB", 1024)
    use_wan_profile: bool = _b("CB_USE_WAN_PROFILE", False)

    # Models (an OpenAI-compatible endpoint: Ollama locally, Capella AI later)
    model_endpoint: str = os.getenv("MODEL_ENDPOINT", "http://127.0.0.1:11434/v1")
    model_api_key: str = os.getenv("MODEL_API_KEY", "ollama")
    chat_model: str = os.getenv("CHAT_MODEL", "qwen2.5:3b")
    chat_max_tokens: int = _i("CHAT_MAX_TOKENS", 512)
    vision_model: str = os.getenv("VISION_MODEL", "qwen2.5vl:3b")
    model_timeout: float = _f("MODEL_TIMEOUT", 900.0)
    # Ollama ignores a forced tool_choice and learns bad habits from a
    # flattened tool history; these two switches keep it on the rails.
    native_tool_history: bool = _b("LLM_NATIVE_TOOL_HISTORY", True)

    embedding_model: str = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
    embedding_dims: int = _i("EMBEDDING_DIMS", 768)
    embedding_query_prefix: str = os.getenv("EMBEDDING_QUERY_PREFIX", "search_query: ")
    embedding_document_prefix: str = os.getenv("EMBEDDING_DOCUMENT_PREFIX",
                                               "search_document: ")
    embedding_batch: int = _i("EMBEDDING_BATCH", 32)

    # Agent Memory server (shared local container, writes <bucket>.agentmemory.*)
    agent_memory_url: str = os.getenv("AGENT_MEMORY_URL", "http://127.0.0.1:8082")
    agent_memory_container: str = os.getenv("AGENT_MEMORY_CONTAINER", "agentmemory-local")
    agent_memory_timeout: float = _f("AGENT_MEMORY_TIMEOUT", 60.0)
    # Every user id this demo writes starts with this, so the shared Agent
    # Memory server can hold other demos' users without collisions.
    user_prefix: str = os.getenv("USER_PREFIX", "RET-")

    # Thresholds (cosine, nomic-embed-text)
    cache_threshold: float = _f("CACHE_SIMILARITY_THRESHOLD", 0.80)
    cache_margin: float = _f("CACHE_MARGIN", 0.03)
    memory_threshold: float = _f("MEMORY_SIMILARITY_THRESHOLD", 0.55)

    # Store rules
    approval_limit_usd: float = _f("APPROVAL_LIMIT_USD", 10000.0)
    tax_rate: float = _f("TAX_RATE", 0.0625)
    free_delivery_over: float = _f("FREE_DELIVERY_OVER", 500.0)
    delivery_fee: float = _f("DELIVERY_FEE", 29.99)

    # UI
    platform_label: str = os.getenv("PLATFORM_LABEL", "Couchbase Server 8.0.3 (local)")
    model_service_label: str = os.getenv("MODEL_SERVICE_LABEL", "Ollama (local)")


settings = Settings()

# Scopes and collections. The agentc scopes (agent_catalog, agent_activity) and
# the Agent Memory scope (agentmemory) are created by those services themselves.
COLLECTIONS: dict[str, list[str]] = {
    "catalog": ["products", "categories", "offers"],
    "commerce": ["customers", "carts", "orders", "payments"],
    "planning": ["room_rules", "room_plans"],
    "support": ["kb_articles"],
    "aidp": ["semantic_cache", "agent_catalog", "audit_log", "sessions", "usage"],
}

# Vector indexes: name -> (scope, collection)
VECTOR_INDEXES: dict[str, tuple[str, str]] = {
    "products_vec_idx": ("catalog", "products"),
    "kb_vec_idx": ("support", "kb_articles"),
    "cache_vec_idx": ("aidp", "semantic_cache"),
    "tools_vec_idx": ("aidp", "agent_catalog"),
}
# Full-text (non-vector) index over product text, with facets.
PRODUCT_FTS_INDEX = "products_text_idx"
