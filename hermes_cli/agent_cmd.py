"""CLI commands for agent definition management (argparse)."""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path


def build_agent_parser(subparsers, cmd_agent=None):
    """Register `hermes agent` subcommands on the given subparsers action."""

    agent_parser = subparsers.add_parser(
        "agent",
        help="Manage agent definitions (.md files with config + persona)",
        description=(
            "Define, list, validate, and inspect agent definitions. "
            "Agents are markdown files with YAML frontmatter for config "
            "(model, reasoning, temperature, tools, skills) and a body "
            "for the system prompt.  Place them in ~/.hermes/agents/ "
            "or .agents/ and reference from config.yaml under 'delegate:'."
        ),
    )
    agent_sub = agent_parser.add_subparsers(dest="agent_command")

    # hermes agent new <name>
    new_p = agent_sub.add_parser(
        "new",
        help="Create new agent definition from template",
    )
    new_p.add_argument("name", help="Agent name (used as filename)")
    new_p.add_argument("--model", "-m", default=None,
                       help="Model name (default: from config)")
    new_p.add_argument("--reasoning", "-r", default="medium",
                       choices=["none", "low", "medium", "high", "max"],
                       help="Reasoning level (default: medium)")
    new_p.add_argument("--temperature", "-t", type=float, default=0.7,
                       help="Temperature (default: 0.7)")
    new_p.add_argument("--global", dest="global_agent", action="store_true",
                       help="Create in ~/.hermes/agents/ (default)")
    new_p.add_argument("--local", action="store_true",
                       help="Create in .agents/ (project-local)")

    # hermes agent list
    list_p = agent_sub.add_parser(
        "list", aliases=["ls"],
        help="List available agent definitions",
    )
    list_p.add_argument("--json", dest="as_json", action="store_true",
                        help="Output as JSON")

    # hermes agent show <name>
    show_p = agent_sub.add_parser("show", help="Show agent definition details")
    show_p.add_argument("name", help="Agent name")

    # hermes agent validate <name>
    val_p = agent_sub.add_parser("validate", help="Validate an agent definition")
    val_p.add_argument("name", help="Agent name")

    # hermes agent add <name> <path>
    add_p = agent_sub.add_parser("add", help="Add existing agent file to config")
    add_p.add_argument("name", help="Agent name for delegate reference")
    add_p.add_argument("path", help="Path to .md agent definition file")

    # hermes agent remove <name>
    rm_p = agent_sub.add_parser("remove", aliases=["rm"],
                                help="Remove agent from config.yaml")
    rm_p.add_argument("name", help="Agent name to remove")

    # hermes agent test <name> [prompt]
    test_p = agent_sub.add_parser(
        "test",
        help="One-shot test: spawn agent with its own persona and send a prompt",
    )
    test_p.add_argument("name", help="Agent name to test")
    test_p.add_argument("prompt", nargs="?", default=None,
                        help="Prompt to send (default: auto-generated test)")
    test_p.add_argument("--json", dest="as_json", action="store_true",
                        help="Output raw JSON result")
    test_p.add_argument("--model", default=None,
                        help="Override agent model")
    test_p.add_argument("--no-tools", action="store_true",
                        help="Disable tools (prompt-only test)")

    if cmd_agent is not None:
        agent_parser.set_defaults(func=cmd_agent)

    return agent_parser


def cmd_agent(args):
    """Dispatch handler for `hermes agent`."""
    sub = getattr(args, "agent_command", None)
    if sub is None:
        # No subcommand — print help
        parser = build_agent_parser(None)
        parser.print_help()
        return 0

    if sub == "new":
        return _cmd_agent_new(args)
    if sub in ("list", "ls"):
        return _cmd_agent_list(args)
    if sub == "show":
        return _cmd_agent_show(args)
    if sub == "validate":
        return _cmd_agent_validate(args)
    if sub == "add":
        return _cmd_agent_add(args)
    if sub in ("remove", "rm"):
        return _cmd_agent_remove(args)
    if sub == "test":
        return _cmd_agent_test(args)

    print(f"Unknown agent subcommand: {sub}", file=sys.stderr)
    return 1


