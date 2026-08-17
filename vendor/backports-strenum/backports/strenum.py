"""A small compatibility implementation of :class:`enum.StrEnum`.

Python 3.11 and newer provide ``enum.StrEnum`` natively. Older supported
Python versions use this equivalent implementation so packages with an
unconditional ``backports-strenum`` dependency remain installable.
"""

from __future__ import annotations

from enum import Enum

try:
    from enum import StrEnum as StrEnum  # type: ignore[assignment]
except ImportError:

    class StrEnum(str, Enum):
        """Backport of Python 3.11's ``enum.StrEnum``."""

        def __new__(cls, value):
            if not isinstance(value, str):
                raise TypeError("StrEnum values must be strings")
            obj = str.__new__(cls, value)
            obj._value_ = value
            return obj

        def __str__(self) -> str:
            return self.value


__all__ = ["StrEnum"]
