"""Abstract base class for pluggable notifier providers.

Notifier providers give the agent the ability to deliver outbound notifications 
to external channels (PushOver, Desktop alerts, Webhooks, etc.). 
Only ONE provider runs at a time, selected via the `notifier.provider` config key.

External providers are registered and managed via the plugin system.

Lifecycle:
  initialize()          — connect, create resources
  deliver_kanban_event() — handle the delivery of kanban task events
  shutdown()             — clean exit
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class NotifierProvider(ABC):
    """Abstract base class for outbound notification providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'gateway', 'pushover')."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured, has credentials, and is ready.

        Should not make network calls — just check config and installed deps.
        """

    @abstractmethod
    def initialize(self, **kwargs) -> None:
        """Initialize the provider.

        Called once at startup. May establish connections or start threads.
        kwargs may include:
          - hermes_home (str): Active HERMES_HOME path.
        """

    @abstractmethod
    async def deliver_kanban_event(
        self,
        events: list[Any],
        subscription: Dict[str, Any],
        task: Any,
        board_slug: str,
        **kwargs
    ) -> bool:
        """Deliver a kanban task event to the configured notification channel.

        Returns True if delivery succeeded (or was safely ignored/queued), 
        or False if delivery failed and should be retried.
        """

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""
