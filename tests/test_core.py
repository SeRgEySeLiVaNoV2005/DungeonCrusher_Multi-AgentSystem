"""Unit tests for core modules (run without external dependencies)."""

import pytest

from src.core.exceptions import (
    AgentError,
    CaptureError,
    CommunicationError,
    ConfigError,
    DungeonCrusherError,
    InputError,
    VisionError,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class TestExceptions:
    """Verify the exception hierarchy is correctly structured."""

    def test_base_exception(self):
        """DungeonCrusherError should be a standard Exception."""
        with pytest.raises(DungeonCrusherError):
            raise DungeonCrusherError("base error")

    def test_inheritance(self):
        """All subclasses must be catchable as DungeonCrusherError."""
        for exc_cls in [
            CaptureError,
            InputError,
            VisionError,
            AgentError,
            CommunicationError,
            ConfigError,
        ]:
            assert issubclass(exc_cls, DungeonCrusherError)
            try:
                raise exc_cls("test")
            except DungeonCrusherError:
                pass  # Expected.

    def test_str_message(self):
        """Exception message is preserved."""
        exc = CaptureError("window not found")
        assert "window not found" in str(exc)


# ---------------------------------------------------------------------------
# Config (without YAML)
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    """Settings dataclass defaults must match config/settings.yaml."""

    def test_default_construction(self):
        """Settings should construct with all defaults."""
        from src.core.config import Settings

        settings = Settings()
        assert settings.game.window_title == "VK Play"
        assert settings.capture.target_fps == 10
        assert settings.capture.monitor == 0
        assert settings.input.action_delay == 0.05
        assert settings.vision.match_confidence == 0.8
        assert settings.vision.ocr_lang == "eng"
        assert settings.agents.max_children == 8
        assert settings.agents.decision_timeout == 1.0
        assert settings.logging.level == "INFO"

    def test_game_config_keywords(self):
        """Game config should have fallback keywords."""
        from src.core.config import GameConfig

        cfg = GameConfig()
        assert "Dungeon" in cfg.window_keywords
        assert "Crusher" in cfg.window_keywords
        assert "Soul Hunters" in cfg.window_keywords


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class TestLogger:
    """Logger module setup and getter."""

    def test_get_logger_returns_logger(self):
        """get_logger should return a Logger instance."""
        from logging import Logger

        from src.core.logger import get_logger

        log = get_logger("test_module")
        assert isinstance(log, Logger)
        assert log.name == "dungeon_crusher.test_module"

    def test_get_logger_handles_full_prefix(self):
        """If name already has prefix, don't double it."""
        from src.core.logger import get_logger

        log = get_logger("dungeon_crusher.already.prefixed")
        assert log.name == "dungeon_crusher.already.prefixed"


# ---------------------------------------------------------------------------
# Message bus
# ---------------------------------------------------------------------------

class TestMessageBus:
    """Pub/sub message bus unit tests."""

    def test_publish_subscribe(self):
        """Subscriber receives published message."""
        from src.communication.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        received: list[Message] = []

        @bus.subscribe(MessageType.SYSTEM_START)
        def handler(msg: Message) -> None:
            received.append(msg)

        msg = Message(MessageType.SYSTEM_START, "test", {"key": "value"})
        bus.publish(msg)

        assert len(received) == 1
        assert received[0].type == MessageType.SYSTEM_START
        assert received[0].payload == {"key": "value"}

    def test_wildcard_subscriber(self):
        """None-type subscriber receives all messages."""
        from src.communication.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        received: list[Message] = []

        bus.add_subscriber(lambda m: received.append(m), None)

        bus.publish(Message(MessageType.SYSTEM_START, "a"))
        bus.publish(Message(MessageType.SYSTEM_STOP, "b"))

        assert len(received) == 2

    def test_unsubscribe(self):
        """Removing a subscriber stops notifications."""
        from src.communication.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        received: list[Message] = []

        def handler(msg: Message) -> None:
            received.append(msg)

        bus.add_subscriber(handler, MessageType.SYSTEM_START)
        bus.publish(Message(MessageType.SYSTEM_START, "test"))
        assert len(received) == 1

        bus.remove_subscriber(handler, MessageType.SYSTEM_START)
        bus.publish(Message(MessageType.SYSTEM_START, "test2"))
        assert len(received) == 1  # No new messages.

    def test_history_capped(self):
        """Message history should be capped."""
        from src.communication.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        bus._history_limit = 5
        for i in range(10):
            bus.publish(Message(MessageType.SYSTEM_START, str(i)))
        assert len(bus.history()) == 5

    def test_subscriber_exception_does_not_break_bus(self):
        """If one subscriber raises, others still get the message."""
        from src.communication.message_bus import Message, MessageBus, MessageType

        bus = MessageBus()
        second_received = []

        def broken(_msg: Message) -> None:
            raise RuntimeError("boom")

        def healthy(msg: Message) -> None:
            second_received.append(msg)

        bus.add_subscriber(broken, MessageType.SYSTEM_START)
        bus.add_subscriber(healthy, MessageType.SYSTEM_START)

        # Should not raise.
        bus.publish(Message(MessageType.SYSTEM_START, "ok"))

        assert len(second_received) == 1


# ---------------------------------------------------------------------------
# Game state
# ---------------------------------------------------------------------------

class TestGameState:
    """GameState and StateTracker tests."""

    def test_state_immutable_defaults(self):
        """Default GameState should have sensible values."""
        from src.game_state.state import GameState

        state = GameState()
        assert state.gold is None
        assert state.hero_level is None
        assert state.current_stage is None
        assert state.ui_elements == {}
        assert state.ocr_texts == {}

    def test_state_tracker_push_and_current(self):
        """Tracker should record and return states."""
        from src.game_state.state import GameState, StateTracker

        tracker = StateTracker(max_history=10)
        assert tracker.current is None

        s1 = GameState(gold=100)
        s2 = GameState(gold=150)
        tracker.push(s1)
        tracker.push(s2)

        assert tracker.current.gold == 150
        assert tracker.previous.gold == 100

    def test_diff_gold(self):
        """Gold diff between consecutive states."""
        from src.game_state.state import GameState, StateTracker

        tracker = StateTracker()
        tracker.push(GameState(gold=100))
        tracker.push(GameState(gold=155))

        assert tracker.diff_gold() == 55

    def test_stable_for(self):
        """Stable check based on frame count."""
        from src.game_state.state import GameState, StateTracker

        tracker = StateTracker()
        assert not tracker.stable_for(3)
        for _ in range(5):
            tracker.push(GameState())
        assert tracker.stable_for(3)
        assert tracker.stable_for(5)
