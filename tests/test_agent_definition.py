"""Tests for agent definition parser and registry."""

import tempfile
from pathlib import Path

import pytest

from agent.agent_definition import (
    AgentDefinition,
    parse_agent_file,
    validate_agent_definition,
)
from agent.agent_registry import AgentRegistry


class TestAgentDefinition:
    """Test AgentDefinition dataclass."""
    
    def test_default_values(self):
        """Test default values."""
        agent = AgentDefinition(name="test")
        assert agent.name == "test"
        assert agent.model == ""  # empty = inherit from parent config
        assert agent.reasoning == "medium"
        assert agent.temperature == 0.7
        assert agent.top_p == 0.9
        assert agent.max_tokens == 4096
        assert agent.tools == []
        assert agent.skills == []
        assert agent.max_depth == 3
        assert agent.timeout == 300
        assert agent.prompt == ""
    
    def test_to_dict(self):
        """Test to_dict method."""
        agent = AgentDefinition(
            name="test",
            model="mimo-v2.5",
            reasoning="high",
            temperature=0.5,
            tools=["read_file", "terminal"],
            skills=["caveman"],
            prompt="Test prompt",
        )
        d = agent.to_dict()
        assert d["name"] == "test"
        assert d["model"] == "mimo-v2.5"
        assert d["reasoning"] == "high"
        assert d["temperature"] == 0.5
        assert d["tools"] == ["read_file", "terminal"]
        assert d["skills"] == ["caveman"]


class TestParseAgentFile:
    """Test parse_agent_file function."""
    
    def test_valid_file(self, tmp_path):
        """Test parsing valid agent file."""
        content = """---
name: test-agent
model: mimo-v2.5
reasoning: high
temperature: 0.5
tools: [read_file, terminal]
skills: [caveman]
---
You are a test agent."""
        
        file_path = tmp_path / "test.md"
        file_path.write_text(content)
        
        agent = parse_agent_file(file_path)
        assert agent is not None
        assert agent.name == "test-agent"
        assert agent.model == "mimo-v2.5"
        assert agent.reasoning == "high"
        assert agent.temperature == 0.5
        assert agent.tools == ["read_file", "terminal"]
        assert agent.skills == ["caveman"]
        assert agent.prompt == "You are a test agent."
    
    def test_missing_frontmatter(self, tmp_path):
        """Test parsing file without frontmatter."""
        content = "You are a test agent."
        
        file_path = tmp_path / "test.md"
        file_path.write_text(content)
        
        agent = parse_agent_file(file_path)
        assert agent is None
    
    def test_invalid_yaml(self, tmp_path):
        """Test parsing file with invalid YAML."""
        content = """---
name: test
invalid: [yaml
---
You are a test agent."""
        
        file_path = tmp_path / "test.md"
        file_path.write_text(content)
        
        agent = parse_agent_file(file_path)
        assert agent is None
    
    def test_missing_name(self, tmp_path):
        """Test parsing file without name (uses filename)."""
        content = """---
model: mimo-v2.5
---
You are a test agent."""
        
        file_path = tmp_path / "my-agent.md"
        file_path.write_text(content)
        
        agent = parse_agent_file(file_path)
        assert agent is not None
        assert agent.name == "my-agent"
    
    def test_string_tools(self, tmp_path):
        """Test parsing tools as comma-separated string."""
        content = """---
name: test
tools: read_file, terminal, patch
---
You are a test agent."""
        
        file_path = tmp_path / "test.md"
        file_path.write_text(content)
        
        agent = parse_agent_file(file_path)
        assert agent is not None
        assert agent.tools == ["read_file", "terminal", "patch"]
    
    def test_nonexistent_file(self):
        """Test parsing nonexistent file."""
        agent = parse_agent_file(Path("/nonexistent/file.md"))
        assert agent is None


