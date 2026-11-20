import json
import os
import threading

from flask import Flask, jsonify, render_template, request
from flask_sock import Sock

from Agent.agent_manager import agent_manager
from Agent.agentic_workflow import (
    Agent,
    Verifier,
    get_guarded_tools,
    set_guarded_tools,
    text_of,
    use_verifier,
)
from Agent.agentops import agentops
from Tools.tools import (
    AVAILABLE_TOOLS,
    get_all_tools_metadata,
    tools as all_tools_list,
)

app = Flask(__name__)
sock = Sock(app)

sessions: dict[str, Verifier] = {}
session_agent_map: dict[str, str] = {}  # sid -> active agent_id
sessions_lock = threading.Lock()


def get_verifier(sid: str) -> Verifier:
    with sessions_lock:
        if sid not in sessions:
            sessions[sid] = Verifier()
        return sessions[sid]


def get_agent():
    """Backward compatibility helper."""
    agents = agent_manager.list_agents()
    agent_id = agents[0]["id"] if agents else "general-assistant"
    return agent_manager.get_agent_instance(agent_id)


# ----------------------------------------------------------------------------
# Frontend Page
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index3.html")


# ----------------------------------------------------------------------------
# Agent Management REST APIs (AgentOps)
# ----------------------------------------------------------------------------
@app.route("/api/agents", methods=["GET", "POST"])
def api_agents():
    if request.method == "GET":
        agents = agent_manager.list_agents()
        return jsonify({"agents": agents, "count": len(agents)}), 200

    if request.method == "POST":
        data = request.get_json() or {}
        name = data.get("name")
        task = data.get("task")
        tools = data.get("tools", [])
        model = data.get("model", "google_genai:gemini-2.5-flash")

        if not name or not task:
            return jsonify({"error": "Fields 'name' and 'task' are required"}), 400

        agent = agent_manager.register_agent(
            name=name,
            task=task,
            tools=tools,
            model=model,
        )
        return jsonify({"status": "created", "agent": agent}), 201


@app.route("/api/agents/<agent_id>", methods=["GET", "PUT", "DELETE"])
def api_agent_detail(agent_id: str):
    if request.method == "GET":
        agent = agent_manager.get_agent(agent_id)
        if not agent:
            return jsonify({"error": "Agent not found"}), 404
        return jsonify({"agent": agent}), 200

    if request.method == "PUT":
        data = request.get_json() or {}
        updated = agent_manager.update_agent(
            agent_id=agent_id,
            name=data.get("name"),
            task=data.get("task"),
            tools=data.get("tools"),
            model=data.get("model"),
        )
        if not updated:
            return jsonify({"error": "Agent not found"}), 404
        return jsonify({"status": "updated", "agent": updated}), 200

    if request.method == "DELETE":
        agents = agent_manager.list_agents()
        if len(agents) <= 1:
            return jsonify({"error": "Cannot delete the last remaining agent"}), 400
        ok = agent_manager.delete_agent(agent_id)
        if not ok:
            return jsonify({"error": "Agent not found"}), 404
        return jsonify({"status": "deleted", "agent_id": agent_id}), 200


@app.route("/api/agents/<agent_id>/run", methods=["POST"])
def api_agent_run(agent_id: str):
    """Run an agent synchronously on a prompt (useful for testing or API integration)."""
    data = request.get_json() or {}
    prompt = data.get("prompt")
    sid = data.get("sid", f"api-session-{agent_id}")

    if not prompt:
        return jsonify({"error": "Field 'prompt' is required"}), 400

    agent_data = agent_manager.get_agent(agent_id)
    if not agent_data:
        return jsonify({"error": "Agent not found"}), 404

    run = agentops.start_run(agent_data["id"], agent_data["name"], sid, prompt)
    verifier = get_verifier(sid)

    try:
        with use_verifier(verifier, run["id"]):
            instance = agent_manager.get_agent_instance(agent_id)
            result = instance.invoke(prompt)

        final_result = text_of(result).strip() or "Task executed successfully."
        agentops.complete_run(run["id"], response=final_result, status="completed")
        agent_manager.record_run(agent_id)
        return jsonify({
            "status": "success",
            "run_id": run["id"],
            "response": final_result,
            "duration_ms": agentops.get_run(run["id"]).get("duration_ms", 0),
        }), 200
    except Exception as e:
        agentops.complete_run(run["id"], status="failed", error=str(e))
        return jsonify({"status": "error", "error": str(e), "run_id": run["id"]}), 500


# ----------------------------------------------------------------------------
# Tool Registry & Guard REST APIs
# ----------------------------------------------------------------------------
@app.route("/api/tools", methods=["GET"])
def api_tools():
    guarded = get_guarded_tools()
    metadata = get_all_tools_metadata()
    for item in metadata:
        item["is_guarded"] = item["name"] in guarded
    return jsonify({
        "tools": metadata,
        "guarded": list(guarded),
        "count": len(metadata),
    }), 200


@app.route("/api/tools/guard", methods=["POST"])
def api_set_guard():
    data = request.get_json() or {}
    new_guarded = data.get("tools", [])
    set_guarded_tools(new_guarded)
    return jsonify({"status": "ok", "guarded": list(get_guarded_tools())}), 200


