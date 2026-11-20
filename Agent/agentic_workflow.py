#from langchain.chat_models import init_chat_model
#from typing import Annotated
#from typing_extensions import TypedDict
#from langgraph.graph import StateGraph, START, END
#from langgraph.graph.message import add_messages
#from langgraph.prebuilt import ToolNode, tools_condition
##from Tools.tools import tools
#from dotenv import load_dotenv
#
#load_dotenv()
#
#
#class State(TypedDict):
#    messages: Annotated[list, add_messages]
#
#
#
#class Agent():
#    # TODO: I have to add tool rag here and dynamic system prompt skill based in agents
#    def __init__(self, model="google_genai:gemini-2.5-flash", system_prompt="", tools=[]):
#
#        self.llm = init_chat_model(model)
#        self.llm_with_tools=self.llm.bind_tools(tools)
#        chatbot=self.chatbot
#        builder = StateGraph(State)
#        builder.add_node(chatbot)
#        builder.add_node("tools", ToolNode(tools))
#        builder.add_edge(START, "chatbot")
#        builder.add_conditional_edges("chatbot", tools_condition)
#        builder.add_edge("tools", "chatbot")
#        self.graph = builder.compile()
#    
#    def chatbot(self,state: State):
#        return {"messages": [self.llm_with_tools.invoke(state["messages"])]}
#    
#    
#    
#    def invoke(self, message):
#        state = self.graph.invoke({"messages": [{"role": "user", "content": message}]})
#        return state["messages"][-1].content


import json
import queue
from contextlib import contextmanager
import threading
import uuid
from contextvars import ContextVar
from typing import Annotated

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
    convert_to_messages,
)
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict
import requests
#from Tools.tools import available_tools
load_dotenv()


# ----------------------------------------------------------------------------
# Helpers: turning messages into JSON for the websocket
# ----------------------------------------------------------------------------


def set_tools(tools):
    requests.post("http://localhost:5000/tool_gaurd/set_available",
                  json={"tools":[i.name for i in tools]}
                  )
    
def text_of(content):
    """Message content can be a str or a list of blocks (e.g. Gemini/Claude)."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def serialize(m):
    d = {"id": m.id, "role": m.type, "content": text_of(m.content)}
    if isinstance(m, AIMessage):
        d["tool_calls"] = [
            {"id": tc["id"], "name": tc["name"], "args": tc["args"]}
            for tc in m.tool_calls
        ]
    if isinstance(m, ToolMessage):
        d["name"] = m.name
        d["tool_call_id"] = m.tool_call_id
    return d


# ----------------------------------------------------------------------------
# Verifier: one per session. The reducer calls request(), which pushes the
# message to the user over the verify websocket and blocks until they answer.
# ----------------------------------------------------------------------------
class Verifier:
    def __init__(self, timeout=600):
        self.timeout = timeout  # seconds to wait before auto-rejecting
        self.ai_rejected = False  # set when an AI message was rejected this turn
        self.ws = None
        self.pending = {}  # message id -> (proposal dict, Queue)
        self._lock = threading.Lock()

    # --- verify websocket lifecycle -----------------------------------------
    def attach(self, ws):
        with self._lock:
            self.ws = ws
            # if the page was reloaded, re-send anything still waiting
            for proposal, _ in list(self.pending.values()):
                self._send(proposal)

    def detach(self, ws):
        with self._lock:
            if self.ws is ws:
                self.ws = None

    def _send(self, payload):  # call with lock held
        if self.ws is None:
            return
        try:
            self.ws.send(json.dumps(payload))
        except Exception:
            self.ws = None

    # --- called from the reducer (graph thread) -----------------------------
    def request(self, msg):
        proposal = {"type": "proposal", **serialize(msg)}
        q = queue.Queue(maxsize=1)
        with self._lock:
            self.pending[msg.id] = (proposal, q)
            self._send(proposal)
        try:
            try:
                decision = q.get(timeout=self.timeout)
            except queue.Empty:
                decision = {"type": "reject"}
            if decision.get("type") not in ("approve", "edit") and not isinstance(msg, ToolMessage):
                self.ai_rejected = True
            return decision
        finally:
            with self._lock:
                self.pending.pop(msg.id, None)

    # --- called from the verify websocket thread ----------------------------
    def resolve(self, decision):
        with self._lock:
            item = self.pending.get(decision.get("id"))
        if item:
            try:
                item[1].put_nowait(decision)
            except queue.Full:
                pass


_current_verifier: ContextVar = ContextVar("verifier", default=None)


def _apply_decision(msg, d):
    """Turn the user's answer into the message that actually gets merged."""
    action = d.get("type")

    if action == "approve":
        return msg

    if action == "edit":
        update = {}
        if d.get("content") is not None:
            update["content"] = d["content"]
        if isinstance(msg, AIMessage) and d.get("tool_calls") is not None:
            old = msg.tool_calls
            update["tool_calls"] = [
                {
                    "id": tc.get("id")
                    or (old[i]["id"] if i < len(old) else str(uuid.uuid4())),
                    "name": tc["name"],
                    "args": tc.get("args", {}),
                    "type": "tool_call",
                }
                for i, tc in enumerate(d["tool_calls"])
            ]
        return msg.model_copy(update=update)

    # reject (or anything unrecognised): nothing is added to memory.
    # Exception: a rejected tool result is replaced by a stub, otherwise the
    # (already approved) AI tool call would have no answer and the next LLM
    # call would fail with a dangling tool_call.
    if isinstance(msg, ToolMessage):
        return ToolMessage(
            content="[rejected by user]",
            tool_call_id=msg.tool_call_id,
            name=msg.name,
            id=msg.id,
        )
    return None