# ── Template ──────────────────────────────────────────────────────────

_TEMPLATE = """---
name: {name}
model: {model}
base_url:                # empty = inherit from config
provider:                # empty = inherit from config
api_mode:                # empty = inherit from config
api_key:                 # empty = inherit from config
reasoning: {reasoning}
temperature: {temperature}
top_p: 0.9
max_tokens: 4096          # max output tokens (NOT context length)
context_length: 0         # 0 = inherit from model default
compression_threshold: 0.0   # 0 = inherit from config (e.g. 0.50 = compress at 50%)
compression_target_ratio: 0.0  # 0 = inherit from config (e.g. 0.20 = keep 20% as tail)
tools: [read_file, search_files, terminal]
skills: []
skill_path:              # empty = default ~/.hermes/skills (e.g. ~/.hermes/skills/caveman)
max_depth: 3
timeout: 300
---
You are a {name} agent.

[Describe what this agent does and how it should behave.]

Rules:
1. [Rule 1]
2. [Rule 2]
3. [Rule 3]
"""


# ── Helpers ───────────────────────────────────────────────────────────

def _load_registry():
    """Load agent registry from config + default directories."""
    from agent.agent_registry import AgentRegistry
    registry = AgentRegistry()

    # Try config.yaml
    try:
        from hermes_cli.config import load_config
        config = load_config()
        registry.load_from_config(config)
    except Exception:
        pass

    # Also scan default directories
    for d in (Path.home() / ".hermes" / "agents", Path.cwd() / ".agents"):
        if d.exists():
            registry.load_from_directory(d)

    return registry


def _cmd_agent_new(args):
    """Create new agent definition from template."""
    name = args.name
    model = args.model
    reasoning = args.reasoning
    temperature = args.temperature
    use_local = args.local

    # Get default model from config if not specified
    if not model:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            raw_model = cfg.get("model", "")
            if isinstance(raw_model, dict):
                model = raw_model.get("default", "") or "mimo-v2.5"
            else:
                model = str(raw_model) or "mimo-v2.5"
        except Exception:
            model = "mimo-v2.5"

    # Determine directory
    if use_local:
        agents_dir = Path.cwd() / ".agents"
    else:
        agents_dir = Path.home() / ".hermes" / "agents"

    agents_dir.mkdir(parents=True, exist_ok=True)
    file_path = agents_dir / f"{name}.md"

    # Check if already exists
    if file_path.exists():
        print(f"Error: {file_path} already exists.", file=sys.stderr)
        print(f"Edit it directly or delete it first.", file=sys.stderr)
        return 1

    # Render template
    content = _TEMPLATE.format(
        name=name,
        model=model,
        reasoning=reasoning,
        temperature=temperature,
    )

    # Write file
    file_path.write_text(content, encoding="utf-8")
    print(f"Created: {file_path}")

    # Add to config
    try:
        from hermes_cli.config import load_config, save_config
        config = load_config()
        if "delegate" not in config:
            config["delegate"] = {}
        config["delegate"][name] = str(file_path)
        save_config(config)
        print(f"Added to config: delegate.{name} = {file_path}")
    except Exception as e:
        print(f"Warning: Could not add to config: {e}", file=sys.stderr)
        print(f"Add manually: hermes config set delegate.{name} {file_path}")

    print(f"\nEdit the agent:")
    print(f"  {file_path}")
    print(f"\nTest it:")
    print(f"  hermes agent test {name}")
    print(f"  hermes agent test {name} \"your prompt here\"")
    return 0


