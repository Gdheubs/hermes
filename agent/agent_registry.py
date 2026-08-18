"""Agent registry for loading and managing agent definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import yaml

from agent.agent_definition import (
    AgentDefinition,
    parse_agent_file,
    validate_agent_definition,
)


class AgentRegistry:
    """Loads and manages agent definitions from .md files."""
    
    def __init__(self):
        self.agents: Dict[str, AgentDefinition] = {}
        self._loaded = False
    
    def load_from_config(self, config: dict) -> int:
        """Load agents from config.yaml delegate section.
        
        Args:
            config: Hermes config dict
            
        Returns:
            Number of agents loaded
        """
        delegate_config = config.get('delegate', {})
        if not isinstance(delegate_config, dict):
            return 0
        
        loaded = 0
        for name, path_str in delegate_config.items():
            if not isinstance(path_str, str):
                continue
            
            # Expand path
            file_path = Path(path_str).expanduser()
            
            # Load agent definition
            agent = parse_agent_file(file_path)
            if agent:
                # Use name from config if not in file
                if not agent.name or agent.name == file_path.stem:
                    agent.name = name
                
                # Validate
                errors = validate_agent_definition(agent)
                if errors:
                    print(f"Warning: Agent '{name}' has validation errors: {errors}")
                    continue
                
                self.agents[name] = agent
                loaded += 1
            else:
                print(f"Warning: Failed to load agent '{name}' from {file_path}")
        
        self._loaded = True
        return loaded
    
    def load_from_directory(self, directory: Path) -> int:
        """Load all agent definitions from a directory.
        
        Args:
            directory: Directory containing .md files
            
        Returns:
            Number of agents loaded
        """
        if not directory.exists():
            return 0
        
        loaded = 0
        for file_path in directory.glob('*.md'):
            agent = parse_agent_file(file_path)
            if agent:
                # Validate
                errors = validate_agent_definition(agent)
                if errors:
                    print(f"Warning: Agent '{agent.name}' has validation errors: {errors}")
                    continue
                
                # Check for duplicates (silent skip or log debug)
                if agent.name in self.agents:
                    continue
                
                self.agents[agent.name] = agent
                loaded += 1
        
        return loaded
    
    def get_agent(self, name: str) -> Optional[AgentDefinition]:
        """Get agent by name.
        
        Args:
            name: Agent name
            
        Returns:
            AgentDefinition if found, None otherwise
        """
        return self.agents.get(name)
    
    def list_agents(self) -> List[AgentDefinition]:
        """List all available agents.
        
        Returns:
            List of AgentDefinition objects
        """
        return list(self.agents.values())
    
    def list_agent_names(self) -> List[str]:
        """List all available agent names.
        
        Returns:
            List of agent names
        """
        return list(self.agents.keys())
    
    def has_agent(self, name: str) -> bool:
        """Check if agent exists.
        
        Args:
            name: Agent name
            
        Returns:
            True if agent exists
        """
        return name in self.agents
    
    def reload(self, config: dict) -> int:
        """Reload agents from config.
        
        Args:
            config: Hermes config dict
            
        Returns:
            Number of agents loaded
        """
        self.agents.clear()
        self._loaded = False
        return self.load_from_config(config)
    
    def get_agent_summary(self) -> List[Dict]:
        """Get summary of all agents.
        
        Returns:
            List of agent summary dicts
        """
        summaries = []
        for agent in self.agents.values():
            summaries.append({
                "name": agent.name,
                "model": agent.model,
                "reasoning": agent.reasoning,
                "temperature": agent.temperature,
                "tools_count": len(agent.tools),
                "skills_count": len(agent.skills),
                "source": str(agent.source_path) if agent.source_path else None,
            })
        return summaries


# Global registry instance
_registry: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    """Get the global agent registry instance.
    
    Returns:
        AgentRegistry instance
    """
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


def load_agents_from_config(config: dict) -> int:
    """Load agents from config into global registry.
    
    Args:
        config: Hermes config dict
        
    Returns:
        Number of agents loaded
    """
    registry = get_agent_registry()
    return registry.load_from_config(config)


def get_agent(name: str) -> Optional[AgentDefinition]:
    """Get agent by name from global registry.
    
    Args:
        name: Agent name
        
    Returns:
        AgentDefinition if found, None otherwise
    """
    registry = get_agent_registry()
    return registry.get_agent(name)
