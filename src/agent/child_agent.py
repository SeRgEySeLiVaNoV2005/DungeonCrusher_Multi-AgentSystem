"""Base class for child agents that perform specific game tasks."""

from __future__ import annotations

from typing import Optional

from ..communication.message_bus import Message, MessageBus, MessageType
from ..core.logger import get_logger
from ..input.emulator import Action
from .base_agent import BaseAgent

logger = get_logger(__name__)


class ChildAgent(BaseAgent):
    """A specialized agent responsible for a single game domain.

    Examples: CombatAgent, NavigationAgent, ResourceCollectorAgent.

    Child agents:

    1. Subscribe to :attr:`MessageType.FRAME_CAPTURED` to receive game state.
    2. Analyse the state and make a decision.
    3. Request the parent to execute actions via :attr:`MessageType.AGENT_ACTION_REQUEST`.
    """

    def __init__(self, name: str, bus: MessageBus, domain: str = "general") -> None:
        """
        Args:
            name: Unique name (e.g. ``'combat_01'``).
            bus: Shared message bus.
            domain: The agent's area of responsibility
                    (``'combat'``, ``'navigation'``, ``'resources'``, etc.).
        """
        super().__init__(name, bus)
        self.domain = domain
        self._frame_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to frame events."""
        self.bus.add_subscriber(self._on_frame_wrapper, MessageType.FRAME_CAPTURED)
        logger.info(f"Child agent '{self.name}' ({self.domain}) ready")

    def on_stop(self) -> None:
        """Unsubscribe from frame events."""
        self.bus.remove_subscriber(self._on_frame_wrapper, MessageType.FRAME_CAPTURED)
        logger.info(f"Child agent '{self.name}' ({self.domain}) stopped")

    # ------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------

    def _on_frame_wrapper(self, message: Message) -> None:
        """Thin wrapper that increments the frame counter and delegates."""
        self._frame_count += 1
        self.on_frame(message)

    def on_frame(self, message: Message) -> None:
        """Process a new game frame.

        Override this in concrete agents. The default is a no-op.

        The ``message.payload`` is a :class:`~src.game_state.state.GameState`.
        """
        pass

    # ------------------------------------------------------------------
    # Action helpers
    # ------------------------------------------------------------------

    def request_action(self, *actions: Action) -> None:
        """Send one or more actions to the parent agent for execution.

        Example::

            self.request_action(
                ClickAction(300, 400),
                WaitAction(0.5),
                KeyAction('1'),
            )
        """
        if len(actions) == 1:
            payload = actions[0]
        else:
            payload = list(actions)

        self.publish(MessageType.AGENT_ACTION_REQUEST, payload=payload)
        logger.debug(
            f"'{self.name}' requested {len(actions)} action(s)"
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def frame_count(self) -> int:
        """How many frames the agent has processed."""
        return self._frame_count
