import json
import queue
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    convert_to_messages,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from Agent.agentops import agentops

load_dotenv()

# ----------------------------------------------------------------------------
# In-Process Thread-Safe Tool Guard Registry
# ----------------------------------------------------------------------------
_guarded_tools: set[str] = {"system_command"}
_guarded_lock = threading.Lock()


def get_guarded_tools() -> set[str]:
    with _guarded_lock:
        return set(_guarded_tools)


def set_guarded_tools(tools_list: list[str]):
    with _guarded_lock:
        _guarded_tools.clear()
        _guarded_tools.update(tools_list)


def VERIFICATION_NEEDED() -> set[str]:
    return get_guarded_tools()


# ----------------------------------------------------------------------------
# Helpers: Turning messages into JSON for the WebSocket and AgentOps
# ----------------------------------------------------------------------------
def text_of(content):
    """Robustly extract text from string, dict block, object block, or list of blocks."""
    if not content:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if "text" in block and block["text"] is not None:
                    parts.append(str(block["text"]))
                elif block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                parts.append(str(block.text))
            elif hasattr(block, "content"):
                parts.append(str(block.content))
        return "".join(parts).strip()
    return str(content).strip()


def serialize(m):
    d = {"id": m.id, "role": m.type, "content": text_of(m.content)}
    if isinstance(m, AIMessage):
        d["tool_calls"] = [
            {"id": tc["id"], "name": tc["name"], "args": tc["args"]}
            for tc in getattr(m, "tool_calls", [])
        ]
    if isinstance(m, ToolMessage):
        d["name"] = m.name
        d["tool_call_id"] = m.tool_call_id
    return d


# ----------------------------------------------------------------------------
# Verifier: one per session for Human-in-the-Loop approvals
# ----------------------------------------------------------------------------
class Verifier:
    def __init__(self, timeout=600):
        self.timeout = timeout  # seconds to wait before auto-rejecting
        self.ai_rejected = False  # set when an AI message was rejected this turn
        self.ws = None
        self.pending = {}  # message id -> (proposal dict, Queue)
        self._lock = threading.Lock()

    def attach(self, ws):
        with self._lock:
            self.ws = ws
            for proposal, _ in list(self.pending.values()):
                self._send(proposal)

    def detach(self, ws):
        with self._lock:
            if self.ws is ws:
                self.ws = None

    def _send(self, payload):
        if self.ws is None:
            return
        try:
            self.ws.send(json.dumps(payload))
        except Exception:
            self.ws = None

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
                decision = {"type": "reject", "id": msg.id, "reason": "timeout"}
            if decision.get("type") not in ("approve", "edit") and not isinstance(msg, ToolMessage):
                self.ai_rejected = True
            return decision
        finally:
            with self._lock:
                self.pending.pop(msg.id, None)

    def resolve(self, decision):
        with self._lock:
            item = self.pending.get(decision.get("id"))
        if item:
            try:
                item[1].put_nowait(decision)
            except queue.Full:
                pass


_current_verifier: ContextVar = ContextVar("verifier", default=None)
_current_run_id: ContextVar = ContextVar("run_id", default=None)


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
            old = getattr(msg, "tool_calls", [])
            update["tool_calls"] = [
                {
                    "id": tc.get("id") or (old[i]["id"] if i < len(old) else str(uuid.uuid4())),
                    "name": tc["name"],
                    "args": tc.get("args", {}),
                    "type": "tool_call",
                }
                for i, tc in enumerate(d["tool_calls"])
            ]
        return msg.model_copy(update=update)

    # Reject
    if isinstance(msg, ToolMessage):
        return ToolMessage(
            content="[rejected by user]",
            tool_call_id=msg.tool_call_id,
            name=msg.name,
            id=msg.id,
        )
    return None


