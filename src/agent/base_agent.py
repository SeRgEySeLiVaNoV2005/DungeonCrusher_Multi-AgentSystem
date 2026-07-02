"""Abstract base class for all agents in the system."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from ..communication.message_bus import Message, MessageBus, MessageType
from ..core.exceptions import AgentLifecycleError
from ..core.logger import get_logger

logger = get_logger(__name__)


class AgentStatus(Enum):
    """Agent lifecycle states."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class BaseAgent(ABC):
    """Abstract agent with a well-defined lifecycle.

    Subclasses implement:

    * :meth:`on_start` — called once when the agent starts.
    * :meth:`on_frame` — called for every game frame (if the agent subscribes).
    * :meth:`on_stop` — called once when the agent stops.

    Each agent runs in its own thread. Communication happens exclusively
    through the shared :class:`MessageBus`.
    """

    def __init__(self, name: str, bus: MessageBus) -> None:
        """
        Args:
            name: Unique human-readable identifier for this agent.
            bus: Shared message bus for inter-agent communication.
        """
        self.name = name
        self.bus = bus
        self._status = AgentStatus.CREATED
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def status(self) -> AgentStatus:
        """Current lifecycle status of the agent."""
        return self._status

    @property
    def is_running(self) -> bool:
        """``True`` if the agent is in the RUNNING state."""
        return self._status == AgentStatus.RUNNING

    # ------------------------------------------------------------------
    # Lifecycle (called by launcher / parent)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the agent in a background thread.

        Transitions: CREATED → STARTING → RUNNING.

        Raises:
            AgentLifecycleError: If already running or in an invalid state.
        """
        if self._status in (AgentStatus.RUNNING, AgentStatus.STARTING):
            raise AgentLifecycleError(
                f"Agent '{self.name}' is already {self._status.value}"
            )

        self._status = AgentStatus.STARTING
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"agent-{self.name}",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"Agent '{self.name}' started")

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the agent to stop and wait for its thread.

        Transitions: RUNNING → STOPPING → STOPPED.

        Args:
            timeout: Seconds to wait for the agent thread to join.
        """
        if self._status in (AgentStatus.STOPPED, AgentStatus.STOPPING):
            return

        logger.info(f"Stopping agent '{self.name}'...")
        self._status = AgentStatus.STOPPING
        self._stop_event.set()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    f"Agent '{self.name}' did not stop within {timeout}s"
                )

        self._status = AgentStatus.STOPPED
        logger.info(f"Agent '{self.name}' stopped")

    # ------------------------------------------------------------------
    # Abstract hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def on_start(self) -> None:
        """Called once when the agent transitions to RUNNING.

        Override to subscribe to message types, load models, etc.
        """
        ...

    @abstractmethod
    def on_stop(self) -> None:
        """Called once when the agent is stopping.

        Override to persist state, unsubscribe, release resources, etc.
        """
        ...

    def on_frame(self, message: Message) -> None:
        """Called for each frame captured by the parent agent.

        Override to react to new screenshots / vision results.
        Default implementation is a no-op.
        """
        pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def publish(self, message_type: MessageType, payload: object = None) -> None:
        """Convenience wrapper — send a message with this agent as source."""
        self.bus.publish(
            Message(type=message_type, source=self.name, payload=payload)
        )

    def _run_loop(self) -> None:
        """Internal thread target.

        Calls :meth:`on_start`, then blocks until :meth:`stop` is called,
        then calls :meth:`on_stop`.
        """
        try:
            self._status = AgentStatus.RUNNING
            self.on_start()
            self.publish(MessageType.AGENT_STARTED)

            # Block until stop is requested.
            self._stop_event.wait()

        except Exception:
            self._status = AgentStatus.ERROR
            logger.exception(f"Agent '{self.name}' crashed")
            self.publish(MessageType.SYSTEM_ERROR, f"Agent '{self.name}' crashed")
        finally:
            try:
                self.on_stop()
            except Exception:
                logger.exception(f"Error during on_stop of '{self.name}'")
            self._status = AgentStatus.STOPPED
            self.publish(MessageType.AGENT_STOPPED)
