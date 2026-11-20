# Project Nemesis

A human-in-the-loop AI agent with a web chat UI. The agent can search the web, fetch pages and run shell commands, but **you decide which tools need your approval** before they run. Anything guarded is shown to you in the browser, where you can approve it, edit it or reject it.

Built with Flask, WebSockets, LangGraph and Gemini.

## Features

- **Chat UI** in the browser (`templates/index3.html`), with live status and tool-call display
- **Approval cards** for guarded actions: approve, edit the arguments, or reject
- **Manage tools** button: pick which tools are guarded, live, without a restart
- **Built-in tools**
  - `web_search`: web search via DuckDuckGo
  - `web_fetch`: reads a page as text, with protection against private and local addresses (SSRF)
  - `system_command`: runs a shell command (60 s timeout, output capped)
- **Per-session approvals** that survive the agent's multi-step reasoning, with a 10-minute auto-reject
- Optional MCP integration (`Agent/agentic_workflow_with_mcp.py`, `main_server.py`)

## How it works

```
Browser ──/ws/chat────▶ Flask ──▶ LangGraph agent (Gemini + tools)
   ▲                                      │
   └────/ws/verify◀── approval request ◀──┘   (blocks until you answer)
```

1. You send a message over `/ws/chat`.
2. The agent decides what to do and may call tools.
3. Every message goes through a reducer (`verified_add_messages`). Depending on the rules below, it either lets the message through or pauses and sends an approval card over `/ws/verify`.
4. Your answer (approve, edit or reject) decides what the agent sees next.

### What needs approval

| Message | Verified? |
|---|---|
| Your messages | No |
| Agent's plain text replies | No |
| Calls to unguarded tools, and their results | No |
| Calls to **guarded** tools, and their results | **Yes** |

The guard list is edited in the UI (**Manage tools**) and is read fresh on every message. It starts empty, so tick `system_command` before using the agent on a machine you care about.

## Quick start

```bash
git clone https://github.com/Noir06-code/Project-Nemesis.git
cd Project-Nemesis

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

echo "GOOGLE_API_KEY=your-key-here" > .env

python server.py            # http://localhost:5000
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | none (required) | Gemini API key, read from the environment or `.env` |
| `HOST` | `0.0.0.0` | Interface to listen on |
| `PORT` | `5000` | Port to listen on |
| `FLASK_DEBUG` | `0` | Set to `1` for the dev reloader, local use only |

The model is set in `Agent/agentic_workflow.py` (default `google_genai:gemini-2.5-flash`).

## Deployment

```bash
gunicorn -w 1 --threads 100 -b 0.0.0.0:$PORT server:app
```

(this is what the included `Procfile` runs)

- **Use one worker.** Sessions, pending approvals and the guard list live in memory. Scale with threads, not processes.
- **Allow WebSocket upgrades** for `/ws/chat` and `/ws/verify` on your proxy, with a long read timeout (approvals can wait up to 10 minutes).
- Serve over **HTTPS**. The page uses `wss://` automatically, and browsers only provide `crypto.randomUUID()` on secure origins.
- Set `GOOGLE_API_KEY` as an environment variable on the host. Do not commit `.env`.

## Project structure

```
server.py                  Flask app, WebSocket routes, guard-list API
shared.py                  In-process guard and tool lists shared by server and agent
Agent/
  agentic_workflow.py      LangGraph agent, verifier, verified_add_messages reducer
  agentic_workflow_with_mcp.py   Variant with MCP servers (optional)
Tools/tools.py             system_command, web_search, web_fetch
templates/index3.html      Chat UI and approval cards
main_server.py             Standalone MCP server example (optional)
```

## Add your own tool

Add a function with the `@tool` decorator in `Tools/tools.py` and include it in the `tools` list. It appears in **Manage tools** on the next start, where you can guard it.

## Security notes

- `system_command` runs arbitrary shell commands as the user running the server. Guard it, and run the app in a container or low-privilege account when deploying.
- The app has no login. Put it behind authentication before exposing it to the internet.
- `web_fetch` refuses localhost, private IPs and redirects, but treat fetched page content as untrusted input to the model.

## Roadmap

- Tool retrieval for large tool sets
- Dynamic system prompts and skills
- Reconnect and session resume in the UI