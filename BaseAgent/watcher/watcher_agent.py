"""Watcher agent — passive debug observer for the frame pipeline."""

from base.child_agent import ChildAgent
from src.communication.message_bus import Message, MessageType
from src.core.logger import get_logger

logger = get_logger(__name__)


class WatcherAgent(ChildAgent):
    """A passive agent that logs every frame for debugging.

    Useful for verifying the frame pipeline works end-to-end.
    """

    def __init__(self, name: str, bus: "MessageBus", domain: str = "debug") -> None:
        super().__init__(name, bus, domain)
        self._last_log_time = 0.0

    def on_frame(self, message: Message) -> None:
        """Log a summary every 30 frames to avoid spam."""
        if self._frame_count % 30 == 0:
            state = message.payload
            ui_count = sum(len(v) for v in state.ui_elements.values()) if state else 0
            logger.info(
                f"[{self.name}] Frame #{self._frame_count}: "
                f"{ui_count} UI element(s) detected"
            )
