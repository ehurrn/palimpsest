# RECON: ml-pipeline → Palimpsest reuse surface

## 1. Transport
- Mechanism (sockets/HTTP/other): Server-Sent Events (SSE) over HTTP. The broker runs a FastMCP server, defaulting to the `streamable-http` transport binding on port `8766` on all interfaces (`0.0.0.0`) (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/__main__.py:22-35` and `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/start_broker.sh:14`).
- Message format (with one verbatim example message): Messages are stored as rows in the SQLite database and serialized into a JSON list. A verbatim example message format from the serialization logic (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/server.py:56-69`) is:
```json
[
  {
    "id": "a1b2c3d4e5f6",
    "from": "claude-orchestrator-8b29f0e1",
    "to": "ollama-gonktop",
    "content": "explain the Advisory Committee on Human Radiation Experiments",
    "timestamp": "2026-06-12T23:37:24.123456+00:00"
  }
]
```
The response returned directly by the `send_message` tool tool-call execution (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/server.py:166`) has this format:
```json
{"status": "sent", "message_id": "a1b2c3d4e5f6", "to": "ollama-gonktop"}
```
- Broker stateful? (what state, where persisted): Yes, the broker is stateful. It tracks registered agent nodes (`agents` table) and pending messages (`messages` table). The state is persisted in a local SQLite database named `mesh.db` in WAL journal mode (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/server.py:17-18` and `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/broker.py:48`).

## 2. send_message tool
- Exact signature (copy from source, cite file:line):
```python
async def send_message(from_agent: str, to_agent: str, content: str) -> str:
```
Cited in: `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/server.py:156`
- Node registration flow: A node registers itself by calling the `register_agent(agent_id, description)` tool (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/server.py:80`). This maps to `AgentBroker.register`, which performs an `INSERT ... ON CONFLICT(agent_id) DO UPDATE` in the `agents` table (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/broker.py:85-92`). The node must periodically send a heartbeat via the `heartbeat(agent_id)` tool (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/server.py:271`). A background lifespan-managed prune loop runs every 60 seconds and deletes agents (along with their unread messages) whose `last_seen` timestamp is older than 300 seconds (5 minutes) (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/server.py:25-33` and `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/broker.py:118-135`).
- Response routing back to caller: A caller (such as the orchestrator) registers an ephemeral sender ID, calls `send_message` to write a message to the target agent's queue, and polls `/sse` (`inbox_count` and `get_messages`) until a response arrives (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/dispatch.py:68-108`). The target daemon polls the broker, gets the message, runs the local LLM to generate a response, and then uses the `send_message` tool to route the response back to the ephemeral sender ID (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/ollama_bridge.py:214-232`).

## 3. Node registry / config
- File(s) where nodes + models are declared (paths): There is **no** central static configuration file. Node identifiers, model mappings, and endpoint URLs are declared dynamically as CLI defaults and command-line parameters in shell scripts (such as `/Users/herren/dev/ml-pipeline/agent-mesh-mcp/start_bridge.sh:27-49` and `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/scripts/start-bridge.sh:13-17`). Active nodes are tracked dynamically in the SQLite database `mesh.db` in the `agents` table (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/broker.py:58-64`).
- Can a NON-mesh process read this config safely? (yes/no + why): Yes. A non-mesh process can read this registry safely by either (a) establishing an SSE client session and executing the `list_agents` tool over HTTP/SSE, or (b) opening the SQLite database `mesh.db` locally in WAL mode and performing a read-only query on the `agents` table (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/broker.py:45-48`).
- How M5 intermittent availability is signaled: There is no dedicated availability flag. Intermittent nodes are represented dynamically. When online, they call `register_agent` and keep themselves alive by calling `heartbeat` (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/ollama_bridge.py:198-208`). When they go offline, the broker's pruning loop automatically deletes them from the registry after 5 minutes of inactivity (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/broker.py:118-135`).

