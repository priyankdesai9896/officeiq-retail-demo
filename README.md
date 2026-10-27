# OfficeIQ — AI-powered office store demo

A Streamlit store for office furniture and supplies that runs entirely on your laptop:
**Couchbase Server** for data, search and vectors, **Ollama** for the models,
the **Couchbase Agent Memory** server for what each customer told the store, and
**Couchbase Agent Catalog** (`agentc`) for versioned tools, prompts and traces.

What you can show with it:

- **Ask the Assistant.** A small local model (`qwen2.5:3b`) calls real catalog tools: search, plan a room, add to cart, list orders, save a preference. Each tool call is checked against a policy.
- **Semantic cache.** Repeated store questions are answered from `aidp.semantic_cache` (vector match), and the app counts the tokens and time saved.
- **Agent Memory.** Four demo customers come with preferences: colours, budget, a carpeted room, video calls, 2-day shipping. Searches, room plans, cart suggestions and checkout all use them.
- **Room Planner.** Type a room or snap a photo of it (`qwen2.5vl:3b` counts the chairs, tables and TVs). The planner sizes the furniture, chooses what to bring and what to buy, and adds the whole plan to the cart.
- **Agent Catalog.** A Tools Hub, Prompts Hub and Agent Tracer in the sidebar, read from the tools, prompts and activity that `agentc` publishes to Couchbase. See [docs/agent_catalog.md](docs/agent_catalog.md).

```
 Browser ── Streamlit app (app.py, retail/*) ──┬── Couchbase Server 8.0  (bucket "retail": catalog, carts,
            :8502                              │      orders, KB, semantic cache, vectors, FTS, agentc)
                                               ├── Ollama :11434  qwen2.5:3b · qwen2.5vl:3b · nomic-embed-text
                                               └── Agent Memory server :8082 ── llama3.2:1b + nomic-embed-text
```

## Requirements