# ----------------------------------------------------------------------------
# The reducer: same as add_messages, but everything that is not a user message
# must be approved / edited / rejected by the user before it is merged.
# ----------------------------------------------------------------------------
'''def verified_add_messages(left, right):
    verifier = _current_verifier.get()
    if verifier is None:  # no session attached -> behave like plain add_messages
        return add_messages(left, right)

    incoming = convert_to_messages(right if isinstance(right, list) else [right])
    accepted = []
    for m in incoming:
        if m.id is None:
            m.id = str(uuid.uuid4())
        if isinstance(m, (HumanMessage, RemoveMessage)):
            accepted.append(m)  # user's own messages skip verification
            continue
        out = _apply_decision(m, verifier.request(m))
        if out is not None:
            accepted.append(out)
    return add_messages(left, accepted)'''

def VERIFICATION_NEEDED():
    data = requests.get("http://localhost:5000/tool_gaurd/get_gaurd")
    return {i for i in data.json().get("tools")}



def verified_add_messages(left, right):
    verifier = _current_verifier.get()

    if verifier is None:
        return add_messages(left, right)

    incoming = convert_to_messages(
        right if isinstance(right, list) else [right]
    )

    accepted = []

    for m in incoming:
        if m.id is None:
            m.id = str(uuid.uuid4())

        # Always allow user messages and removal operations
        if isinstance(m, (HumanMessage, RemoveMessage)):
            accepted.append(m)
            continue

        # Tool results: skip verification unless the tool is guarded
        if isinstance(m, ToolMessage):
            if {m.name}.isdisjoint(VERIFICATION_NEEDED()):
                accepted.append(m)
                continue

        if isinstance(m, AIMessage):
            # Plain text replies (no tool calls) are never verified
            if not m.tool_calls:
                accepted.append(m)
                continue

            # Tool calls: skip verification unless a guarded tool is involved
            tool_names = {tc["name"] for tc in m.tool_calls}
            if tool_names.isdisjoint(VERIFICATION_NEEDED()):
                accepted.append(m)
                continue

        # Everything else (guarded tool calls and their results) requires verification
        out = _apply_decision(m, verifier.request(m))

        if out is not None:
            accepted.append(out)

    return add_messages(left, accepted)

class State(TypedDict):
    messages: Annotated[list, verified_add_messages]


@contextmanager
def use_verifier(verifier):
    """Wrap agent.invoke(...) in this so the reducer knows which user to ask."""
    token = _current_verifier.set(verifier)
    try:
        yield
    finally:
        _current_verifier.reset(token)


class Agent():
    # TODO: I have to add tool rag here and dynamic system prompt skill based in agents
    def __init__(self, model="google_genai:gemini-2.5-flash", system_prompt="", tools=[]):

        self.llm = init_chat_model(model)
        self.llm_with_tools=self.llm.bind_tools(tools)
        set_tools(tools)
        print("tool list is sent")
        chatbot=self.chatbot
        builder = StateGraph(State)
        builder.add_node(chatbot)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "chatbot")
        builder.add_conditional_edges("chatbot", tools_condition)
        builder.add_edge("tools", "chatbot")
        self.graph = builder.compile()

    def chatbot(self,state: State):
        return {"messages": [self.llm_with_tools.invoke(state["messages"])]}



    def invoke(self, message):
        state = self.graph.invoke({"messages": [{"role": "user", "content": message}]})
        return state["messages"][-1].content