import sys
import os
import json
import logging
import asyncio
import signal
import threading
from pathlib import Path
from typing import Any, Dict, Optional, List, Callable

try:
    from .storage import SimpleStorage
except ImportError:
    from storage import SimpleStorage

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("peon-agent")

try:
    from openai import OpenAI
except ImportError:
    logger.warning("openai SDK not found, falling back to echo mode")
    OpenAI = None

def get_config() -> Dict[str, str]:
    return {
        "api_key": os.environ.get("ORCHAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("PEON_API_KEY", ""),
        "base_url": os.environ.get("ORCHAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or os.environ.get("PEON_BASE_URL", ""),
        "model": os.environ.get("ORCHAI_MODEL") or os.environ.get("OPENAI_MODEL") or os.environ.get("PEON_MODEL", "gpt-4o"),
    }

def get_default_storage_tools() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_storage",
                "description": "Read a value from the agent's storage by key",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"}
                    },
                    "required": ["key"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_storage",
                "description": "Write a value to the agent's storage by key",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"}
                    },
                    "required": ["key", "value"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_storage_keys",
                "description": "List all keys in the agent's storage",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }
    ]

async def execute_task(client: Optional[OpenAI], config: Dict[str, str], role: str, prompt: str, task: str, tools: Optional[List[Dict]] = None, available_tools: Optional[Dict[str, Callable]] = None, max_iterations: int = 5) -> Dict[str, Any]:
    if not client or not config["api_key"] or not config["api_key"].strip():
        logger.info("Using echo fallback (no API key or OpenAI SDK)")
        system_prompt = f"You are a specialized AI agent with role: {role}. {prompt}"
        return {"output": f"[ECHO] {system_prompt}\nTask: {task}", "missing_tools": [], "partial": False}

    if available_tools is None:
        available_tools = {}

    try:
        logger.info(f"Sending task to model...")
        messages = [
            {"role": "system", "content": f"You are a specialized AI agent with role: {role}. {prompt}"},
            {"role": "user", "content": task}
        ]
        
        missing_tools_set = set()
        final_content = "Error: No response from model"
        partial = False
        
        for iteration in range(max_iterations):
            response = client.chat.completions.create(
                model=config["model"],
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
                timeout=120.0
            )
            message = response.choices[0].message
            
            if message.tool_calls:
                logger.info(f"Agent {role} received {len(message.tool_calls)} tool call(s)")
                messages.append(message)
                
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_id = tool_call.id
                    
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON in tool arguments: {e}")
                        tool_result = f"Error: Invalid JSON in tool arguments: {str(e)}"
                        missing_tools_set.add(tool_name)
                    else:
                        if tool_name in available_tools:
                            try:
                                logger.info(f"Executing tool: {tool_name}")
                                tool_result = available_tools[tool_name](**tool_args)
                                if not isinstance(tool_result, str):
                                    tool_result = json.dumps(tool_result, ensure_ascii=False)
                            except Exception as e:
                                logger.error(f"Tool execution failed: {e}")
                                tool_result = f"Error executing tool {tool_name}: {str(e)}"
                        else:
                            logger.warning(f"Tool {tool_name} not available")
                            tool_result = f"Error: Tool {tool_name} not available"
                            missing_tools_set.add(tool_name)
                    
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": tool_result
                    })
            else:
                final_content = message.content if message.content else "Error: Empty response from model"
                break
        
        if iteration == max_iterations - 1 and message.tool_calls:
            final_content = message.content if message and message.content else "Error: Max tool call iterations reached"
            partial = True
        
        return {
            "output": final_content,
            "missing_tools": list(missing_tools_set),
            "partial": partial
        }
        
    except Exception as e:
        logger.error(f"LLM execution failed: {e}")
        return {"output": f"Error executing task: {str(e)}", "missing_tools": [], "partial": False}

def send_response(data: Dict[str, Any]):
    print(json.dumps(data, ensure_ascii=False), flush=True)
    sys.stdout.flush()

async def main():
    agent_id = os.environ.get("PEON_AGENT_ID", "unknown")
    role = os.environ.get("PEON_ROLE", "assistant")
    prompt = os.environ.get("PEON_PROMPT", "")
    config = get_config()
    
    logger.info(f"Agent {agent_id} ({role}) initialized")
    
    storage = SimpleStorage()
    client = None
    if OpenAI and config["api_key"]:
        client_kwargs = {"api_key": config["api_key"]}
        if config["base_url"]:
            client_kwargs["base_url"] = config["base_url"]
        client = OpenAI(**client_kwargs)

    available_tools = {
        "read_storage": storage.read,
        "write_storage": storage.write,
        "list_storage_keys": storage.list_keys,
    }

    default_tools = get_default_storage_tools()
    spawn_tools_env = os.environ.get("PEON_SPAWN_TOOLS")
    spawn_tools = []
    if spawn_tools_env:
        try:
            spawn_tools = json.loads(spawn_tools_env)
        except Exception:
            pass
    
    combined_tools = default_tools + spawn_tools

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    
    def handle_signal():
        logger.info("Received stop signal, exiting...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    send_response({"status": "ready", "agent_id": agent_id})

    input_queue = asyncio.Queue()
    def read_stdin():
        while not stop_event.is_set():
            line = sys.stdin.readline()
            if not line:
                input_queue.put_nowait(None)
                break
            input_queue.put_nowait(line)

    threading.Thread(target=read_stdin, daemon=True).start()

    while not stop_event.is_set():
        try:
            get_task = asyncio.create_task(input_queue.get())
            stop_task = asyncio.create_task(asyncio.sleep(0.1))
            
            done, pending = await asyncio.wait([get_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
            
            for p in pending:
                p.cancel()

            if stop_event.is_set():
                break
                
            line = await get_task
            if line is None:
                break
            
            req = json.loads(line)
            method = req.get("method")
            params = req.get("params", {})
            task_id = params.get("task_id", "default")
            
            if method == "assign_task":
                task = params.get("task", "")
                tools = params.get("tools")
                max_iter = params.get("max_iterations", 5)
                
                if tools is None:
                    tools = combined_tools
                
                result = await execute_task(client, config, role, prompt, task, tools, available_tools, max_iter)
                storage.write(f"{agent_id}:result:{task_id}", result)
                storage.write(f"{agent_id}:last_result", result)
                storage.write(f"{agent_id}:last_task_id", task_id)

                logger.info(f"Agent {agent_id} task {task_id} completed")
                send_response({
                    "result": "ok",
                    "task_id": task_id,
                    "output": result.get("output", ""),
                    "missing_tools": result.get("missing_tools", []),
                    "partial": result.get("partial", False)
                })
            
            elif method == "read_storage":
                key = params.get("key")
                val = storage.read(key) if key else None
                send_response({"result": val} if key else {"error": "missing key"})
            
            elif method == "stop":
                break
            
            else:
                send_response({"error": f"unknown method {method}"})
                    
        except Exception as e:
            logger.error(f"Agent {agent_id} error: {e}")
            send_response({"error": str(e)})

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass