"""MCP agent tools — expose agent definitions as callable MCP tools.

Each registered agent becomes a tool the main agent can call:
  - agent_<name>(message) → send message to agent, get response
  - agent_list() → list available agents
  - agent_info(name) → show agent details
  - agent_add(name, path) → add agent to config
  - agent_remove(name) → remove agent from config
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Agent registry reference (loaded lazily)
_registry = None


def _get_registry():
    """Get or load the agent registry."""
    global _registry
    if _registry is None:
        from agent.agent_registry import get_agent_registry
        _registry = get_agent_registry()
        
        # Load from config if not loaded yet
        if not _registry._loaded:
            try:
                from hermes_cli.config import load_config
                config = load_config()
                _registry.load_from_config(config)
            except Exception:
                pass
            
            # Also scan default directories
            for d in (Path.home() / ".hermes" / "agents", Path.cwd() / ".agents"):
                if d.exists():
                    _registry.load_from_directory(d)
    
    return _registry


def get_agent_tools() -> List[Dict[str, Any]]:
    """Get MCP tool definitions for all registered agents.
    
    Returns:
        List of tool definitions in MCP format
    """
    registry = _get_registry()
    tools = []
    
    for agent_def in registry.list_agents():
        tool = {
            "name": f"agent_{agent_def.name}",
            "description": (
                f"Delegate a task to the '{agent_def.name}' agent. "
                f"Model: {agent_def.model or 'inherit'}, Reasoning: {agent_def.reasoning}. "
                f"{agent_def.prompt[:100]}..."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Task or message to send to the agent",
                    },
                },
                "required": ["message"],
            },
        }
        tools.append(tool)
    
    # Add agent_list tool
    tools.append({
        "name": "agent_list",
        "description": "List all available agent definitions with their config",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    })
    
    # Add agent_info tool
    tools.append({
        "name": "agent_info",
        "description": "Show detailed info about a specific agent (config, persona, skills)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Agent name to inspect",
                },
            },
            "required": ["name"],
        },
    })
    
    # Add agent_add tool
    tools.append({
        "name": "agent_add",
        "description": (
            "Add a new agent definition. Creates .md file from template and registers in config. "
            "Usage: agent_add(name='coder', model='mimo-v2.5', reasoning='medium', temperature=0.3)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Agent name (alphanumeric, underscore, hyphen)",
                },
                "model": {
                    "type": "string",
                    "description": "Model name (empty = inherit from parent config)",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Reasoning level: none, low, medium, high, max (default: medium)",
                },
                "temperature": {
                    "type": "number",
                    "description": "Temperature 0.0-1.0 (default: 0.7)",
                },
                "prompt": {
                    "type": "string",
                    "description": "Agent persona/system prompt (optional, edit .md file later)",
                },
                "skill_path": {
                    "type": "string",
                    "description": "Custom skills directory path (optional)",
                },
                "tools": {
                    "type": "string",
                    "description": "Comma-separated tool list (optional, default: all)",
                },
            },
            "required": ["name"],
        },
    })
    
    # Add agent_remove tool
    tools.append({
        "name": "agent_remove",
        "description": "Remove an agent definition from config (does not delete .md file)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Agent name to remove",
                },
            },
            "required": ["name"],
        },
    })
    
    return tools


def handle_agent_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Handle an agent tool call.
    
    Args:
        tool_name: Name of the tool (e.g., "agent_coder", "agent_list")
        arguments: Tool arguments
        
    Returns:
        Tool result as string
    """
    registry = _get_registry()
    
    # Handle agent_list
    if tool_name == "agent_list":
        agents = registry.list_agents()
        if not agents:
            return "No agents available."
        
        result = []
        for a in agents:
            result.append(
                f"- {a.name}: model={a.model or 'inherit'}, reasoning={a.reasoning}, "
                f"temp={a.temperature}, tools={len(a.tools)}, skills={len(a.skills)}, "
                f"skill_path={a.skill_path or 'default'}"
            )
        return "\n".join(result)
    
    # Handle agent_info
    if tool_name == "agent_info":
        name = arguments.get("name", "")
        agent_def = registry.get_agent(name)
        if not agent_def:
            return f"Error: Agent '{name}' not found."
        
        return (
            f"Agent: {agent_def.name}\n"
            f"Model: {agent_def.model or 'inherit from parent'}\n"
            f"Provider: {agent_def.provider or 'inherit from parent'}\n"
            f"Base URL: {agent_def.base_url or 'inherit from parent'}\n"
            f"Reasoning: {agent_def.reasoning}\n"
            f"Temperature: {agent_def.temperature}\n"
            f"Top P: {agent_def.top_p}\n"
            f"Max Tokens: {agent_def.max_tokens}\n"
            f"Context Length: {agent_def.context_length or 'inherit'}\n"
            f"Compression Threshold: {agent_def.compression_threshold}\n"
            f"Compression Target Ratio: {agent_def.compression_target_ratio}\n"
            f"Tools: {', '.join(agent_def.tools) if agent_def.tools else 'all'}\n"
            f"Skills: {', '.join(agent_def.skills) if agent_def.skills else 'all'}\n"
            f"Skill Path: {agent_def.skill_path or 'default (~/.hermes/skills)'}\n"
            f"Max Depth: {agent_def.max_depth}\n"
            f"Timeout: {agent_def.timeout}\n"
            f"Source: {agent_def.source_path}\n"
            f"\n--- Persona ---\n{agent_def.prompt}"
        )
    
    # Handle agent_add
    if tool_name == "agent_add":
        name = arguments.get("name", "")
        if not name:
            return "Error: Agent name is required."
        
        # Validate name
        import re
        if not re.match(r'^[a-zA-Z0-9_-]+$', name):
            return f"Error: Invalid name '{name}'. Use alphanumeric, underscore, or hyphen."
        
        # Check if already exists
        if registry.has_agent(name):
            return f"Error: Agent '{name}' already exists. Use agent_remove first or choose a different name."
        
        # Build template
        model = arguments.get("model", "")
        reasoning = arguments.get("reasoning", "medium")
        temperature = arguments.get("temperature", 0.7)
        prompt = arguments.get("prompt", f"You are a {name} agent.")
        skill_path = arguments.get("skill_path", "")
        tools_str = arguments.get("tools", "")
        
        tools_list = "[read_file, search_files, terminal]" if not tools_str else f"[{tools_str}]"
        
        template = f"""---
name: {name}
model: {model}
provider:
base_url:
api_mode:
api_key:
reasoning: {reasoning}
temperature: {temperature}
top_p: 0.9
max_tokens: 4096
context_length: 0
compression_threshold: 0.0
compression_target_ratio: 0.0
tools: {tools_list}
skills: []
skill_path: {skill_path}
max_depth: 3
timeout: 300
---
{prompt}
"""
        
        # Write file
        agents_dir = Path.home() / ".hermes" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        file_path = agents_dir / f"{name}.md"
        
        if file_path.exists():
            return f"Error: File {file_path} already exists."
        
        file_path.write_text(template)
        
        # Add to config
        try:
            from hermes_cli.config import load_config, save_config
            config = load_config()
            if "delegate" not in config:
                config["delegate"] = {}
            config["delegate"][name] = str(file_path)
            save_config(config)
        except Exception as exc:
            return f"Agent file created at {file_path}, but failed to add to config: {exc}. Add manually: hermes config set delegate.{name} {file_path}"
        
        # Reload registry
        registry.agents.clear()
        registry._loaded = False
        
        return f"Agent '{name}' created at {file_path} and added to config. Edit the .md file to customize persona and skills."
    
    # Handle agent_remove
    if tool_name == "agent_remove":
        name = arguments.get("name", "")
        if not name:
            return "Error: Agent name is required."
        
        if not registry.has_agent(name):
            return f"Error: Agent '{name}' not found."
        
        # Remove from config
        try:
            from hermes_cli.config import load_config, save_config
            config = load_config()
            delegate = config.get("delegate", {})
            if name in delegate:
                del delegate[name]
                config["delegate"] = delegate
                save_config(config)
        except Exception as exc:
            return f"Failed to remove from config: {exc}"
        
        # Remove from registry
        if name in registry.agents:
            del registry.agents[name]
        
        return f"Agent '{name}' removed from config. .md file preserved."
    
    # Handle agent_<name> delegation
    if tool_name.startswith("agent_"):
        agent_name = tool_name[6:]  # Remove "agent_" prefix
        agent_def = registry.get_agent(agent_name)
        
        if not agent_def:
            return f"Error: Agent '{agent_name}' not found."
        
        message = arguments.get("message", "")
        if not message:
            return "Error: No message provided."
        
        return _delegate_to_agent(agent_def, message)
    
    return f"Error: Unknown agent tool '{tool_name}'."


def _delegate_to_agent(agent_def, message: str) -> str:
    """Delegate a task to an agent using its definition.
    
    Args:
        agent_def: AgentDefinition to use
        message: Task message
        
    Returns:
        Agent's response
    """
    try:
        # Import delegate_task functionality
        from model_tools import handle_function_call
        
        # Build delegate_task args with agent config
        args = {
            "task": message,
            "agent": agent_def.name,
        }
        
        # Call delegate_task with agent name
        result = handle_function_call("delegate_task", args)
        
        if isinstance(result, str):
            return result
        return json.dumps(result)
        
    except Exception as e:
        logger.exception("Failed to delegate to agent %s", agent_def.name)
        return f"Error delegating to agent '{agent_def.name}': {e}"


def register_agent_tools(registry) -> List[Dict[str, Any]]:
    """Register agent tools with the given registry.
    
    Args:
        registry: Tool registry to register with
        
    Returns:
        List of registered tool definitions
    """
    tools = get_agent_tools()
    
    for tool in tools:
        try:
            registry.register_tool(
                name=tool["name"],
                description=tool["description"],
                input_schema=tool["inputSchema"],
                handler=lambda args, _tool=tool: handle_agent_tool(_tool["name"], args),
            )
        except Exception as e:
            logger.warning("Failed to register agent tool %s: %s", tool["name"], e)
    
    return tools
