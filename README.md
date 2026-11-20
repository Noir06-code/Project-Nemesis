# Project Nemesis · Multi-Agent & AgentOps Studio

A human-in-the-loop multi-agent execution platform and AgentOps observability tool. Users can **register custom agents** by assigning a name, task (system prompt), and available tools, run multiple agents concurrently, inspect execution traces, and verify guarded tool calls in real time.

Built with **Flask**, **WebSockets**, **LangGraph**, and **Gemini**.

---

## Key Features

1. **Multi-Agent Registration & Management**:
   - Register new agents with a **Name**, **Task (System Prompt)**, and **Assigned Tools**.
   - Custom model assignment (e.g. `google_genai:gemini-2.5-flash-lite`, `google_genai:gemini-2.5-flash`, etc.).
   - Edit, delete, and switch between agents seamlessly on the fly.
   - Persistent storage in `data/agents.json`.

2. **AgentOps Observability & Tracing**:
   - Platform metrics: Active agents, total runs, tool calls count, human approval stats.
   - Granular execution run traces: input prompt, tool calls, human interventions, latency, tool output, and final agent response.
   - Step-by-step trace inspection modal in the dashboard.

3. **Human-in-the-Loop Tool Guarding**:
   - Guard sensitive tools (e.g., `system_command`) so they require human approval before execution.
   - Interactive verification cards in the chat UI:
     - **Approve**: Run tool as requested.
     - **Edit & Send**: Modify tool arguments before execution.
     - **Reject**: Cancel tool call and inform the agent.

4. **Extensible Tool Registry**:
   - `web_search`: DuckDuckGo real-time web search.
   - `web_fetch`: SSRF-protected webpage content extractor.
   - `system_command`: Run shell commands with timeout and output limits (default guarded).
   - `calculator`: Safe mathematical evaluator.
   - `save_note`: Save findings and key notes into memory.
   - `read_notes`: Retrieve and search saved notes.

---

## Project Structure

```
├── Agent/
│   ├── agent_manager.py       # Multi-agent registry, CRUD, persistence, lifecycle
│   ├── agentops.py            # AgentOps observability, execution tracing, metrics
│   ├── agentic_workflow.py    # LangGraph agent, verifier, verified_add_messages reducer
│   └── agentic_workflow_with_mcp.py  # Optional MCP client variant
├── Tools/
│   └── tools.py               # Tool registry (web_search, system_command, calculator, etc.)
├── templates/
│   └── index3.html            # Multi-agent dashboard, Chat UI, AgentOps traces, Tool guards
├── data/                      # Persistent storage (agents, runs, stats, notes)
├── server.py                  # Flask + WebSocket server, REST APIs for agents, tools, runs
├── requirements.txt
└── README.md
```

---

## Quick Start

### 1. Setup Environment
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure API Key
Create `.env` file:
```env
GOOGLE_API_KEY=your-gemini-api-key
PORT=5000
```

### 3. Run Server
```bash
python3 server.py
```
Open **http://localhost:5000** in your browser.

---

## REST API Reference

### Agents API
- `GET /api/agents` - List all registered agents.
- `POST /api/agents` - Register a new agent (`{ "name": "...", "task": "...", "tools": ["web_search", ...], "model": "..." }`).
- `GET /api/agents/<agent_id>` - Get agent details.
- `PUT /api/agents/<agent_id>` - Update agent name, task, tools, or model.
- `DELETE /api/agents/<agent_id>` - Delete an agent.
- `POST /api/agents/<agent_id>/run` - Run an agent synchronously on a prompt (`{ "prompt": "..." }`).

### Tools & Guards API
- `GET /api/tools` - List all available tools and their guard statuses.
- `POST /api/tools/guard` - Set guarded tools (`{ "tools": ["system_command", ...] }`).

### AgentOps Observability API
- `GET /api/stats` - Platform metrics (agents, runs, tool calls, approvals).
- `GET /api/runs` - List recent execution runs.
- `GET /api/runs/<run_id>` - Get detailed execution trace with step events.

### WebSockets
- `/ws/chat?sid=<sid>&agent_id=<agent_id>` - Interactive chat with active agent.
- `/ws/verify?sid=<sid>` - Human-in-the-loop verification channel.
