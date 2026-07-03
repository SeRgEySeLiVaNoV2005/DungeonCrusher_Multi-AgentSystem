"""Tests for the CombatAgent state machine and combat logic."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from agents.combat.combat_agent import CombatAgent, CombatState, ScannerResult
from src.communication.message_bus import MessageBus, MessageType
from src.game_state.state import GameState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_screenshot(h: int = 600, w: int = 800) -> np.ndarray:
    """Return a dummy BGR screenshot (dark grey)."""
    return np.full((h, w, 3), 40, dtype=np.uint8)


def _make_red_bar_screenshot() -> np.ndarray:
    """Return a screenshot with red pixels in the upper half (simulates
    enemy health bars)."""
    img = _make_screenshot()
    # Draw a large red rectangle in the upper area — needs >0.5% of
    # upper-half pixels (threshold ≈1440 px) to trigger the scanner.
    img[80:130, 200:600, 2] = 200  # Red channel high.
    img[80:130, 200:600, 1] = 0    # Green channel low.
    img[80:130, 200:600, 0] = 0    # Blue channel low.
    return img


def _make_frame(screenshot: np.ndarray) -> GameState:
    """Return a GameState with the given screenshot and minimal metadata."""
    return GameState(
        screenshot=screenshot,
        metadata={"window_region": {"left": 0, "top": 0, "width": 800, "height": 600}},
    )


def _message(state: GameState) -> MagicMock:
    """Return a mock Message whose payload is the given GameState."""
    msg = MagicMock()
    msg.payload = state
    msg.type = MessageType.FRAME_CAPTURED
    msg.source = "parent"
    return msg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


@pytest.fixture
def matcher() -> MagicMock:
    """Template matcher that returns no matches by default."""
    m = MagicMock()
    m.find_all.return_value = []
    m.confidence = 0.8
    # CombatAgent guard requires at least one known template to engage.
    m.template_names = ("enemy_health_bar",)
    return m


@pytest.fixture
def agent(bus, matcher) -> CombatAgent:
    """A CombatAgent with default settings and colour scanner disabled
    (so templates control detection deterministically)."""
    a = CombatAgent(
        name="test_combat",
        bus=bus,
        matcher=matcher,
        abilities=["1", "2"],
        ability_cooldown=0.05,
        colour_scanner_enabled=False,
        scan_interval=2,
        combat_timeout_frames=3,
    )
    # Bypass the real thread start — we drive frames manually.
    a.on_start()
    return a


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInitialisation:
    def test_initial_state_is_idle(self, agent):
        assert agent.combat_state == CombatState.IDLE

    def test_not_fighting_initially(self, agent):
        assert agent.is_fighting is False

    def test_zero_stats_initially(self, agent):
        assert agent.combats_fought == 0
        assert agent.abilities_used == 0

    def test_abilities_configured(self, agent):
        assert agent._abilities == ["1", "2"]

    def test_scanners_built(self, agent):
        # Template scanner always present.
        assert len(agent._scanners) >= 1


# ---------------------------------------------------------------------------
# State machine — transitions
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_stays_idle_without_combat_signal(self, agent, bus):
        """No templates → no combat signal → stays IDLE."""
        frame = _make_frame(_make_screenshot())
        for _ in range(10):
            agent.on_frame(_message(frame))
        assert agent.combat_state == CombatState.IDLE

    def test_idle_to_scanning_on_combat_signal(self, agent, matcher, bus):
        """A matching combat template triggers IDLE → SCANNING."""
        from src.vision.template_matcher import MatchResult
        match = MatchResult(
            name="enemy_health_bar",
            confidence=0.95,
            x=140, y=55,
            bounds=(100, 50, 80, 10),
        )
        matcher.find_all.return_value = [match]

        frame = _make_frame(_make_screenshot())
        # First call: guard fires → SCANNING. Then on_update runs.
        agent.on_frame(_message(frame))
        assert agent.combat_state == CombatState.SCANNING

    def test_scanning_timeout_back_to_idle(self, agent, matcher, bus):
        """SCANNING without confirmed enemies → back to IDLE after timeout."""
        # Trigger IDLE → SCANNING.
        from src.vision.template_matcher import MatchResult
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.combat_state == CombatState.SCANNING

        # Now stop returning matches (false alarm).
        matcher.find_all.return_value = []
        # Advance enough frames for the scan timeout.
        for _ in range(10):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.combat_state == CombatState.IDLE

    def test_scanning_to_combat_when_enemies_confirmed(self, agent, matcher, bus):
        """Continuous combat signals move SCANNING → COMBAT."""
        from src.vision.template_matcher import MatchResult
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        # First frame: IDLE → SCANNING.
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.combat_state == CombatState.SCANNING
        # Second frame: SCANNING → COMBAT (enemies confirmed).
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.combat_state == CombatState.COMBAT

    def test_combat_to_cleanup_when_enemies_gone(self, agent, matcher, bus):
        """After N frames without enemies, COMBAT → CLEANUP."""
        from src.vision.template_matcher import MatchResult
        # Get into COMBAT.
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # IDLE → SCANNING
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # SCANNING → COMBAT
        assert agent.combat_state == CombatState.COMBAT

        # Remove enemies. After combat_timeout_frames (3) ticks
        # without enemies, COMBAT → CLEANUP fires.
        matcher.find_all.return_value = []
        for _ in range(3):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.combat_state == CombatState.CLEANUP

    def test_cleanup_to_idle(self, agent, matcher, bus):
        """After cleanup tick, CLEANUP → IDLE."""
        from src.vision.template_matcher import MatchResult
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # IDLE → SCANNING
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # SCANNING → COMBAT
        matcher.find_all.return_value = []
        for _ in range(3):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.combat_state == CombatState.CLEANUP
        # One more tick → IDLE (cleanup_done guard fires).
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.combat_state == CombatState.IDLE


# ---------------------------------------------------------------------------
# Ability rotation
# ---------------------------------------------------------------------------


class TestAbilityRotation:
    def test_uses_ability_during_combat(self, agent, matcher, bus):
        """In COMBAT, the agent sends KeyActions for abilities."""
        from src.vision.template_matcher import MatchResult
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        # Enter COMBAT.
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # IDLE → SCANNING
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # SCANNING → COMBAT

        # The first combat tick should fire an ability.
        # Advance a bit past cooldown (0.05s).
        time.sleep(0.06)
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.abilities_used >= 1

    def test_cycles_through_abilities(self, agent, matcher, bus):
        """Abilities cycle 1 → 2 → 1 → 2 ..."""
        from src.vision.template_matcher import MatchResult
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # IDLE → SCANNING
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # SCANNING → COMBAT

        for _ in range(4):
            time.sleep(0.06)
            agent.on_frame(_message(_make_frame(_make_screenshot())))

        # After 4 ability uses: 1, 2, 1, 2 → highest _ability_index = 4.
        assert agent._ability_index == 4

    def test_cooldown_respected(self, agent, matcher, bus):
        """No ability fires before cooldown elapses."""
        from src.vision.template_matcher import MatchResult
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # IDLE → SCANNING
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # SCANNING → COMBAT

        # First tick — ability fires.
        time.sleep(0.06)
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        count_after_first = agent.abilities_used

        # Second tick immediately — cooldown blocks.
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.abilities_used == count_after_first  # No new ability.

    def test_no_abilities_when_idle(self, agent, bus):
        """In IDLE the agent sends no actions."""
        count_before = agent.abilities_used
        for _ in range(5):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.abilities_used == count_before


# ---------------------------------------------------------------------------
# Colour scanner
# ---------------------------------------------------------------------------


class TestColourScanner:
    def test_red_pixels_detected(self, agent):
        """A screenshot with a red bar triggers the colour scanner."""
        agent._colour_scanner_enabled = True
        agent._build_scanners()

        img = _make_red_bar_screenshot()
        result = agent._colour_scanner(img)
        assert result.enemies_detected is True
        assert "red pixels" in result.detail.lower()

    def test_no_red_no_detection(self, agent):
        """A plain screenshot triggers nothing."""
        agent._colour_scanner_enabled = True
        agent._build_scanners()

        img = _make_screenshot()
        result = agent._colour_scanner(img)
        assert result.enemies_detected is False

    def test_colour_scanner_triggers_combat_signal(self, agent, bus):
        """The colour scanner alone can trigger IDLE → SCANNING."""
        agent._colour_scanner_enabled = True
        agent._build_scanners()

        # No templates — only colour scanner.
        frame = _make_frame(_make_red_bar_screenshot())
        agent.on_frame(_message(frame))
        # Guard should have fired.
        assert agent.combat_state == CombatState.SCANNING


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_combat_counter_increments(self, agent, matcher, bus):
        """Each full combat cycle increments the counter."""
        from src.vision.template_matcher import MatchResult
        # Fight 1.
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # → SCANNING
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # → COMBAT
        matcher.find_all.return_value = []
        for _ in range(5):
            agent.on_frame(_message(_make_frame(_make_screenshot())))  # → CLEANUP → IDLE
        assert agent.combats_fought == 1

        # Fight 2.
        matcher.find_all.return_value = [
            MatchResult(name="enemy_health_bar", confidence=0.9, x=10, y=10, bounds=(-15, 8, 50, 5)),
        ]
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # → SCANNING
        agent.on_frame(_message(_make_frame(_make_screenshot())))  # → COMBAT
        matcher.find_all.return_value = []
        for _ in range(5):
            agent.on_frame(_message(_make_frame(_make_screenshot())))  # → CLEANUP → IDLE
        assert agent.combats_fought == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_null_state_does_not_crash(self, agent, bus):
        msg = MagicMock()
        msg.payload = None
        msg.type = MessageType.FRAME_CAPTURED
        agent.on_frame(msg)
        assert agent.combat_state == CombatState.IDLE

    def test_no_screenshot_does_not_crash(self, agent, bus):
        state = GameState(screenshot=None)
        msg = _message(state)
        agent.on_frame(msg)
        assert agent.combat_state == CombatState.IDLE

    def test_scanner_exception_does_not_crash(self, agent, matcher, bus):
        matcher.find_all.side_effect = RuntimeError("boom")
        frame = _make_frame(_make_screenshot())
        agent.on_frame(_message(frame))
        # Gracefully handled — stays IDLE.
        assert agent.combat_state == CombatState.IDLE
