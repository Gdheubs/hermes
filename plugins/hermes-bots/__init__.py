"""Hermes Bot Mode unified plugin entry point.

Bot Mode contributes its user interface through ``desktop/plugin.js`` and its
private group API through ``dashboard/plugin_api.py``. It intentionally adds no
model-facing tools to the core agent schema.
"""

from __future__ import annotations


def register(_context) -> None:
    """Register no model tools; Desktop and dashboard discovery are declarative."""
