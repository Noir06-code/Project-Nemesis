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
agent = Agent(tools=tools)  # your Agent class, unchanged

sessions = {}
sessions_lock = threading.Lock()
tool_gaurd_needed=[]
available_tools=[]


def get_verifier(sid):
    with sessions_lock:
        if sid not in sessions:
            sessions[sid] = Verifier()
        return sessions[sid]


@app.route("/")
def index():
    return render_template("index2.html")

@app.route("/tool_gaurd/<mode>", methods=["GET", "POST"])
def tool_gaurd(mode:str):
    if mode=="set_gaurd":
        tool_gaurd_needed.extend(request.get_json().get("tools"))
        return {"status":"done"},200
    elif mode=="get_gaurd":
        return {"tools":tool_gaurd_needed},200
    elif mode=="set_available":
        available_tools.extend(request.get_json().get("tools"))
        return {"status":"done"},200
    else:
        return {"tools": available_tools},200

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
                answer = agent.invoke(data["content"])
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
    app.run(host="127.0.0.1", port=5000, threaded=True)