#!/usr/bin/env python
"""Peon Orchestrator - MCP Server for managing AI agents."""

import asyncio
import json
import os
import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("peon-orchestrator")

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Import MCP
try:
    from mcp.server import Server
    import mcp.server.stdio
    from mcp.types import Tool, TextContent
    from pydantic import BaseModel
except ImportError as e:
    logger.error(f"MCP SDK not installed: {e}")
    sys.exit(1)

# Import orchestrator
try:
    from .orchestrator import AgentOrchestrator
except ImportError:
    try:
        from orchestrator import AgentOrchestrator
    except ImportError as e:
        logger.error(f"Failed to import orchestrator: {e}")
        sys.exit(1)


# Pydantic модели для параметров запросов
class ToolsListParams(BaseModel):
    pass

class ToolsCallParams(BaseModel):
    name: str
    arguments: Optional[Dict[str, Any]] = None


def create_tool_definitions() -> List[Dict[str, Any]]:
    """Create tool definitions as dictionaries."""
    return [
        {
            "name": "list_agents",
            "description": "List all managed agents with status, busy flag, current task and task counts",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "spawn_agent",
            "description": "Spawn a new agent with the given role and system prompt",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "role_name": {
                        "type": "string",
                        "description": "Role name (e.g., 'coder')"
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "System prompt for the agent"
                    },
                    "tools": {
                        "type": "array",
                        "description": "Optional list of tool definitions (OpenAI format) to make available to the agent",
                        "items": {"type": "object"}
                    }
                },
                "required": ["role_name", "system_prompt"]
            }
        },
        {
            "name": "stop_agent",
            "description": "Stop a running agent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "ID of the agent"
                    }
                },
                "required": ["agent_id"]
            }
        },
        {
            "name": "agent_status",
            "description": "Get the status of an agent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"}
                },
                "required": ["agent_id"]
            }
        },
        {
            "name": "assign_task",
            "description": "Assign a task to an agent asynchronously. Returns task_id immediately. Use task_status and task_result to track progress.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "task": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "object"}},
                    "max_iterations": {"type": "integer", "default": 5}
                },
                "required": ["agent_id", "task"]
            }
        },
        {
            "name": "task_status",
            "description": "Get detailed status of a specific task including missing_tools, partial flag, and timestamps.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "task_id": {"type": "string"}
                },
                "required": ["agent_id", "task_id"]
            }
        },
        {
            "name": "task_result",
            "description": "Get the result of a task enriched with task_id and status. If task_id is omitted, returns the last result.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "task_id": {"type": "string"}
                },
                "required": ["agent_id"]
            }
        },
        {
            "name": "aggregate_results",
            "description": "Aggregate results for multiple tasks including missing_tools, status and output.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_id": {"type": "string"},
                                "task_id": {"type": "string"}
                            },
                            "required": ["agent_id", "task_id"]
                        }
                    }
                },
                "required": ["tasks"]
            }
        },
        {
            "name": "broadcast",
            "description": "Send a message to all running agents in parallel. Returns list of {agent_id, task_id}.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"}
                },
                "required": ["message"]
            }
        },
        {
            "name": "read_storage",
            "description": "Read data from shared storage",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"}
                },
                "required": ["key"]
            }
        },
        {
            "name": "write_storage",
            "description": "Write data to shared storage. Handles JSON strings automatically. Protected keys are restricted.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"}
                },
                "required": ["key", "value"]
            }
        },
        {
            "name": "list_storage",
            "description": "List all keys in shared storage",
            "inputSchema": {"type": "object", "properties": {}}
        },
        {
            "name": "storage_cleanup",
            "description": "Remove keys from storage matching a glob pattern with optional exclusion.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "default": "*"},
                    "exclude": {"type": "string"}
                }
            }
        },
        {
            "name": "cleanup_task_history",
            "description": "Remove old tasks from history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "older_than_seconds": {"type": "integer", "default": 3600}
                }
            }
        }
    ]


