# Peon Orchestrator Usage Skill

This skill provides guidance on using the Peon AI Orchestrator for managing AI agents in the Peon AI ecosystem.

## Overview
The Peon Orchestrator is an MCP server that manages AI agents and provides shared, atomic storage for data exchange. It supports asynchronous task execution and persistent task history.

## Configuration
Key environment variables:
- `ORCHAI_API_KEY` - API key for LLM access.
- `ORCHAI_MODEL` - Model to use (e.g., `gpt-4o`).
- `PEON_STORAGE` - Path to shared storage (default: `.peon-orchestrator/storage`).

## Usage via MCP Client

### Agent Management
- `spawn_agent(role_name, system_prompt, tools?)` -> `agent_id`
- `list_agents()` -> List agents with `busy` status, `current_task_id`, `pending_tasks`, and `running_tasks`.
- `stop_agent(agent_id)` -> Gracefully stops the agent and marks its tasks as `agent_stopped`.

### Asynchronous Task Workflow
Tasks are asynchronous by default. The orchestrator returns a `task_id` immediately.

1. **Assign Task**:
   `assign_task(agent_id, task, tools?, max_iterations?)` -> `task_id`
   - `tools`: Optional list of OpenAI-format tools for the LLM.
   - `max_iterations`: Limit on tool-call loops (default: 5).

2. **Check Status**:
   `task_status(agent_id, task_id)` -> Returns:
   ```json
   {
     "status": "done",
     "missing_tools": ["tool_name"],
     "partial": false,
     "created_at": 1725875200.0,
     "finished_at": 1725875245.0
   }
   ```
   Statuses: `pending`, `running`, `done`, `error`, `agent_stopped`.

3. **Retrieve Result**:
   `task_result(agent_id, task_id?)` -> Returns:
   ```json
   {
     "task_id": "...",
     "status": "done",
     "result": {
       "output": "The agent's response text",
       "missing_tools": [],
       "partial": false
     }
   }
   ```

### Storage Operations
The storage is thread-safe and atomic.
- `read_storage(key)`
- `write_storage(key, value)` - Note: Keys like `:result:` and `__orchestrator__` are protected.
- `storage_cleanup(pattern?, exclude?)` - Uses glob patterns (e.g., `agent_*`).

## AI Conductor (Дирижер ИИ) Usage
The conductor manages the "orchestra" by coordinating agents through iterative loops.

### Iteration Strategy
When an agent completes a task, check the `missing_tools` and `partial` fields:
- **`missing_tools`**: List of tools the LLM tried to use but were not implemented in the agent.
- **`partial`**: True if the agent reached `max_iterations` before finishing.

**Conductor Logic**:
1. If `missing_tools` is not empty, spawn a new agent with the required tools or re-assign the task with a broader toolset.
2. If `partial` is true, re-assign the task with a higher `max_iterations`.

### Result Aggregation
Use `aggregate_results(tasks)` to collect outputs from multiple sub-tasks for final synthesis:
```python
results = await mcp.aggregate_results([
    {"agent_id": "coder_1", "task_id": "task_A"},
    {"agent_id": "tester_1", "task_id": "task_B"}
])
```

## Task History Management
Task metadata is persistent across restarts. To prevent memory leaks, use:
- `cleanup_task_history(older_than_seconds?)` - Removes tasks finished before the specified time (default: 3600s).

## Troubleshooting
- **Handshake Timeout**: Agent failed to start (check logs in stderr).
- **Busy Status**: Agent is executing a task; new tasks for this agent will stay in `pending`.
- **Protected Key Error**: Attempted to write to internal orchestrator keys.

## License
MIT License.