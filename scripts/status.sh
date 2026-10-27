#!/usr/bin/env bash
# One ✅/❌ line per service: app, Couchbase, Ollama + each model, Agent Memory, Agent Catalog.
#   ./scripts/status.sh
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && { set -a; . ./.env; set +a; }
CB_USERNAME=${CB_USERNAME:-Administrator}; CB_PASSWORD=${CB_PASSWORD:-password}
CB_BUCKET=${CB_BUCKET:-retail}; CB_MGMT_URL=${CB_MGMT_URL:-http://127.0.0.1:8091}
OLLAMA=${OLLAMA_HOST:-http://127.0.0.1:11434}; MEM=${AGENT_MEMORY_URL:-http://127.0.0.1:8082}
APP_PORT=${APP_PORT:-8502}
CHAT=${CHAT_MODEL:-qwen2.5:3b}; VISION=${VISION_MODEL:-qwen2.5vl:3b}; EMB=${EMBEDDING_MODEL:-nomic-embed-text}
ok()  { printf '✅ %-16s %s\n' "$1" "$2"; }
bad() { printf '❌ %-16s %s\n' "$1" "$2"; }
py()  { python3 -c "$1" 2>/dev/null; }

r=$(curl -s -m 3 "localhost:$APP_PORT/_stcore/health")
[ "$r" = ok ] && ok App "http://localhost:$APP_PORT" || bad App "not running — ./scripts/run.sh"

r=$(curl -s -m 5 -u "$CB_USERNAME:$CB_PASSWORD" "$CB_MGMT_URL/pools/default/buckets/$CB_BUCKET" \
    | py 'import json,sys;d=json.load(sys.stdin);print(d["basicStats"]["itemCount"],"docs ·",",".join(n["status"] for n in d["nodes"]))')
[ -n "$r" ] && ok Couchbase "bucket $CB_BUCKET: $r" || bad Couchbase "bucket $CB_BUCKET not reachable at $CB_MGMT_URL"

r=$(curl -s -m 3 "$OLLAMA/api/version" | py 'import json,sys;print(json.load(sys.stdin)["version"])')
[ -n "$r" ] && ok Ollama "version $r" || bad Ollama "not running — start the Ollama app or 'ollama serve'"

r=$(curl -s -m 120 "$OLLAMA/api/generate" -d "{\"model\":\"$CHAT\",\"prompt\":\"Say OK\",\"stream\":false,\"options\":{\"num_predict\":5}}" \
    | py 'import json,sys;d=json.load(sys.stdin);print(d["response"].strip()[:10],"·",d["total_duration"]//10**6,"ms")')
[ -n "$r" ] && ok "Chat LLM" "$CHAT answered: $r" || bad "Chat LLM" "$CHAT — ollama pull $CHAT"

r=$(curl -s -m 60 "$OLLAMA/api/embed" -d "{\"model\":\"$EMB\",\"input\":\"office chair\"}" \
    | py 'import json,sys;print(len(json.load(sys.stdin)["embeddings"][0]),"dims")')
[ -n "$r" ] && ok Embeddings "$EMB: $r" || bad Embeddings "$EMB — ollama pull $EMB"

r=$(curl -s -m 10 "$OLLAMA/api/show" -d "{\"model\":\"$VISION\"}" \
    | py 'import json,sys;c=json.load(sys.stdin)["capabilities"];print("vision" if "vision" in c else "")')
[ "$r" = vision ] && ok Vision "$VISION installed" || bad Vision "$VISION — ollama pull $VISION"

r=$(curl -s -m 30 "$MEM/health/models" | py 'import json,sys;d=json.load(sys.stdin);print(d["status"],"· llm",d["llm"]["model"],"· embedding",d["embedding"]["model"])')
case "$r" in healthy*) ok "Agent Memory" "$r";; "") bad "Agent Memory" "not running at $MEM (optional — see README)";; *) bad "Agent Memory" "$r";; esac

if [ -x .venv/bin/agentc ]; then
  r=$(.venv/bin/agentc ls --db 2>/dev/null | grep -c $'^\e\[1m[a-z_]')
  [ "${r:-0}" -gt 0 ] && ok "Agent Catalog" "$r tools and prompts published (.venv/bin/agentc status for detail)" \
                      || bad "Agent Catalog" "nothing published — .venv/bin/agentc index agent_catalog && .venv/bin/agentc publish tools prompts"
else
  bad "Agent Catalog" "no .venv — run ./scripts/setup.sh"
fi
