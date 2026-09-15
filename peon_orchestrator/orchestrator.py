import sys
import os
import json
import threading
import subprocess
import uuid
import logging
import atexit
import signal
import time
from typing import Any, List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor

# Import common storage
try:
    from .storage import SimpleStorage
except ImportError:
    from storage import SimpleStorage

logger = logging.getLogger("peon-orchestrator.orchestrator")

def _readline_with_timeout(stream, timeout: float) -> Optional[str]:
    result = [None]
    def target():
        try:
            result[0] = stream.readline()
        except Exception:
            pass
    t = threading.Thread(target=target)
    t.daemon = True
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None
    return result[0]

HISTORY_KEY = "__orchestrator__:task_history"

def get_config() -> Dict[str, str]:
    return {
        "api_key": os.getenv("ORCHAI_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
        "base_url": os.getenv("ORCHAI_BASE_URL") or os.getenv("OPENAI_BASE_URL", ""),
        "model": os.getenv("ORCHAI_MODEL") or os.getenv("OPENAI_MODEL", "auto"),
    }

class AgentOrchestrator:
    def __init__(self):
        self.storage_path = os.getenv("PEON_STORAGE", ".peon-orchestrator/storage")
        self.storage = SimpleStorage(self.storage_path)
        self.agents: Dict[str, Dict[str, Any]] = {}
        self.agent_tools: Dict[str, List[Dict]] = {}
        
        # Load task history from storage
        self.task_history: Dict[str, Dict[str, Any]] = self.storage.read(HISTORY_KEY) or {}
        
        self.lock = threading.RLock()
        self.config = get_config()
        self.executor = ThreadPoolExecutor(max_workers=50)
        self._shutdown_done = False
        
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        atexit.register(self.shutdown)
    
    def _save_history(self):
        """Persist task history to storage."""
        with self.lock:
            # Limit history size before saving if needed
            self.storage.write(HISTORY_KEY, self.task_history)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.shutdown()
        sys.exit(0)

    def shutdown(self):
        with self.lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            aids = list(self.agents.keys())
        
        for aid in aids:
            self.stop_agent(aid, timeout=1.0)
        self.executor.shutdown(wait=False)
        self._save_history()

    def _get_status(self, process: subprocess.Popen) -> str:
        return "running" if process.poll() is None else "stopped"
    
    def list_agents(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [
                {
                    "agent_id": aid,
                    "role_name": info["role_name"],
                    "status": self._get_status(info["process"]),
                    "busy": info["busy"].locked(),
                    "current_task_id": info.get("current_task_id"),
                    "pending_tasks": len([tid for tid in info.get("tasks", []) if self.task_history.get(tid, {}).get("status") == "pending"]),
                    "running_tasks": len([tid for tid in info.get("tasks", []) if self.task_history.get(tid, {}).get("status") == "running"])
                }
                for aid, info in self.agents.items()
            ]
    
    def spawn_agent(self, role_name: str, system_prompt: str, tools: Optional[List[Dict]] = None) -> str:
        if not role_name or not role_name.strip():
            raise ValueError("role_name cannot be empty")
        if not system_prompt or not system_prompt.strip():
            raise ValueError("system_prompt cannot be empty")

        agent_id = str(uuid.uuid4())
        env = os.environ.copy()
        env.update({
            "PEON_ROLE": role_name,
            "PEON_PROMPT": system_prompt,
            "PEON_AGENT_ID": agent_id,
            "PEON_STORAGE": self.storage_path,
            "ORCHAI_API_KEY": self.config["api_key"],
            "ORCHAI_BASE_URL": self.config["base_url"],
            "ORCHAI_MODEL": self.config["model"]
        })
        if tools is not None:
            env["PEON_SPAWN_TOOLS"] = json.dumps(tools)
        
        logger.info(f"Using executable: {sys.executable}")
        proc = subprocess.Popen(
            [sys.executable, '-m', 'peon_orchestrator.agent'],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True
        )
        
        ready = False
        line = _readline_with_timeout(proc.stdout, 10.0)
        if line:
            try:
                handshake = json.loads(line)
                if handshake.get("status") == "ready":
                    ready = True
            except Exception as e:
                logger.error(f"Handshake parse error for {agent_id}: {e}")
        
        if not ready:
            proc.kill()
            raise RuntimeError(f"Agent {agent_id} handshake timeout or failure")

        with self.lock:
            self.agents[agent_id] = {
                "role_name": role_name,
                "process": proc,
                "stdin_lock": threading.Lock(),
                "busy": threading.Lock(),
                "tasks": [], 
                "current_task_id": None
            }
            self.agent_tools[agent_id] = tools if tools is not None else []
            
        return agent_id
    
    def stop_agent(self, agent_id: str, timeout: float = 5.0) -> bool:
        with self.lock:
            info = self.agents.get(agent_id)
            if not info:
                return False
            proc = info["process"]
            stdin_lock = info["stdin_lock"]
            tasks = info.get("tasks", [])
        
            # Mark all pending/running tasks as agent_stopped
            for tid in tasks:
                task = self.task_history.get(tid)
                if task and task["status"] in ("pending", "running"):
                    task["status"] = "agent_stopped"
                    task["finished_at"] = time.time()

        if proc.poll() is None:
            if stdin_lock.acquire(timeout=1.0):
                try:
                    proc.stdin.write(json.dumps({"method": "stop"}) + "\n")
                    proc.stdin.flush()
                except:
                    pass
                finally:
                    stdin_lock.release()
            
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        
        with self.lock:
            self.agent_tools.pop(agent_id, None)
            self.agents.pop(agent_id, None)
        return True
    
    def agent_status(self, agent_id: str) -> str:
        with self.lock:
            info = self.agents.get(agent_id)
            if not info:
                return "unknown"
            return self._get_status(info["process"])
    
    def assign_task(self, agent_id: str, task: str, tools: Optional[List[Dict]] = None, max_iterations: int = 5) -> str:
        task_id = str(uuid.uuid4())
        created_at = time.time()
        with self.lock:
            info = self.agents.get(agent_id)
            if not info:
                raise KeyError(f"Agent {agent_id} not found")
            
            task_info = {
                "task_id": task_id,
                "agent_id": agent_id,
                "status": "pending",
                "created_at": created_at,
                "missing_tools": [],
                "partial": False,
                "output": None
            }
            self.task_history[task_id] = task_info
            info["tasks"].append(task_id)
        
        try:
            self.executor.submit(self._do_assign_task, agent_id, task, task_id, tools, max_iterations)
        except RuntimeError:
            with self.lock:
                task_info["status"] = "error"
                task_info["error"] = "Orchestrator is shutting down"
            raise RuntimeError("Orchestrator is shutting down")
            
        return task_id

    def _do_assign_task(self, agent_id: str, task: str, task_id: str, tools: Optional[List[Dict]] = None, max_iterations: int = 5):
        with self.lock:
            info = self.agents.get(agent_id)
            task_info = self.task_history.get(task_id)
            if not info:
                if task_info:
                    task_info["status"] = "error"
                    task_info["error"] = "Agent disappeared before execution"
                return
            proc = info["process"]
            stdin_lock = info["stdin_lock"]
            busy_lock = info["busy"]
            
        if proc.poll() is not None:
            with self.lock: 
                task_info["status"] = "error"
                task_info["error"] = "Agent not running"
                task_info["finished_at"] = time.time()
            return
            
        if tools is None:
            with self.lock:
                tools = self.agent_tools.get(agent_id, [])
        
        msg = json.dumps({
            "method": "assign_task", 
            "params": {
                "task": task, 
                "task_id": task_id,
                "tools": tools,
                "max_iterations": max_iterations
            }
        }) + "\n"
        
        with busy_lock:
            with self.lock:
                task_info["status"] = "running"
                info["current_task_id"] = task_id
            
            try:
                with stdin_lock:
                    proc.stdin.write(msg)
                    proc.stdin.flush()
                    
                    line = _readline_with_timeout(proc.stdout, 600.0)
                    if line is None:
                        raise RuntimeError("Task execution timeout")
                    if not line:
                        raise RuntimeError("Agent disconnected")
                    
                    resp = json.loads(line)
                    result = self.storage.read(f"{agent_id}:result:{task_id}")
                    
                    missing = []
                    partial = False
                    output = None
                    if isinstance(result, dict):
                        missing = result.get("missing_tools", [])
                        partial = result.get("partial", False)
                        output = result.get("output")
                    else:
                        output = str(result)
                    
                    with self.lock:
                        task_info.update({
                            "status": "done",
                            "missing_tools": missing,
                            "partial": partial,
                            "output": output,
                            "finished_at": time.time()
                        })
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}")
                with self.lock:
                    task_info.update({
                        "status": "error",
                        "error": str(e),
                        "finished_at": time.time()
                    })
            finally:
                    with self.lock:
                        if info.get("current_task_id") == task_id:
                            info["current_task_id"] = None
                        # Remove from active tasks list in info
                        if task_id in info["tasks"]:
                            info["tasks"].remove(task_id)
                    self._save_history()

    def task_status(self, agent_id: str, task_id: str) -> Dict[str, Any]:
        with self.lock:
            task = self.task_history.get(task_id)
            if not task:
                return {"status": "task_not_found"}
            if task["agent_id"] != agent_id:
                return {"status": "task_agent_mismatch"}
            return task

    def task_result(self, agent_id: str, task_id: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            if task_id:
                task = self.task_history.get(task_id)
                if task and task["agent_id"] == agent_id:
                    result = self.storage.read(f"{agent_id}:result:{task_id}")
                    return {
                        "task_id": task_id, 
                        "status": task["status"],
                        "result": result or {"output": task.get("output")}
                    }
                return {"task_id": task_id, "status": "task_not_found", "result": None}
            
            last_task_id = self.storage.read(f"{agent_id}:last_task_id")
            if last_task_id:
                task = self.task_history.get(last_task_id)
                last_result = self.storage.read(f"{agent_id}:last_result")
                return {
                    "task_id": last_task_id, 
                    "status": task["status"] if task else "done", 
                    "result": last_result or ({"output": task.get("output")} if task else None)
                }
            return {"task_id": None, "status": "no_tasks", "result": None}
    
    def aggregate_results(self, task_list: List[Dict[str, str]]) -> Dict[str, Any]:
        aggregated = {}
        for item in task_list:
            aid = item.get("agent_id")
            tid = item.get("task_id")
            if aid and tid:
                res = self.task_result(aid, tid)
                stat = self.task_status(aid, tid)
                aggregated[f"{aid}:{tid}"] = {
                    "task_id": tid,
                    "agent_id": aid,
                    "status": stat.get("status"),
                    "missing_tools": stat.get("missing_tools", []),
                    "created_at": stat.get("created_at"),
                    "finished_at": stat.get("finished_at"),
                    "output": stat.get("output"),
                    "result": res.get("result")
                }
        return aggregated

    def broadcast(self, message: str) -> List[Dict[str, str]]:
        with self.lock:
            active_ids = [aid for aid, info in self.agents.items() if info["process"].poll() is None]
        
        # Parallel submission
        task_ids = list(self.executor.map(lambda aid: self.assign_task(aid, message), active_ids))
        
        results = []
        for aid, tid in zip(active_ids, task_ids):
            results.append({"agent_id": aid, "task_id": tid})
        return results
    
    def read_storage(self, key: str) -> Any:
        return self.storage.read(key)
    
    def write_storage(self, key: str, value: Any) -> None:
        if ":result:" in key or key.endswith(":last_result") or key.endswith(":last_task_id") or key.startswith("__orchestrator__"):
            raise ValueError("Protected storage key")
        self.storage.write(key, value)
    
    def list_storage(self) -> List[str]:
        return self.storage.list_keys()

    def storage_cleanup(self, pattern: str = "*", exclude: Optional[str] = None) -> int:
        return self.storage.cleanup(pattern, exclude)

    def cleanup_task_history(self, older_than_seconds: int = 3600) -> int:
        """Remove old tasks from history."""
        now = time.time()
        count = 0
        with self.lock:
            for tid in list(self.task_history.keys()):
                t = self.task_history[tid]
                if t.get("finished_at") and now - t["finished_at"] > older_than_seconds:
                    del self.task_history[tid]
                    count += 1
            if count > 0:
                self._save_history()
        return count
