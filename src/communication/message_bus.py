"""Thread-safe pub/sub message bus for inter-agent communication."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any, Callable, DefaultDict, Dict, List, Set

from ..core.exceptions import CommunicationError
from ..core.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

class MessageType(Enum):
    """Standard message categories on the bus."""

    # --- System ---
    SYSTEM_START = auto()
    SYSTEM_STOP = auto()
    SYSTEM_ERROR = auto()

    # --- Frame pipeline ---
    FRAME_CAPTURED = auto()     # New screenshot available
    FRAME_PROCESSED = auto()    # Vision pipeline finished

    # --- Agent ---
    AGENT_STARTED = auto()
    AGENT_STOPPED = auto()
    AGENT_DECISION = auto()     # A child agent made a decision
    AGENT_ACTION_REQUEST = auto()  # Agent wants the parent to execute an action

    # --- Game state ---
    STATE_UPDATED = auto()      # Game state changed
    STATE_REQUEST = auto()      # Request current game state

    # --- UI events ---
    UI_ELEMENT_FOUND = auto()   # A template was matched
    UI_ELEMENT_LOST = auto()    # A template disappeared


@dataclass(frozen=True)
class Message:
    """An immutable message sent over the bus."""

    type: MessageType
    """Category of the message."""

    source: str
    """Name of the sender (e.g. ``'parent_agent'``, ``'combat_agent'``)."""

    payload: Any = None
    """Arbitrary data attached to the message."""

    timestamp: datetime = field(default_factory=datetime.now)
    """When the message was created."""


# Type alias for subscriber callbacks.
Subscriber = Callable[[Message], None]


# ---------------------------------------------------------------------------
# Message Bus
# ---------------------------------------------------------------------------

class MessageBus:
    """A thread-safe publish-subscribe message bus.

    Agents subscribe to specific :class:`MessageType` values (or ``None`` for
    all messages). When a message is published, every matching subscriber is
    notified on the publisher's thread — subscribers **must not block**.

    Example::

        bus = MessageBus()

        @bus.subscribe(MessageType.STATE_UPDATED)
        def on_state(msg: Message):
            print(f"New state: {msg.payload}")

        bus.publish(Message(MessageType.STATE_UPDATED, "test", {"gold": 100}))
    """

    def __init__(self) -> None:
        self._subscribers: DefaultDict[
            Optional[MessageType], List[Subscriber]
        ] = defaultdict(list)
        self._lock = threading.RLock()
        self._message_history: List[Message] = []
        self._history_limit = 1000

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe(
        self,
        message_type: Optional[MessageType] = None,
    ) -> Callable[[Subscriber], Subscriber]:
        """Decorator that registers a callback for a message type.

        Args:
            message_type: Receive only messages of this type.
                          ``None`` = receive everything.

        Returns:
            A decorator that returns the subscriber unchanged.
        """
        def decorator(func: Subscriber) -> Subscriber:
            with self._lock:
                self._subscribers[message_type].append(func)
            logger.debug(
                f"Subscribed '{func.__name__}' to {message_type}"
            )
            return func
        return decorator

    def add_subscriber(
        self, callback: Subscriber, message_type: Optional[MessageType] = None
    ) -> None:
        """Register a callback without using the decorator."""
        with self._lock:
            self._subscribers[message_type].append(callback)
        logger.debug(f"Added subscriber for {message_type}")

    def remove_subscriber(
        self, callback: Subscriber, message_type: Optional[MessageType] = None
    ) -> None:
        """Unregister a previously registered callback."""
        with self._lock:
            subs = self._subscribers[message_type]
            if callback in subs:
                subs.remove(callback)
                logger.debug(f"Removed subscriber for {message_type}")

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(self, message: Message) -> None:
        """Push a message to all matching subscribers.

        Subscribers are called **synchronously** on the publishing thread.
        If a subscriber raises, the error is logged but propagation continues.
        """
        with self._lock:
            self._message_history.append(message)
            # Trim history.
            if len(self._message_history) > self._history_limit:
                self._message_history = self._message_history[-self._history_limit:]

            # Gather subscribers: type-specific + wildcard (None).
            specific = self._subscribers.get(message.type, [])
            wildcard = self._subscribers.get(None, [])

        all_subscribers = list(specific) + list(wildcard)

        for subscriber in all_subscribers:
            try:
                subscriber(message)
            except Exception:
                logger.exception(
                    f"Subscriber '{getattr(subscriber, '__name__', subscriber)}' "
                    f"failed for message {message.type.name}"
                )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def history(self, message_type: Optional[MessageType] = None) -> List[Message]:
        """Return recent messages, optionally filtered by type.

        Args:
            message_type: Filter to this type only. ``None`` = no filter.

        Returns:
            A copy of the matching messages (oldest first).
        """
        with self._lock:
            if message_type is None:
                return list(self._message_history)
            return [m for m in self._message_history if m.type == message_type]

    def clear_history(self) -> None:
        """Drop all stored message history."""
        with self._lock:
            self._message_history.clear()
