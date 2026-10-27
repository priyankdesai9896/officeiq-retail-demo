# Agent Catalog: Tools Hub, Prompts Hub, Agent Tracer

The **AGENT CATALOG** section in the sidebar (under "AI models in use": Agent Tracer, Tools Hub, Prompts Hub, styled after the Capella console) opens three views showing what the
Couchbase AI Data Plane console shows for an agent, read straight from bucket
`retail`. Everything is published and logged by Couchbase's `agentc`.

| View | What it shows | Where it lives |
|---|---|---|
| 🧰 Tools Hub | The store assistant's 13 tools: owner, version, side effect, and its policy (granted, not granted, retired version, spending limit) with the count of policy checks | `retail.agent_catalog.tools`; the policy lives in `retail.aidp.agent_catalog` and the checks in `aidp.audit_log` |
| 📝 Prompts Hub | The 5 prompts the app sends its models, with their version, and whether the app loaded each one from the hub or fell back to the copy in the code | `retail.agent_catalog.prompts` |
| 🛰️ Agent Tracer | Laid out like Capella's: search by **Session ID** or **Tags**, **Date Range / App** filters, and a list of sessions. Click a session for its **Session Trace Logs**, filtered by trace type (User, Internal, LLM, Tool Call, Tool Results, Hand-off, System, Assistant) and by tag | `retail.agent_activity.logs` |

"Show what happened" under each request has a **🛰️ Open in Agent Tracer** button that jumps to that run.

## How a request is logged
Each browser session is a session under the root span `store_assistant` (the App). Each request is a child span (`ask`, `search`, `plan_room`, `read_photo`, `replicate_photo`, `add_to_cart`, `checkout`, `save_preference`), tagged `run`, `user`, `ms`, `tokens_spent` and `tokens_saved`. Its messages, in order:

| Trace type | agentc kind | What we log |
|---|---|---|
| User | `user` | What was asked |
| Tool Call | `tool-call` | `ask`: each tool the chat model chose to call, with its arguments. Other flows: the catalog tool the request is (e.g. `plan_room`) |
| Internal | `request-header` | Before each chat or vision model call: the prompts sent, from the Prompts Hub, and (for `ask`) the tools offered to the model |
| LLM | `chat-completion` | The model's answer, tokens and latency |
| System | `key-value` | Every other step: the service, reads, writes, indexes, ms |
| Tool Results | `tool-result` | What the tool returned to the model (`error` if the policy blocked it) |
| Assistant | `assistant` | The answer shown to the user |

"Open in Agent Tracer" opens the session filtered to the tag `run=<id>`. Clear the Tags box to see the whole session.

## Ask the Assistant: real tool calling
- `retail/assistant.py: run_agent()` reads the prompt `store_assistant` **and the tools it lists** from the catalog (`agentc_bridge.agent()`): `search_kb`, `search_products`, `add_to_cart`, `view_cart`, `track_order`, `list_orders`, `issue_refund`. The tools are the functions in `agent_catalog/tools/store_tools.py`.
- `qwen2.5:3b` is given the tool schemas and decides what to call, for up to 4 rounds, then answers.
- Every tool first calls `governance.authorize()`, which writes to `aidp.audit_log`. `issue_refund` isn't granted, so it's refused and the model tells the customer.
- The customer comes from `retail.agent_context.CURRENT`, never from the model, so it can't read another customer's cart or orders.
- **Ollama 0.40.3 drops some valid tool calls** (a refund request came back empty) and rewrites others. So the app sends Qwen's own chat format to `/api/generate` (raw) and reads the model's `<tool_call>` blocks itself.
- **The semantic cache:** a request that mentions the customer (`my`, `cart`, `order`, `add`, `refund`…) skips it. An answer is cached only when the model used `search_kb` alone.

## How it's published
- The sources are `agent_catalog/tools/store_tools.py` and `agent_catalog/prompts/*.yaml`.
- `.git/hooks/post-commit` runs `agentc index agent_catalog` and then `agentc publish tools prompts`, so **every commit republishes**. The catalog version is the commit.
- The app reads each prompt by name (`retail/agentc_bridge.py: prompt()`) and caches it for 60 s. If agentc can't be reached, it uses the copy in the code; the Model step in "Show what happened" says which (`hub @ <commit>` or `code`).
- To change a prompt, edit its YAML and commit. Keep the copy in the code (`retail/assistant.py`, `retail/vision.py`) in step, since it's the fallback.

## Setting it up again
```sh
.venv/bin/agentc init --add-hook-for agent_catalog   # scopes, indexes, views, hook
# The generated hook calls `agentc` from PATH — point it at .venv/bin/agentc.
git commit --allow-empty -m "Publish the Agent Catalog"   # index + publish
.venv/bin/python scripts/setup_cluster.py              # adds idx_logs_run for the tracer
```

## Gotchas
- **Publishing needs a commit.** An uncommitted change is indexed as "dirty" and isn't published.
- **Embeddings** come from Ollama `nomic-embed-text` (`AGENT_CATALOG_EMBEDDING_MODEL_*` in `.env`), so nothing is downloaded. Indexing and the app must use the same model.
- **Reset demo** clears the Agent Tracer, the semantic cache and saved photo readings. It never touches the Tools or Prompts Hubs.
- **Publishing is incremental:** an item that didn't change keeps the version it was first published at. The hubs show the newest record of each name, and drop names that are no longer in `.agent-catalog/` (the hook's local index). Publishing one commit twice stores each record twice; the hubs show one.
- **Policy is separate from the catalog.** Being listed in the Tools Hub doesn't let the assistant use a tool. `retail.governance.authorize()` checks the grant before every call and writes the decision to `aidp.audit_log`.