# ----------------------------------------------------------------------------
# The Reducer
# ----------------------------------------------------------------------------
def verified_add_messages(left, right):
    verifier = _current_verifier.get()
    run_id = _current_run_id.get()
    guarded = get_guarded_tools()

    if verifier is None:
        return add_messages(left, right)

    incoming = convert_to_messages(
        right if isinstance(right, list) else [right]
    )

    accepted = []

    for m in incoming:
        if m.id is None:
            m.id = str(uuid.uuid4())

        # Human and Remove messages pass unconditionally
        if isinstance(m, (HumanMessage, RemoveMessage)):
            accepted.append(m)
            continue

        # Tool result message
        if isinstance(m, ToolMessage):
            if run_id:
                agentops.log_step(run_id, "tool_result", {
                    "tool": m.name,
                    "tool_call_id": m.tool_call_id,
                    "content": text_of(m.content)[:1000]
                })

            if {m.name}.isdisjoint(guarded):
                accepted.append(m)
                continue

            # Guarded tool result approval
            if run_id:
                agentops.log_step(run_id, "human_approval_request", {
                    "type": "tool_result",
                    "name": m.name,
                    "content": text_of(m.content)
                })
                agentops.record_approval_request()

            decision = verifier.request(m)
            if run_id:
                agentops.log_step(run_id, "human_decision", decision)
                agentops.record_approval(decision.get("type"))

            out = _apply_decision(m, decision)
            if out is not None:
                accepted.append(out)
            continue

        # AI Message
        if isinstance(m, AIMessage):
            # Plain text reply without tool calls
            if not getattr(m, "tool_calls", None):
                accepted.append(m)
                continue

            tool_names = {tc["name"] for tc in m.tool_calls}
            if run_id:
                agentops.log_step(run_id, "tool_call", {
                    "tools": list(tool_names),
                    "tool_calls": m.tool_calls
                })

            # Check if any tool is in guarded set
            if tool_names.isdisjoint(guarded):
                accepted.append(m)
                continue

            if run_id:
                agentops.log_step(run_id, "human_approval_request", {
                    "type": "ai_tool_call",
                    "tools": list(tool_names),
                    "tool_calls": m.tool_calls
                })
                agentops.record_approval_request()

            decision = verifier.request(m)
            if run_id:
                agentops.log_step(run_id, "human_decision", decision)
                agentops.record_approval(decision.get("type"))

            out = _apply_decision(m, decision)
            if out is not None:
                accepted.append(out)
            continue

        accepted.append(m)

    return add_messages(left, accepted)


class State(TypedDict):
    messages: Annotated[list, verified_add_messages]


@contextmanager
def use_verifier(verifier, run_id: str = None):
    token_v = _current_verifier.set(verifier)
    token_r = _current_run_id.set(run_id)
    try:
        yield
    finally:
        _current_verifier.reset(token_v)
        _current_run_id.reset(token_r)


# Base instruction appended to all agents to guarantee a non-empty final response
BASE_AGENT_GUIDELINES = (
    "\n\nOperational Guidelines:\n"
    "- When you use tools, ALWAYS provide a comprehensive, direct, and helpful final response explaining and synthesizing the findings or calculation results to the user.\n"
    "- Never finish your turn with an empty message after executing tools."
)


# ----------------------------------------------------------------------------
# Agent Class
# ----------------------------------------------------------------------------
class Agent:
    def __init__(
        self,
        id: str = None,
        name: str = "Agent",
        model: str = "google_genai:gemini-flash-latest",
        system_prompt: str = "",
        tools: list = None
    ):
        self.id = id or str(uuid.uuid4())
        self.name = name
        self.model = model
        self.system_prompt = (system_prompt.strip() + BASE_AGENT_GUIDELINES).strip()
        self.tools = tools or []
        self._build_graph()

    def _build_graph(self):
        self.llm = init_chat_model(self.model)
        if self.tools:
            self.llm_with_tools = self.llm.bind_tools(self.tools)
        else:
            self.llm_with_tools = self.llm

        builder = StateGraph(State)
        builder.add_node("chatbot", self.chatbot)

        if self.tools:
            builder.add_node("tools", ToolNode(self.tools))
            builder.add_edge(START, "chatbot")
            builder.add_conditional_edges("chatbot", tools_condition)
            builder.add_edge("tools", "chatbot")
        else:
            builder.add_edge(START, "chatbot")
            builder.add_edge("chatbot", END)

        self.graph = builder.compile()

    def chatbot(self, state: State):
        messages = list(state["messages"])
        has_system = any(
            isinstance(m, SystemMessage) or getattr(m, "type", "") == "system"
            for m in messages
        )
        if self.system_prompt and not has_system:
            messages = [SystemMessage(content=self.system_prompt)] + messages

        try:
            response = self.llm_with_tools.invoke(messages)
            return {"messages": [response]}
        except Exception as e:
            # Fallback to gemini-flash-latest on quota or model errors
            if "RESOURCE_EXHAUSTED" in str(e) or "NOT_FOUND" in str(e):
                fallback_llm = init_chat_model("google_genai:gemini-flash-latest")
                if self.tools:
                    fallback_llm = fallback_llm.bind_tools(self.tools)
                self.llm_with_tools = fallback_llm
                response = self.llm_with_tools.invoke(messages)
                return {"messages": [response]}
            raise

    def invoke(self, message: str) -> str:
        state = self.graph.invoke({"messages": [HumanMessage(content=message)]})
        messages = state.get("messages", [])

        # 1. Search backwards for the last non-empty AIMessage text
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                txt = text_of(m.content)
                if txt:
                    return txt

        # 2. If no non-empty AIMessage, check for ToolMessage output
        for m in reversed(messages):
            if isinstance(m, ToolMessage):
                tool_output = text_of(m.content)
                if tool_output and tool_output != "[rejected by user]":
                    return f"Action completed. Result:\n{tool_output}"

        # 3. Fallback to last message text or default confirmation
        if messages:
            txt = text_of(messages[-1].content)
            if txt:
                return txt

        return "Task completed successfully."
