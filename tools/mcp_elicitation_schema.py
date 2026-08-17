import logging

logger = logging.getLogger(__name__)


def _format_elicitation_schema_summary(schema: dict, server_name: str) -> str:
    """Render a JSON-schema-ish requested_schema to a human-readable field list.

    Elicitation schemas are restricted to a flat object with named top-level
    properties. We surface field names, types, and descriptions so the user
    can tell what the server is asking for before approving.
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return f"Approval requested by MCP server '{server_name}'."

    lines = [f"Fields requested by MCP server '{server_name}':"]
    for field_name, field_spec in props.items():
        field_type = ""
        field_desc = ""
        if isinstance(field_spec, dict):
            field_type = str(field_spec.get("type", "") or "")
            field_desc = str(field_spec.get("description", "") or "")
        suffix = f" ({field_type})" if field_type else ""
        if field_desc:
            lines.append(f"  - {field_name}{suffix}: {field_desc}")
        else:
            lines.append(f"  - {field_name}{suffix}")
    return "\n".join(lines)