def _cmd_agent_list(args):
    as_json = getattr(args, "as_json", False)
    registry = _load_registry()
    agents = registry.list_agents()

    if not agents:
        print("No agents found.")
        print("Create one: hermes agent new <name>")
        return 0

    if as_json:
        print(_json.dumps([a.to_dict() for a in agents], indent=2))
        return 0

    print(f"Available agents ({len(agents)}):\n")
    for a in agents:
        tools_str = ", ".join(a.tools[:3])
        if len(a.tools) > 3:
            tools_str += f" +{len(a.tools) - 3}"
        skills_str = ", ".join(a.skills[:3])
        if len(a.skills) > 3:
            skills_str += f" +{len(a.skills) - 3}"

        print(f"  {a.name}")
        print(f"    Model: {a.model}  |  Reasoning: {a.reasoning}  |  Temp: {a.temperature}")
        if tools_str:
            print(f"    Tools: {tools_str}")
        if skills_str:
            print(f"    Skills: {skills_str}")
        if a.source_path:
            print(f"    Source: {a.source_path}")
        print()
    return 0


def _cmd_agent_show(args):
    name = args.name
    registry = _load_registry()
    agent_def = registry.get_agent(name)

    if not agent_def:
        print(f"Agent '{name}' not found.", file=sys.stderr)
        return 1

    a = agent_def
    print(f"Agent:      {a.name}")
    print(f"Model:      {a.model}")
    print(f"Reasoning:  {a.reasoning}")
    print(f"Temperature:{a.temperature}")
    print(f"Top P:      {a.top_p}")
    print(f"Max Tokens: {a.max_tokens}")
    print(f"Max Depth:  {a.max_depth}")
    print(f"Timeout:    {a.timeout}s")
    print(f"Tools:      {', '.join(a.tools) or '(none)'}")
    print(f"Skills:     {', '.join(a.skills) or '(none)'}")
    print(f"Source:     {a.source_path or '(inline)'}")
    print(f"\nPrompt:\n{a.prompt}")
    return 0


def _cmd_agent_validate(args):
    name = args.name
    registry = _load_registry()
    agent_def = registry.get_agent(name)

    if not agent_def:
        print(f"Agent '{name}' not found.", file=sys.stderr)
        return 1

    from agent.agent_definition import validate_agent_definition
    errors = validate_agent_definition(agent_def)

    if errors:
        print(f"Agent '{name}' has {len(errors)} error(s):")
        for e in errors:
            print(f"  x {e}")
        return 1

    print(f"Agent '{name}' is valid.")
    print(f"  Model: {agent_def.model}  |  Reasoning: {agent_def.reasoning}")
    print(f"  Tools: {len(agent_def.tools)}  |  Skills: {len(agent_def.skills)}")
    return 0


def _cmd_agent_add(args):
    name = args.name
    path = Path(args.path).expanduser().resolve()

    if not path.exists():
        print(f"Error: {path} does not exist.", file=sys.stderr)
        return 1

    # Validate the file first
    from agent.agent_definition import parse_agent_file, validate_agent_definition
    agent_def = parse_agent_file(path)
    if not agent_def:
        print(f"Error: {path} is not a valid agent definition (missing YAML frontmatter).",
              file=sys.stderr)
        return 1

    errors = validate_agent_definition(agent_def)
    if errors:
        print("Error: Agent definition has validation errors:")
        for e in errors:
            print(f"  x {e}")
        return 1

    # Add to config.yaml
    from hermes_cli.config import load_config, save_config
    config = load_config()
    if "delegate" not in config:
        config["delegate"] = {}

    config["delegate"][name] = str(path)
    save_config(config)
    print(f"Added agent '{name}' -> {path}")
    return 0


def _cmd_agent_remove(args):
    name = args.name

    from hermes_cli.config import load_config, save_config
    config = load_config()
    delegate = config.get("delegate") or {}

    if name not in delegate:
        print(f"Agent '{name}' not found in config.yaml delegate section.",
              file=sys.stderr)
        return 1

    del delegate[name]
    config["delegate"] = delegate
    save_config(config)
    print(f"Removed agent '{name}' from config.yaml.")
    return 0


