import json
import os
import threading

from flask import Flask, request, render_template
from flask_sock import Sock
from Agent.agentic_workflow import Agent, Verifier, text_of, use_verifier

try:
    from Tools.tools import tools  # your real tools
except ImportError:
    tools = []

app = Flask(__name__)
sock = Sock(app)
agent: Agent = None
agent_lock = threading.Lock()

def get_agent():
    global agent

    with agent_lock:
        if agent is None:
            agent = Agent(tools=tools)

    return agent
sessions = {}
sessions_lock = threading.Lock()
tool_gaurd_needed=[]
available_tools=[]


def get_verifier(sid):
    get_agent()
    with sessions_lock:
        if sid not in sessions:
            sessions[sid] = Verifier()
        return sessions[sid]


@app.route("/")
def index():
    get_agent()
    return render_template("index3.html")

@app.route("/tool_gaurd/<mode>", methods=["GET", "POST"])
def tool_gaurd(mode:str):
    if mode=="set_gaurd":
        tool_gaurd_needed[:] = request.get_json().get("tools", [])
        return {"status":"done"},200
    elif mode=="get_gaurd":
        return {"tools":tool_gaurd_needed},200
    elif mode=="set_available":
        available_tools[:] = request.get_json().get("tools", [])
        return {"status":"done"},200
    elif mode=="get_available":
        return {"tools": available_tools},200
    return {"error": "unknown mode"},404

# ---- chat channel: user <-> agent -------------------------------------------
@sock.route("/ws/chat")
def chat(ws):
    sid = request.args.get("sid", "default")
    verifier = get_verifier(sid)

    def send(obj):
        ws.send(json.dumps(obj))

    while True:
        raw = ws.receive()
        if raw is None:
            break
        data = json.loads(raw)
        if data.get("type") != "user_message":
            continue

        verifier.ai_rejected = False
        try:
            with use_verifier(verifier):
                answer = get_agent().invoke(data["content"])
            if verifier.ai_rejected:
                send({
                    "type": "info",
                    "content": "The AI output was rejected, so nothing was added to memory.",
                })
            else:
                send({"type": "message", "role": "ai", "content": text_of(answer)})
        except Exception as e:
            send({"type": "error", "content": str(e)})
        send({"type": "done"})


# ---- verify channel: reducer <-> user ---------------------------------------
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
            verifier.resolve(json.loads(raw))
    finally:
        verifier.detach(ws)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)