## 4. Model lifecycle
- How Ollama models launch / stay warm: Models are loaded dynamically on the first request to the Ollama server chat endpoint (`/api/chat`) (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/ollama_bridge.py:83-85`). They are kept warm by the Ollama server's internal model-eviction timeout.
- Daemon long-lived? Restart behavior: Yes, the bridge daemons are designed to be long-lived. When running in `--daemon` mode, they run a reconnect loop that handles lost connection errors to the MCP server by waiting and attempting to reconnect with exponential backoff (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/ollama_bridge.py:344-374`).
- Is direct Ollama API (http://node:11434) usable outside send_message? (verify a config or code reference proving the port/binding, cite it): Yes. Direct Ollama API endpoints are reachable. The startup bridge shell scripts execute direct cURL checks to verify models (e.g. `curl -s ${OLLAMA_URL}/api/tags` cited in `/Users/herren/dev/ml-pipeline/agent-mesh-mcp/start_bridge.sh:29`). Furthermore, SSH tunnels are explicitly used to forward the remote node's port 11434 to local port 11434 or 11435 for direct access (cited in `/Users/herren/dev/ml-pipeline/agent-mesh-mcp/start_tunnel.sh:50`).

## 5. Health / retry
- Behavior when a node drops mid-task (cite code):
If a node drops mid-task, it will stop sending heartbeats and the broker will eventually prune it (removing it from the `agents` table and deleting pending messages to it) (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/broker.py:126-133`). In the meantime, the caller (`dispatch_and_wait` loop) will continue polling the inbox for a response until it hits the timeout threshold (default 120s), at which point it raises a `TimeoutError` and exits with code 1 (cited in `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/dispatch.py:89-108` and `/Users/herren/dev/ml-pipeline/working/agent-mesh-mcp/src/agent_mesh_mcp/dispatch.py:185-187`). No automatic retry or failover is implemented at the transport layer.

## 6. Reuse verdict
| Capability | ml-pipeline provides | Palimpsest must build |
|---|---|---|
| node availability | Dynamic presence registry (`agents` table) in SQLite `mesh.db` queried via `list_agents` tool. | Non-mesh worker check to read node presence from `mesh.db` or call `list_agents` via SSE. |
| node/model config | Ad-hoc CLI arguments and conditional mappings inside startup shell scripts (e.g. `start_bridge.sh`). | A structured, centralized `config.toml` that explicitly maps nodes to their models and capabilities. |
| job persistence   | None. Message queue is in-memory/ephemeral; pending messages are dropped if an agent is pruned or unregisters. | A dedicated SQL-backed `jobs` table in `palimpsest.db` to manage and track long-running pipeline tasks. |
| retry/idempotency | None. Drops are handled by client timeouts; no task-level retry mechanism exists. | Task-level retry counting, failure handling, and idempotent side-effect execution (overwriting outputs). |
| file transfer     | None. Only text/JSON message contents are supported. | Dedicated HTTP endpoints on the broker (e.g. `/file/{doc_id}.pdf` and `/ocr/{doc_id}.json`) to serve/receive files. |

## 7. Plan corrections
- **Centralized Config Registry**: Contrary to the assumption that a static node configuration is available in `ml-pipeline` that can be read directly by non-mesh processes, there is no such file. Palimpsest must declare the static cluster topology and model maps itself in the planned `config.toml`. Non-mesh processes can only dynamically query active node presence from `mesh.db`'s `agents` table.
- **Direct Ollama API**: The assumption that Ollama models can be invoked directly outside the `send_message` path is **confirmed**. The Ollama server is directly accessible via HTTP (e.g., port 11434), meaning Lane B workers can send prompts to local Ollama endpoints directly, avoiding the overhead and timeout risks of the mesh queue.
- **Availability Signal Reusability**: The assumption that the mesh availability signal is reusable by Lane B is **confirmed**. Since the broker writes heartbeat timestamps to `mesh.db`, Lane B's coordinator can inspect the `last_seen` column in the `agents` table of `mesh.db` to check node liveness.
- **Task Retry/Failure Handling**: The assumption that the broker manages job state is **corrected**. The `ml-pipeline` broker is only a message broker, not a job broker. Palimpsest must implement the entire job queue state machine, heartbeat leases, and retries in its own broker and DB.
