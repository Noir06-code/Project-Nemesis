import datetime
import json
import os
import re
import threading
import uuid
from typing import Optional

AGENTS_FILE = "data/agents.json"
DEFAULT_MODEL = "google_genai:gemini-flash-latest"

DEFAULT_AGENTS = [
    {
        "id": "general-assistant",
        "name": "General Assistant",
        "task": "You are an intelligent, versatile AI assistant. Help the user answer questions, do research, solve calculations, and manage notes. Be concise, direct, and factual.",
        "tools": ["web_search", "calculator", "save_note", "read_notes"],
        "model": DEFAULT_MODEL,
        "created_at": "2026-11-20T00:00:00Z",
        "total_runs": 0,
        "last_run_at": None,
        "status": "active"
    },
    {
        "id": "deep-researcher",
        "name": "Deep Research Analyst",
        "task": "You are an expert research analyst. Search the web, fetch primary source articles, analyze findings critically, and provide comprehensive summaries with citations.",
        "tools": ["web_search", "web_fetch", "save_note"],
        "model": DEFAULT_MODEL,
        "created_at": "2026-11-20T00:00:00Z",
        "total_runs": 0,
        "last_run_at": None,
        "status": "active"
    },
    {
        "id": "system-devops",
        "name": "DevOps & System Assistant",
        "task": "You are a Linux DevOps and system administration agent. Assist the user with terminal tasks, command execution, and environment diagnostics. Use system_command to inspect the system.",
        "tools": ["system_command"],
        "model": DEFAULT_MODEL,
        "created_at": "2026-11-20T00:00:00Z",
        "total_runs": 0,
        "last_run_at": None,
        "status": "active"
    }
]


def _slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", s) or "agent"


class AgentManager:
    """Manages the registry of multiple agents, persistence, and execution instances."""

    def __init__(self):
        self._lock = threading.Lock()
        self.agents: dict[str, dict] = {}
        self._instances: dict[str, any] = {}
        self._load()

    def _load(self):
        with self._lock:
            if os.path.exists(AGENTS_FILE):
                try:
                    with open(AGENTS_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, list):
                            for a in data:
                                if "id" in a:
                                    # Ensure reliable model without quota lock
                                    if a.get("model") in ("google_genai:gemini-2.5-flash", "google_genai:gemini-2.5-flash-lite"):
                                        a["model"] = DEFAULT_MODEL
                                    self.agents[a["id"]] = a
                        elif isinstance(data, dict):
                            self.agents = data
                except Exception as e:
                    print(f"[AgentManager] Error reading {AGENTS_FILE}: {e}")

            if not self.agents:
                for a in DEFAULT_AGENTS:
                    self.agents[a["id"]] = dict(a)
            self._persist_unlocked()

    def _persist_unlocked(self):
        os.makedirs(os.path.dirname(AGENTS_FILE) or "data", exist_ok=True)
        try:
            with open(AGENTS_FILE, "w", encoding="utf-8") as f:
                json.dump(list(self.agents.values()), f, indent=2)
        except Exception as e:
            print(f"[AgentManager] Error saving {AGENTS_FILE}: {e}")

    def list_agents(self) -> list[dict]:
        with self._lock:
            return list(self.agents.values())

    def get_agent(self, agent_id: str) -> Optional[dict]:
        with self._lock:
            return self.agents.get(agent_id)

    def register_agent(
        self,
        name: str,
        task: str,
        tools: list[str],
        model: str = DEFAULT_MODEL
    ) -> dict:
        base_id = _slugify(name)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        with self._lock:
            agent_id = base_id
            if agent_id in self.agents:
                agent_id = f"{base_id}-{uuid.uuid4().hex[:6]}"

            agent_data = {
                "id": agent_id,
                "name": name.strip(),
                "task": task.strip(),
                "tools": list(set(tools or [])),
                "model": model.strip() or DEFAULT_MODEL,
                "created_at": now_iso,
                "total_runs": 0,
                "last_run_at": None,
                "status": "active"
            }

            self.agents[agent_id] = agent_data
            self._persist_unlocked()

        return agent_data

    def update_agent(
        self,
        agent_id: str,
        name: Optional[str] = None,
        task: Optional[str] = None,
        tools: Optional[list[str]] = None,
        model: Optional[str] = None
    ) -> Optional[dict]:
        with self._lock:
            agent = self.agents.get(agent_id)
            if not agent:
                return None

            if name is not None:
                agent["name"] = name.strip()
            if task is not None:
                agent["task"] = task.strip()
            if tools is not None:
                agent["tools"] = list(set(tools))
            if model is not None:
                agent["model"] = model.strip() or DEFAULT_MODEL

            self._instances.pop(agent_id, None)
            self._persist_unlocked()
            return dict(agent)

    def delete_agent(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id not in self.agents:
                return False
            del self.agents[agent_id]
            self._instances.pop(agent_id, None)
            self._persist_unlocked()
            return True

    def record_run(self, agent_id: str):
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock:
            agent = self.agents.get(agent_id)
            if agent:
                agent["total_runs"] = agent.get("total_runs", 0) + 1
                agent["last_run_at"] = now_iso
                self._persist_unlocked()

    def get_agent_instance(self, agent_id: str):
        with self._lock:
            agent_data = self.agents.get(agent_id)
            if not agent_data:
                if self.agents:
                    agent_id = next(iter(self.agents))
                    agent_data = self.agents[agent_id]
                else:
                    agent_data = DEFAULT_AGENTS[0]
                    self.agents[agent_data["id"]] = dict(agent_data)
                    agent_id = agent_data["id"]

            if agent_id in self._instances:
                return self._instances[agent_id]

        from Agent.agentic_workflow import Agent
        from Tools.tools import get_tools_by_names

        assigned_tools = get_tools_by_names(agent_data.get("tools", []))
        instance = Agent(
            id=agent_data["id"],
            name=agent_data["name"],
            model=agent_data.get("model", DEFAULT_MODEL),
            system_prompt=agent_data.get("task", ""),
            tools=assigned_tools
        )

        with self._lock:
            self._instances[agent_id] = instance

        return instance


agent_manager = AgentManager()