def _cmd_agent_test(args):
    """One-shot test: build AIAgent with agent persona and run one turn."""
    name = args.name
    prompt = args.prompt
    model_override = getattr(args, "model", None)
    no_tools = getattr(args, "no_tools", False)

    registry = _load_registry()
    agent_def = registry.get_agent(name)

    if not agent_def:
        print(f"Agent '{name}' not found.", file=sys.stderr)
        return 1

    # Default prompt
    if not prompt:
        prompt = "Say hello, tell me your name, what model you are, and what tools you have."

    # Show agent info
    print(f"Testing agent: {name}")
    print(f"  Model: {model_override or agent_def.model}")
    print(f"  Reasoning: {agent_def.reasoning}")
    print(f"  Temperature: {agent_def.temperature}")
    print(f"  Tools: {', '.join(agent_def.tools[:5]) or '(none)'}")
    print(f"  Skills: {', '.join(agent_def.skills[:3]) or '(none)'}")
    print(f"  Prompt: {prompt[:80]}...")
    print()

    # Build the ephemeral system prompt from agent definition
    system_prompt = agent_def.prompt

    # Load skills if any
    if agent_def.skills:
        skills_text = _load_skills_text(agent_def.skills)
        if skills_text:
            system_prompt = skills_text + "\n\n" + system_prompt

    # Spawn AIAgent with ephemeral system prompt
    print("Spawning agent...")
    try:
        from run_agent import AIAgent

        # Build toolsets from agent tools
        enabled_toolsets = None
        if no_tools:
            enabled_toolsets = []
        elif agent_def.tools:
            enabled_toolsets = _resolve_toolsets(agent_def.tools)

        # Build request_overrides for temperature/top_p
        request_overrides = {}
        if agent_def.temperature != 0.7:
            request_overrides["temperature"] = agent_def.temperature
        if agent_def.top_p != 0.9:
            request_overrides["top_p"] = agent_def.top_p

        # Parse reasoning
        reasoning_config = None
        if agent_def.reasoning and agent_def.reasoning != "medium":
            try:
                from hermes_constants import parse_reasoning_effort
                reasoning_config = parse_reasoning_effort(agent_def.reasoning)
            except Exception:
                pass

        child = AIAgent(
            model=model_override or agent_def.model,
            max_iterations=50,
            reasoning_config=reasoning_config,
            enabled_toolsets=enabled_toolsets,
            quiet_mode=True,
            ephemeral_system_prompt=system_prompt,
            log_prefix=f"[agent-test-{name}]",
            platform="agent-test",
            skip_context_files=True,
            skip_memory=True,
            request_overrides=request_overrides or None,
        )

        # Run one conversation turn
        result = child.run_conversation(
            user_message=prompt,
            conversation_history=[],
        )

        # Print result
        if isinstance(result, dict):
            output = result.get("final_response") or result.get("content") or str(result)
        else:
            output = str(result)

        print(f"\n--- Response ({name}) ---")
        print(output)

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1

    return 0


def _load_skills_text(skill_names):
    """Load skill text from .hermes/skills/."""
    skills_dir = Path.home() / ".hermes" / "skills"
    if not skills_dir.exists():
        return ""

    parts = []
    for name in skill_names:
        skill_path = skills_dir / name / "SKILL.md"
        if skill_path.exists():
            try:
                content = skill_path.read_text(encoding="utf-8")
                parts.append(f"[Skill: {name}]\n{content}")
            except Exception:
                pass

    return "\n\n".join(parts)


def _resolve_toolsets(tool_names):
    """Resolve tool names to toolset names."""
    try:
        from toolsets import TOOLSETS
        tool_to_toolset = {}
        for ts_name, ts_def in TOOLSETS.items():
            for t in (ts_def.get("tools") or []):
                if t not in tool_to_toolset:
                    tool_to_toolset[t] = ts_name

        toolsets = set()
        for t in tool_names:
            ts = tool_to_toolset.get(t)
            if ts:
                toolsets.add(ts)

        return sorted(toolsets) if toolsets else None
    except Exception:
        return None
