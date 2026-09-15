# Peon AI Orchestrator

MCP server orchestrator for managing AI agents (coder, reviewer, tester) in the Peon AI ecosystem.

## Features
- **Agent Management:** Dynamic spawning, stopping, and monitoring of agent processes.
- **Asynchronous Task Execution:** Assign tasks and receive a `task_id` immediately. Agents process tasks in parallel.
- **Persistent Task History:** Global history of all tasks, statuses, and results, saved to disk and accessible even after agent termination.
- **Shared Atomic Storage:** Thread-safe, file-based "pit" (`.peon-orchestrator/storage/`) for data exchange with safe key encoding (Base64).
- **AI Integration:** OpenAI support with configurable iteration limits and timeouts.
- **Conductor-Ready:** Detailed task statuses including `missing_tools` and `partial` flags for iterative agent coordination.

## Installation

It is recommended to use [uv](https://github.com/astral-sh/uv) for dependency management and execution.

### 1. Install uv
If `uv` is not installed yet:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install from GitHub
```bash
uv pip install git+https://github.com/prmgint/peon-ai-orchestrator.git
```

### 3. Local Installation (for development)
```bash
uv pip install -e .
```

## Eclipse Configuration (MCP)

1. Open MCP settings in Eclipse (Peon AI / MCP Settings).
2. Add a new server:
   - **Name:** `peon-orchestrator`
   - **Command:** `uvx`
   - **Args:** `peon-orchestrator`
3. Set environment variables: `ORCHAI_API_KEY`, `ORCHAI_MODEL` (e.g., `gpt-4o`).

## Usage
The orchestrator provides the following tools via MCP:

### Agent Management
- `spawn_agent(role_name, system_prompt, tools?)` - Create an agent. Returns `agent_id`.
- `list_agents()` - List agents with `busy` status and task counts.
- `stop_agent(agent_id)` - Stop an agent.

### Task Management
- `assign_task(agent_id, task, tools?, max_iterations?)` - Assign task asynchronously. Returns `task_id`.
- `task_status(agent_id, task_id)` - Get detailed status (`pending`, `running`, `done`, `error`, `agent_stopped`).
- `task_result(agent_id, task_id?)` - Get enriched result with `output` and `missing_tools`.
- `aggregate_results(tasks)` - Collect results for multiple `{agent_id, task_id}` pairs.
- `cleanup_task_history(older_than_seconds?)` - Remove old task metadata.

### Storage Operations
- `read_storage(key)` - Read value from shared storage.
- `write_storage(key, value)` - Write value (atomic). Protected keys are restricted.
- `list_storage()` - List all keys.
- `storage_cleanup(pattern?, exclude?)` - Remove keys using glob patterns.

## Conductor Workflow
1. **Spawn** agents with specific roles.
2. **Assign** a task to an agent.
3. **Poll** `task_status` until it is `done`.
4. **Analyze** `missing_tools` in the result.
5. **Iterate** by spawning new agents or reassigning with more tools if needed.
6. **Aggregate** sub-task results into a final synthesis.

## License
MIT License.