# Backward compatibility for existing UI calls
@app.route("/tool_gaurd/<mode>", methods=["GET", "POST"])
def tool_gaurd(mode: str):
    if mode == "set_gaurd":
        new_tools = request.get_json().get("tools", [])
        set_guarded_tools(new_tools)
        return {"status": "done"}, 200
    elif mode == "get_gaurd":
        return {"tools": list(get_guarded_tools())}, 200
    elif mode == "set_available":
        return {"status": "done"}, 200
    elif mode == "get_available":
        return {"tools": list(AVAILABLE_TOOLS.keys())}, 200
    return {"error": "unknown mode"}, 404


# ----------------------------------------------------------------------------
# AgentOps Observability REST APIs
# ----------------------------------------------------------------------------
@app.route("/api/runs", methods=["GET"])
def api_runs():
    agent_id = request.args.get("agent_id")
    limit = int(request.args.get("limit", 50))
    runs = agentops.get_runs(limit=limit, agent_id=agent_id)
    return jsonify({"runs": runs, "count": len(runs)}), 200


@app.route("/api/runs/<run_id>", methods=["GET"])
def api_run_detail(run_id: str):
    run = agentops.get_run(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    return jsonify({"run": run}), 200


@app.route("/api/stats", methods=["GET"])
def api_stats():
    stats = agentops.get_stats()
    stats["total_agents"] = len(agent_manager.list_agents())
    stats["available_tools_count"] = len(AVAILABLE_TOOLS)
    stats["guarded_tools_count"] = len(get_guarded_tools())
    return jsonify(stats), 200


# ----------------------------------------------------------------------------
# WebSocket: Chat channel (User <-> Agent)
# ----------------------------------------------------------------------------
@sock.route("/ws/chat")
def chat(ws):
    sid = request.args.get("sid", "default")
    agent_id_param = request.args.get("agent_id")

    with sessions_lock:
        if agent_id_param and agent_manager.get_agent(agent_id_param):
            session_agent_map[sid] = agent_id_param
        elif sid not in session_agent_map:
            agents = agent_manager.list_agents()
            if agents:
                session_agent_map[sid] = agents[0]["id"]

    verifier = get_verifier(sid)

    def send(obj):
        try:
            ws.send(json.dumps(obj))
        except Exception:
            pass

    while True:
        raw = ws.receive()
        if raw is None:
            break

        try:
            data = json.loads(raw)
        except Exception:
            continue

        msg_type = data.get("type")

        # Handle agent switching
        if msg_type == "switch_agent":
            target_agent_id = data.get("agent_id")
            target_agent = agent_manager.get_agent(target_agent_id)
            if target_agent:
                with sessions_lock:
                    session_agent_map[sid] = target_agent_id
                send({
                    "type": "info",
                    "content": f"Switched to agent: {target_agent['name']}",
                    "agent_id": target_agent_id,
                })
            else:
                send({"type": "error", "content": f"Agent '{target_agent_id}' not found"})
            continue

        if msg_type != "user_message":
            continue

        user_content = data.get("content", "").strip()
        if not user_content:
            continue

        # Determine target agent for this message
        with sessions_lock:
            active_agent_id = data.get("agent_id") or session_agent_map.get(sid)

        agent_data = agent_manager.get_agent(active_agent_id)
        if not agent_data:
            agents = agent_manager.list_agents()
            agent_data = agents[0]
            with sessions_lock:
                session_agent_map[sid] = agent_data["id"]

        # Track execution run in AgentOps
        run = agentops.start_run(
            agent_id=agent_data["id"],
            agent_name=agent_data["name"],
            session_id=sid,
            prompt=user_content,
        )

        verifier.ai_rejected = False
        try:
            with use_verifier(verifier, run["id"]):
                agent_instance = agent_manager.get_agent_instance(agent_data["id"])
                answer = agent_instance.invoke(user_content)

            if verifier.ai_rejected:
                agentops.complete_run(run["id"], response="[rejected by user]", status="rejected")
                send({
                    "type": "info",
                    "content": "The AI action was rejected, so nothing was added to memory.",
                    "run_id": run["id"],
                })
            else:
                final_text = text_of(answer).strip()
                if not final_text:
                    final_text = "Task executed successfully."
                agentops.complete_run(run["id"], response=final_text, status="completed")
                agent_manager.record_run(agent_data["id"])
                send({
                    "type": "message",
                    "role": "ai",
                    "agent_id": agent_data["id"],
                    "agent_name": agent_data["name"],
                    "content": final_text,
                    "run_id": run["id"],
                })
        except Exception as e:
            agentops.complete_run(run["id"], status="failed", error=str(e))
            send({"type": "error", "content": str(e), "run_id": run["id"]})

        send({"type": "done", "run_id": run["id"]})


# ----------------------------------------------------------------------------
# WebSocket: Verify channel (Human-in-the-Loop Reducer <-> User)
# ----------------------------------------------------------------------------
@sock.route("/ws/verify")
def verify(ws):
    sid = request.args.get("sid", "default")
    verifier = get_verifier(sid)
    verifier.attach(ws)
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                verifier.resolve(json.loads(raw))
            except Exception:
                pass
    finally:
        verifier.detach(ws)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"Starting Multi-Agent Nemesis & AgentOps on {host}:{port}")
    app.run(host=host, port=port, debug=debug, threaded=True)
