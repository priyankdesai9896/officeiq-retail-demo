#!/usr/bin/env bash
# One-shot setup for a fresh clone. Safe to re-run: every step skips what's already done.
#
#   ./scripts/setup.sh
#
# Environment switches:
#   SKIP_COUCHBASE=1         use a Couchbase Server you already run (set it in .env)
#   SKIP_MODELS=1            don't pull Ollama models
#   SKIP_MEMORY=1            don't start or seed the Agent Memory server
#   AGENT_MEMORY_IMAGE_TAR=/path/agentmemory-server.tar   docker load the Agent Memory image first
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

say()  { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

MODELS=(qwen2.5:3b qwen2.5vl:3b nomic-embed-text llama3.2:1b)
MEM_IMAGE=agentmemory-server:v1.0.0

# --------------------------------------------------------------------------- #
say "Checking prerequisites"
have docker || die "Docker is required — install Docker Desktop: https://www.docker.com/products/docker-desktop/"
docker info >/dev/null 2>&1 || die "Docker is installed but not running — start Docker Desktop."
have ollama || die "Ollama is required — 'brew install ollama' or https://ollama.com/download"
PY=""
for p in python3.13 python3.12 python3; do
  if have "$p" && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then PY=$p; break; fi
done
[ -n "$PY" ] || die "Python 3.12 or newer is required — 'brew install python@3.13'"
echo "docker ✓  ollama ✓  $($PY --version) ✓"

# --------------------------------------------------------------------------- #
say "Config files"
[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }
[ -f agent-memory.env ] || { cp agent-memory.env.example agent-memory.env; echo "created agent-memory.env"; }
set -a; . ./.env; set +a
CB_MGMT_URL=${CB_MGMT_URL:-http://127.0.0.1:8091}

# --------------------------------------------------------------------------- #
if [ "${SKIP_MODELS:-0}" != 1 ]; then
  say "Ollama models (about 7 GB the first time)"
  curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null \
    || { echo "starting 'ollama serve' in the background"; (ollama serve >/dev/null 2>&1 &); sleep 3; }
  for m in "${MODELS[@]}"; do
    if ollama list | awk 'NR>1{print $1}' | grep -qx "$m\(:latest\)\{0,1\}"; then
      echo "= $m"
    else
      echo "+ $m"; ollama pull "$m"
    fi
  done
fi

# --------------------------------------------------------------------------- #
if [ "${SKIP_COUCHBASE:-0}" != 1 ]; then
  say "Couchbase Server (Docker)"
  if curl -s -m 2 "$CB_MGMT_URL/pools" >/dev/null && ! docker ps --format '{{.Names}}' | grep -qx officeiq-couchbase; then
    die "Something else is already listening on $CB_MGMT_URL (another Couchbase Server?).
   Stop it, or use it instead: set its address and credentials in .env and re-run with SKIP_COUCHBASE=1."
  fi
  docker compose up -d couchbase
  printf 'waiting for Couchbase to start'
  for _ in $(seq 1 90); do curl -s -m 2 "$CB_MGMT_URL/pools" >/dev/null && break; printf .; sleep 2; done; echo
  if curl -s -o /dev/null -w '%{http_code}' -u "$CB_USERNAME:$CB_PASSWORD" "$CB_MGMT_URL/pools/default" | grep -q 200; then
    echo "= cluster already initialised"
  else
    docker exec officeiq-couchbase couchbase-cli cluster-init -c 127.0.0.1 \
      --cluster-name officeiq --cluster-username "$CB_USERNAME" --cluster-password "$CB_PASSWORD" \
      --services data,index,query,fts \
      --cluster-ramsize 1024 --cluster-index-ramsize 256 --cluster-fts-ramsize 256 \
      --index-storage-setting default
  fi
fi

# --------------------------------------------------------------------------- #
say "Python environment (.venv)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "installed $(wc -l < <(.venv/bin/pip list --format=freeze)) packages"

# --------------------------------------------------------------------------- #
say "Bucket, scopes, collections and indexes"
.venv/bin/python scripts/setup_cluster.py

say "Loading data (products and help articles get embeddings — a few minutes on CPU)"
.venv/bin/python scripts/load_data.py

# --------------------------------------------------------------------------- #
say "Agent Catalog (tools and prompts)"
.venv/bin/agentc -ni init catalog activity
if git rev-parse --git-dir >/dev/null 2>&1; then
  HOOK="$(git rev-parse --git-path hooks)/post-commit"
  cat > "$HOOK" <<'HOOK'
#!/bin/sh
# Every commit re-indexes agent_catalog/ and publishes its tools and prompts.
cd "$(git rev-parse --show-toplevel)" || exit 0
.venv/bin/agentc index agent_catalog
.venv/bin/agentc publish tools prompts
# agentc made the agent_activity scope; add the app's index on its logs.
.venv/bin/python scripts/setup_cluster.py | grep -v '^  = ' || true
HOOK
  chmod +x "$HOOK"
  echo "installed post-commit hook"
fi
.venv/bin/agentc index agent_catalog
.venv/bin/agentc publish tools prompts

# --------------------------------------------------------------------------- #
say "Agent Memory (optional)"
if [ -n "${AGENT_MEMORY_IMAGE_TAR:-}" ] && [ "${SKIP_MEMORY:-0}" != 1 ]; then docker load -i "$AGENT_MEMORY_IMAGE_TAR"; fi
if [ "${SKIP_MEMORY:-0}" = 1 ]; then
  echo "skipped (SKIP_MEMORY=1)"
elif docker image inspect "$MEM_IMAGE" >/dev/null 2>&1; then
  docker compose --profile memory up -d agentmemory
  printf 'waiting for Agent Memory'
  for _ in $(seq 1 60); do curl -s -m 2 "${AGENT_MEMORY_URL:-http://127.0.0.1:8082}/health" | grep -q healthy && break; printf .; sleep 3; done; echo
  .venv/bin/python scripts/seed_memory.py --reset
else
  cat <<EOF
The Agent Memory server image ($MEM_IMAGE) isn't on a public registry, so it's
not included. Everything else works without it; signed-in customers just don't
get remembered preferences. Once you have the image from Couchbase:
    docker load -i agentmemory-server.tar      # or AGENT_MEMORY_IMAGE_TAR=... ./scripts/setup.sh
    ./scripts/setup.sh
EOF
fi

# --------------------------------------------------------------------------- #
say "Status"
bash scripts/status.sh || true
printf '\nDone. Start the app with:  ./scripts/run.sh   →  http://localhost:%s\n' "${APP_PORT:-8502}"
