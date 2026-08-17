import re
from typing import List
import logging

logger = logging.getLogger(__name__)


_MCP_INJECTION_PATTERNS = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
     "prompt override attempt ('ignore previous instructions')"),
    (re.compile(r"you\s+are\s+now\s+a", re.I),
     "identity override attempt ('you are now a...')"),
    (re.compile(r"your\s+new\s+(task|role|instructions?)\s+(is|are)", re.I),
     "task override attempt"),
    (re.compile(r"system\s*:\s*", re.I),
     "system prompt injection attempt"),
    (re.compile(r"<\s*(system|human|assistant)\s*>", re.I),
     "role tag injection attempt"),
    (re.compile(r"do\s+not\s+(tell|inform|mention|reveal)", re.I),
     "concealment instruction"),
    (re.compile(r"(curl|wget|fetch)\s+https?://", re.I),
     "network command in description"),
    (re.compile(r"base64\.(b64decode|decodebytes)", re.I),
     "base64 decode reference"),
    (re.compile(r"exec\s*\(|eval\s*\(", re.I),
     "code execution reference"),
    (re.compile(r"import\s+(subprocess|os|shutil|socket)", re.I),
     "dangerous import reference"),
]


def _scan_mcp_description(server_name: str, tool_name: str, description: str) -> List[str]:
    """Scan an MCP tool description for prompt injection patterns.

    Returns a list of finding strings (empty = clean).
    """
    findings = []
    if not description:
        return findings
    for pattern, reason in _MCP_INJECTION_PATTERNS:
        if pattern.search(description):
            findings.append(reason)
    if findings:
        logger.warning(
            "MCP server '%s' tool '%s': suspicious description content — %s. "
            "Description: %.200s",
            server_name, tool_name, "; ".join(findings),
            description,
        )
    return findings