- macOS or Linux, about **16 GB RAM** (give Docker at least 2 GB) and **12 GB of free disk** (7 GB of models, plus the Couchbase image and data).
- [Docker Desktop](https://www.docker.com/products/docker-desktop/), which runs Couchbase Server.
- [Ollama](https://ollama.com/download), run natively for speed (`brew install ollama`).
- Python **3.12+**.

Everything runs on the CPU. On an Intel laptop a chat answer takes a few seconds, and a room photo takes 1 to 3 minutes.

## Quick start

```bash
git clone https://github.com/priyankdesai9896/officeiq-retail-demo.git
cd officeiq-retail-demo
./scripts/setup.sh        # models, Couchbase, data, Agent Catalog: about 10-20 min the first time
./scripts/run.sh          # http://localhost:8502
```

`setup.sh` is safe to re-run, and each step skips what's already done:

1. It copies `.env.example` → `.env` and `agent-memory.env.example` → `agent-memory.env`. The defaults work as they are.
2. It runs `ollama pull` for the exact models the demo uses: `qwen2.5:3b` (chat), `qwen2.5vl:3b` (vision), `nomic-embed-text` (embeddings) and `llama3.2:1b` (Agent Memory).
3. It starts Couchbase Server (`docker compose up -d couchbase`) and initialises the cluster: user `Administrator`, password `password`.
4. It creates `.venv` and installs `requirements.txt`.
5. `scripts/setup_cluster.py` creates the `retail` bucket, its scopes and collections, the GSI indexes, a full-text index and the vector indexes.
6. `scripts/load_data.py` loads `data/*.json` and embeds the products and help articles.
7. It runs `agentc init`, `index` and `publish` for the tools and prompts in `agent_catalog/`, and installs a post-commit hook that republishes them on every commit.
8. It starts and seeds Agent Memory, if the image is available (see below).

Options: `SKIP_COUCHBASE=1` uses a Couchbase Server you already run (put its address in `.env`), `SKIP_MODELS=1` skips the model pulls, and `SKIP_MEMORY=1` skips Agent Memory.

## Agent Memory server image

The Couchbase Agent Memory server (`agentmemory-server:v1.0.0`) isn't on a public registry, so this repo doesn't include it. **Without it the demo still works**: search, the assistant, the room planner, the cart and the Agent Catalog. Signed-in customers just don't get remembered preferences.

If you have the image from Couchbase:

```bash
docker load -i agentmemory-server.tar
./scripts/setup.sh        # starts it (docker compose --profile memory up -d) and seeds the 4 customers
```

## Using the demo

Pick a customer under **Signed in as** in the sidebar:

| Customer | Who | What Agent Memory knows |
|---|---|---|
| Guest | anyone | nothing; asks go to the semantic cache |
| Rachel Kim (RET-1001) | Facilities director, new HQ floor | white furniture with gray chairs, ~$25k budget, 10-seat conference room |
| Leo Martins (RET-1002) | Startup office manager | black and walnut, electric standing desks, prints a lot in color |
| Priyank Desai (RET-1003) | Remote engineer, spare-bedroom office | walnut and black, desk ≥ 55″ for two monitors, carpet needs a chair mat, 2-day shipping |
| James O'Neill (RET-1004) | Teacher, small home office | white, under $600 total, no single item over $400 |

Things to try:
- "My back hurts standing all day at my desk"
- "Set up an office that seats 12 and is video capable"
- Room Planner → upload a room photo, then **Read my photo**
- The cart's Add buttons next to each suggestion
- Sidebar → Agent Tracer

Pay with the test card `4242 4242 4242 4242`. `4000 0000 0000 0002` is declined.

## Checking the stack

```bash
./scripts/status.sh
```
It prints one ✅/❌ line each for the app, Couchbase (doc count), Ollama, the chat LLM (a live "Say OK"), embeddings (768 dims), the vision model, Agent Memory (`/health/models`) and the Agent Catalog. The checks behind it, one at a time:

```bash
set -a; . ./.env; set +a
curl -s localhost:8502/_stcore/health
curl -s -u "$CB_USERNAME:$CB_PASSWORD" localhost:8091/pools/default/buckets/$CB_BUCKET
curl -s localhost:8082/health/models
ollama list; ollama ps
.venv/bin/agentc status; .venv/bin/agentc ls --db
```

## Reset between demos

```bash
./scripts/reset_demo.sh   # clears the semantic cache and token tally, photo readings, activity log
# then restart ./scripts/run.sh
```

## Troubleshooting

- **"Something else is already listening on :8091".** You already run Couchbase Server. Either stop it, or point `.env` at it and run `SKIP_COUCHBASE=1 ./scripts/setup.sh`.
- **Agent Memory says "LLM service unavailable".** Run `curl localhost:8082/health/models` to make it re-check its models. On a busy CPU, the container's own health check can show *unhealthy* while the API is fine.
- **First answer is slow.** Ollama loads each model on first use. Later calls are faster.
- **Using different models.** Set `CHAT_MODEL`, `VISION_MODEL` and `EMBEDDING_MODEL` in `.env`. Any OpenAI-compatible endpoint works through `MODEL_ENDPOINT`. Changing the embedding model means re-running `scripts/load_data.py`.

## Layout

```
app.py                 Streamlit UI
retail/                assistant (tool calling), planner, vision, search, commerce, memory, semantic cache, tracing
agent_catalog/         tools and prompts published with agentc
scripts/               setup, run, status, reset; cluster setup, data load, memory seed, data generator
data/                  generated catalog, customers, memories, offers, help articles, room rules
assets/rooms/          room photos used by the planner
```

The product images are 3D renders drawn by the app (`retail/render.py`). The room photos come from Pixabay under the Pixabay Content License, with credits in `data/room_credits.json`.

## License

The code is under the [MIT License](LICENSE). The room photos are under the Pixabay Content License.