async def execute_tool(orchestrator: AgentOrchestrator, name: str, arguments: Dict[str, Any]) -> str:
    """Execute a tool and return result as string."""
    try:
        logger.info(f"Calling tool: {name} with args: {arguments}")
        
        if name == "list_agents":
            result = await asyncio.to_thread(orchestrator.list_agents)
            
        elif name == "spawn_agent":
            result = await asyncio.to_thread(
                orchestrator.spawn_agent, 
                arguments.get("role_name"), 
                arguments.get("system_prompt"),
                arguments.get("tools")
            )
            
        elif name == "stop_agent":
            result = await asyncio.to_thread(orchestrator.stop_agent, arguments.get("agent_id"))
            
        elif name == "agent_status":
            result = await asyncio.to_thread(orchestrator.agent_status, arguments.get("agent_id"))
            
        elif name == "assign_task":
            result = await asyncio.to_thread(
                orchestrator.assign_task, 
                arguments.get("agent_id"), 
                arguments.get("task"),
                arguments.get("tools"),
                arguments.get("max_iterations", 5)
            )

        elif name == "task_status":
            result = await asyncio.to_thread(
                orchestrator.task_status, 
                arguments.get("agent_id"), 
                arguments.get("task_id")
            )
            
        elif name == "task_result":
            result = await asyncio.to_thread(
                orchestrator.task_result, 
                arguments.get("agent_id"),
                arguments.get("task_id")
            )

        elif name == "aggregate_results":
            result = await asyncio.to_thread(
                orchestrator.aggregate_results,
                arguments.get("tasks", [])
            )
            
        elif name == "broadcast":
            result = await asyncio.to_thread(orchestrator.broadcast, arguments.get("message"))
            
        elif name == "read_storage":
            result = await asyncio.to_thread(orchestrator.read_storage, arguments.get("key"))
            
        elif name == "write_storage":
            await asyncio.to_thread(orchestrator.write_storage, arguments.get("key"), arguments.get("value"))
            result = "Value written"
            
        elif name == "list_storage":
            result = await asyncio.to_thread(orchestrator.list_storage)
            
        elif name == "storage_cleanup":
            result = await asyncio.to_thread(
                orchestrator.storage_cleanup, 
                arguments.get("pattern", "*"),
                arguments.get("exclude")
            )
            result = f"Cleaned up {result} items"
            
        elif name == "cleanup_task_history":
            result = await asyncio.to_thread(
                orchestrator.cleanup_task_history,
                arguments.get("older_than_seconds", 3600)
            )
            result = f"Cleaned up {result} tasks"
            
        else:
            raise ValueError(f"Unknown tool: {name}")

        if isinstance(result, str):
            return result
        else:
            return json.dumps(result, ensure_ascii=False, default=str)
            
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}", exc_info=True)
        raise


async def main():
    """Run the MCP server."""
    try:
        orchestrator = AgentOrchestrator()
        logger.info(f"Using python executable: {sys.executable}")
        logger.info("Orchestrator initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize orchestrator: {e}")
        sys.exit(1)
    
    server = Server("peon-orchestrator")
    
    async def list_tools_handler(ctx: Any, params: ToolsListParams) -> Dict[str, Any]:
        return {"tools": create_tool_definitions()}
    
    async def call_tool_handler(ctx: Any, params: ToolsCallParams) -> Dict[str, Any]:
        try:
            name = params.name
            arguments = params.arguments or {}
            result = await execute_tool(orchestrator, name, arguments)
            return {"content": [{"type": "text", "text": result}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}
    
    server.add_request_handler("tools/list", ToolsListParams, list_tools_handler)
    server.add_request_handler("tools/call", ToolsCallParams, call_tool_handler)
    
    logger.info("Starting MCP server...")
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


def main_sync():
    """Synchronous entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main_sync()