class TestValidateAgentDefinition:
    """Test validate_agent_definition function."""
    
    def test_valid_definition(self):
        """Test valid definition."""
        agent = AgentDefinition(
            name="test",
            model="mimo-v2.5",
            reasoning="high",
            temperature=0.7,
            top_p=0.9,
            max_tokens=4096,
            prompt="Test prompt",
        )
        errors = validate_agent_definition(agent)
        assert errors == []
    
    def test_missing_name(self):
        """Test missing name."""
        agent = AgentDefinition(name="", prompt="Test")
        errors = validate_agent_definition(agent)
        assert "Name is required" in errors
    
    def test_invalid_name(self):
        """Test invalid name."""
        agent = AgentDefinition(name="test agent", prompt="Test")
        errors = validate_agent_definition(agent)
        assert any("Invalid name" in e for e in errors)
    
    def test_invalid_reasoning(self):
        """Test invalid reasoning."""
        agent = AgentDefinition(name="test", reasoning="invalid", prompt="Test")
        errors = validate_agent_definition(agent)
        assert any("Invalid reasoning" in e for e in errors)
    
    def test_invalid_temperature(self):
        """Test invalid temperature."""
        agent = AgentDefinition(name="test", temperature=3.0, prompt="Test")
        errors = validate_agent_definition(agent)
        assert any("Invalid temperature" in e for e in errors)
    
    def test_invalid_top_p(self):
        """Test invalid top_p."""
        agent = AgentDefinition(name="test", top_p=1.5, prompt="Test")
        errors = validate_agent_definition(agent)
        assert any("Invalid top_p" in e for e in errors)
    
    def test_missing_prompt(self):
        """Test missing prompt."""
        agent = AgentDefinition(name="test", prompt="")
        errors = validate_agent_definition(agent)
        assert "Prompt is required" in errors


class TestAgentRegistry:
    """Test AgentRegistry class."""
    
    def test_load_from_config(self, tmp_path):
        """Test loading agents from config."""
        # Create agent file
        content = """---
name: test-agent
model: mimo-v2.5
---
You are a test agent."""
        
        agent_file = tmp_path / "test.md"
        agent_file.write_text(content)
        
        # Create config
        config = {
            "delegate": {
                "test-agent": str(agent_file),
            }
        }
        
        registry = AgentRegistry()
        loaded = registry.load_from_config(config)
        
        assert loaded == 1
        assert registry.has_agent("test-agent")
        assert registry.get_agent("test-agent").model == "mimo-v2.5"
    
    def test_load_from_directory(self, tmp_path):
        """Test loading agents from directory."""
        # Create agent files
        content1 = """---
name: agent1
---
You are agent1."""
        
        content2 = """---
name: agent2
---
You are agent2."""
        
        (tmp_path / "agent1.md").write_text(content1)
        (tmp_path / "agent2.md").write_text(content2)
        
        registry = AgentRegistry()
        loaded = registry.load_from_directory(tmp_path)
        
        assert loaded == 2
        assert registry.has_agent("agent1")
        assert registry.has_agent("agent2")
    
    def test_list_agents(self, tmp_path):
        """Test listing agents."""
        content = """---
name: test
---
You are a test agent."""
        
        (tmp_path / "test.md").write_text(content)
        
        registry = AgentRegistry()
        registry.load_from_directory(tmp_path)
        
        agents = registry.list_agents()
        assert len(agents) == 1
        assert agents[0].name == "test"
    
    def test_get_agent_summary(self, tmp_path):
        """Test getting agent summary."""
        content = """---
name: test
model: mimo-v2.5
reasoning: high
tools: [read_file, terminal]
---
You are a test agent."""
        
        (tmp_path / "test.md").write_text(content)
        
        registry = AgentRegistry()
        registry.load_from_directory(tmp_path)
        
        summaries = registry.get_agent_summary()
        assert len(summaries) == 1
        assert summaries[0]["name"] == "test"
        assert summaries[0]["model"] == "mimo-v2.5"
        assert summaries[0]["tools_count"] == 2
