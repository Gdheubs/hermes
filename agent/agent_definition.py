"""Agent definition parser for .md files with YAML frontmatter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class AgentDefinition:
    """Agent definition loaded from .md file."""
    
    # Identity
    name: str
    model: str = ""  # empty = inherit from config
    
    # Provider (empty = inherit from config)
    base_url: str = ""
    provider: str = ""
    api_mode: str = ""
    api_key: str = ""
    
    # Generation
    reasoning: str = "medium"
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 4096
    context_length: int = 0  # 0 = inherit from model default
    
    # Capabilities
    tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    skill_path: str = ""  # empty = default ~/.hermes/skills
    disabled_toolsets: List[str] = field(default_factory=list)
    
    # Delegation
    max_depth: int = 3
    timeout: int = 300
    
    # Compression (0 = inherit from config)
    compression_threshold: float = 0.0   # compress when context usage exceeds this ratio
    compression_target_ratio: float = 0.0  # fraction of threshold to preserve as recent tail
    
    # System
    prompt: str = ""
    source_path: Optional[Path] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "provider": self.provider,
            "api_mode": self.api_mode,
            "reasoning": self.reasoning,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "context_length": self.context_length,
            "compression_threshold": self.compression_threshold,
            "compression_target_ratio": self.compression_target_ratio,
            "tools": self.tools,
            "skills": self.skills,
            "skill_path": self.skill_path,
            "disabled_toolsets": self.disabled_toolsets,
            "max_depth": self.max_depth,
            "timeout": self.timeout,
            "prompt": self.prompt[:100] + "..." if len(self.prompt) > 100 else self.prompt,
            "source_path": str(self.source_path) if self.source_path else None,
        }


def parse_agent_file(file_path: Path) -> Optional[AgentDefinition]:
    """Parse a markdown agent definition file.
    
    Args:
        file_path: Path to .md file with YAML frontmatter
        
    Returns:
        AgentDefinition if valid, None if invalid
    """
    try:
        if not file_path.exists():
            return None
        
        content = file_path.read_text(encoding="utf-8")
        
        # Split frontmatter and body
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
        if not match:
            return None
        
        frontmatter_str, body = match.groups()
        
        # Parse YAML frontmatter
        try:
            frontmatter = yaml.safe_load(frontmatter_str)
        except yaml.YAMLError:
            # Try to salvage by replacing common invalid patterns
            try:
                sanitized = frontmatter_str.replace('***', '4096')
                frontmatter = yaml.safe_load(sanitized)
            except yaml.YAMLError:
                return None
        
        if not isinstance(frontmatter, dict):
            return None
        
        # Extract name (required)
        name = frontmatter.get('name')
        if not name:
            # Use filename as fallback
            name = file_path.stem
        
        # Extract optional fields with defaults
        # Use (x or "") to convert YAML None to empty string
        model = str(frontmatter.get('model') or '')
        base_url = str(frontmatter.get('base_url') or '')
        provider = str(frontmatter.get('provider') or '')
        api_mode = str(frontmatter.get('api_mode') or '')
        api_key = str(frontmatter.get('api_key') or '')
        reasoning = str(frontmatter.get('reasoning', 'medium'))
        temperature = float(frontmatter.get('temperature', 0.7))
        top_p = float(frontmatter.get('top_p', 0.9))
        max_tokens = int(frontmatter.get('max_tokens', 4096))
        context_length = int(frontmatter.get('context_length', 0))
        compression_threshold = float(frontmatter.get('compression_threshold', 0.0))
        compression_target_ratio = float(frontmatter.get('compression_target_ratio', 0.0))
        
        # Tools and skills as lists
        tools = frontmatter.get('tools', [])
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(',')]
        elif not isinstance(tools, list):
            tools = []
        
        skills = frontmatter.get('skills', [])
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(',')]
        elif not isinstance(skills, list):
            skills = []
        skill_path = str(frontmatter.get('skill_path', ''))
        
        disabled_toolsets = frontmatter.get('disabled_toolsets', [])
        if isinstance(disabled_toolsets, str):
            disabled_toolsets = [d.strip() for d in disabled_toolsets.split(',')]
        elif not isinstance(disabled_toolsets, list):
            disabled_toolsets = []
        
        # Delegation settings
        max_depth = int(frontmatter.get('max_depth', 3))
        timeout = int(frontmatter.get('timeout', 300))
        
        return AgentDefinition(
            name=name,
            model=model,
            base_url=base_url,
            provider=provider,
            api_mode=api_mode,
            api_key=api_key,
            reasoning=reasoning,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            context_length=context_length,
            compression_threshold=compression_threshold,
            compression_target_ratio=compression_target_ratio,
            tools=tools,
            skills=skills,
            skill_path=skill_path,
            disabled_toolsets=disabled_toolsets,
            max_depth=max_depth,
            timeout=timeout,
            prompt=body.strip(),
            source_path=file_path,
        )
    except Exception as e:
        # Log error but don't raise
        print(f"Error parsing agent file {file_path}: {e}")
        return None


def validate_agent_definition(agent_def: AgentDefinition) -> List[str]:
    """Validate an agent definition.
    
    Args:
        agent_def: AgentDefinition to validate
        
    Returns:
        List of validation errors (empty if valid)
    """
    errors = []
    
    # Name validation
    if not agent_def.name:
        errors.append("Name is required")
    elif not re.match(r'^[a-zA-Z0-9_-]+$', agent_def.name):
        errors.append(f"Invalid name: {agent_def.name}. Use alphanumeric, underscore, or hyphen.")
    
    # Model validation (optional — inherits from parent if empty)
    # Only validate format if explicitly set
    if agent_def.model and not re.match(r'^[a-zA-Z0-9_./-]+$', agent_def.model):
        errors.append(f"Invalid model: {agent_def.model}")
    
    # Reasoning validation
    valid_reasoning = {'none', 'low', 'medium', 'high', 'max'}
    if agent_def.reasoning not in valid_reasoning:
        errors.append(f"Invalid reasoning: {agent_def.reasoning}. Use: {', '.join(valid_reasoning)}")
    
    # Temperature validation
    if not (0.0 <= agent_def.temperature <= 2.0):
        errors.append(f"Invalid temperature: {agent_def.temperature}. Must be 0.0-2.0")
    
    # Top_p validation
    if not (0.0 <= agent_def.top_p <= 1.0):
        errors.append(f"Invalid top_p: {agent_def.top_p}. Must be 0.0-1.0")
    
    # Max_tokens validation
    if agent_def.max_tokens < 1:
        errors.append(f"Invalid max_tokens: {agent_def.max_tokens}. Must be > 0")
    
    # Max_depth validation
    if agent_def.max_depth < 1:
        errors.append(f"Invalid max_depth: {agent_def.max_depth}. Must be > 0")
    
    # Timeout validation
    if agent_def.timeout < 1:
        errors.append(f"Invalid timeout: {agent_def.timeout}. Must be > 0")
    
    # Prompt validation
    if not agent_def.prompt:
        errors.append("Prompt is required")
    
    return errors
