import datetime
import json
import os
import threading
import time
import uuid

RUNS_FILE = "data/agentops_runs.json"
STATS_FILE = "data/agentops_stats.json"
MAX_SAVED_RUNS = 200


class AgentOps:
    """AgentOps Observability, Execution Tracing, and Operations Platform."""

    def __init__(self):
        self._lock = threading.Lock()
        self.runs: dict[str, dict] = {}
        self.stats = {
            "total_runs": 0,
            "total_tool_calls": 0,
            "approvals_requested": 0,
            "approvals_approved": 0,
            "approvals_edited": 0,
            "approvals_rejected": 0,
            "failed_runs": 0,
        }
        self._load()

    def _load(self):
        with self._lock:
            if os.path.exists(STATS_FILE):
                try:
                    with open(STATS_FILE, "r", encoding="utf-8") as f:
                        saved_stats = json.load(f)
                        self.stats.update(saved_stats)
                except Exception:
                    pass

            if os.path.exists(RUNS_FILE):
                try:
                    with open(RUNS_FILE, "r", encoding="utf-8") as f:
                        saved_runs = json.load(f)
                        if isinstance(saved_runs, list):
                            for r in saved_runs:
                                if "id" in r:
                                    self.runs[r["id"]] = r
                        elif isinstance(saved_runs, dict):
                            self.runs.update(saved_runs)
                except Exception:
                    pass

    def _persist(self):
        os.makedirs("data", exist_ok=True)
        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, indent=2)

            # Keep only the most recent MAX_SAVED_RUNS
            sorted_runs = sorted(
                self.runs.values(),
                key=lambda x: x.get("start_time", 0),
                reverse=True
            )[:MAX_SAVED_RUNS]

            with open(RUNS_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted_runs, f, indent=2)
        except Exception as e:
            print(f"[AgentOps] Error saving persistence: {e}")

    def start_run(self, agent_id: str, agent_name: str, session_id: str, prompt: str) -> dict:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        now = time.time()
        iso_now = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()

        run = {
            "id": run_id,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "session_id": session_id,
            "prompt": prompt,
            "status": "running",
            "start_time": now,
            "created_at": iso_now,
            "end_time": None,
            "duration_ms": 0,
            "steps": [],
            "tool_calls_count": 0,
            "response": None,
            "error": None,
        }

        with self._lock:
            self.runs[run_id] = run
            self.stats["total_runs"] += 1
            self._persist()

        return run

    def log_step(self, run_id: str, event_type: str, data: dict):
        if not run_id:
            return

        step = {
            "timestamp": time.time(),
            "iso_time": datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3],
            "event": event_type,
            "data": data,
        }

        with self._lock:
            run = self.runs.get(run_id)
            if run:
                run["steps"].append(step)
                if event_type == "tool_call":
                    count = len(data.get("tool_calls", [])) or 1
                    run["tool_calls_count"] += count
                    self.stats["total_tool_calls"] += count
                self._persist()

    def record_approval_request(self):
        with self._lock:
            self.stats["approvals_requested"] += 1
            self._persist()

    def record_approval(self, decision_type: str):
        with self._lock:
            if decision_type == "approve":
                self.stats["approvals_approved"] += 1
            elif decision_type == "edit":
                self.stats["approvals_edited"] += 1
            elif decision_type == "reject":
                self.stats["approvals_rejected"] += 1
            self._persist()

    def complete_run(self, run_id: str, response: str = None, status: str = "completed", error: str = None):
        if not run_id:
            return

        with self._lock:
            run = self.runs.get(run_id)
            if run:
                now = time.time()
                run["end_time"] = now
                run["duration_ms"] = int((now - run.get("start_time", now)) * 1000)
                run["status"] = status
                if response is not None:
                    run["response"] = response
                if error:
                    run["error"] = error
                    self.stats["failed_runs"] += 1
                self._persist()

    def get_runs(self, limit: int = 50, agent_id: str = None) -> list:
        with self._lock:
            runs_list = list(self.runs.values())

        if agent_id:
            runs_list = [r for r in runs_list if r.get("agent_id") == agent_id]

        runs_list.sort(key=lambda x: x.get("start_time", 0), reverse=True)
        return runs_list[:limit]

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            return self.runs.get(run_id)

    def get_stats(self) -> dict:
        with self._lock:
            stats_copy = dict(self.stats)
            # Calculate average duration
            completed_runs = [r for r in self.runs.values() if r.get("duration_ms", 0) > 0]
            if completed_runs:
                avg_dur = int(sum(r["duration_ms"] for r in completed_runs) / len(completed_runs))
            else:
                avg_dur = 0
            stats_copy["avg_duration_ms"] = avg_dur
            return stats_copy


# Singleton instance
agentops = AgentOps()
