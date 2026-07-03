"""Tests for the AutomaticLevelingHeroesAgent state machine and leveling logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from agents.automatic_leveling_heroes.automatic_leveling_heroes_agent import (
    AutomaticLevelingHeroesAgent,
    LevelingState,
)
from src.communication.message_bus import MessageBus, MessageType
from src.game_state.state import GameState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_screenshot(h: int = 600, w: int = 800) -> np.ndarray:
    """Return a dummy BGR screenshot (dark grey)."""
    return np.full((h, w, 3), 40, dtype=np.uint8)


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


def _agent_status_msg(domain: str, working: bool, state: str = "idle") -> MagicMock:
    """Return a mock AGENT_STATUS message."""
    msg = MagicMock()
    msg.payload = {"domain": domain, "working": working, "state": state}
    msg.type = MessageType.AGENT_STATUS
    msg.source = f"{domain}_01"
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
    m.find_one.return_value = None
    m.confidence = 0.6
    m.template_names = ("geroi", "prokachka", "prokachka_gray", "HiringHero")
    return m


@pytest.fixture
def agent(bus, matcher) -> AutomaticLevelingHeroesAgent:
    """A LevelingAgent with short timeouts for fast testing."""
    from src.core.config import AutomaticLevelingConfig

    cfg = AutomaticLevelingConfig(
        idle_timeout_seconds=60,
        scroll_clicks=2,
        scroll_delay=0.01,
        scan_interval_frames=2,
        level_up_wait=0.01,
        navigate_check_frames=5,
        max_scrolls=5,
        red_template="prokachka",
        gray_template="prokachka_gray",
        hire_template="HiringHero",
        end_template="end",
    )
    a = AutomaticLevelingHeroesAgent(
        name="test_leveling",
        bus=bus,
        matcher=matcher,
        config=cfg,
    )
    # Bypass the real thread start — we drive frames manually.
    a.on_start()
    return a


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestInitialisation:
    def test_initial_state_is_idle(self, agent):
        assert agent.leveling_state == LevelingState.IDLE

    def test_not_leveling_initially(self, agent):
        assert agent.is_leveling is False

    def test_zero_levels_initially(self, agent):
        assert agent.total_levels == 0

    def test_not_blocked_initially(self, agent):
        assert agent.is_blocked is False


# ---------------------------------------------------------------------------
# State machine — idle detection
# ---------------------------------------------------------------------------


class TestIdleDetection:
    def test_stays_idle_when_user_active(self, agent, matcher):
        """With active user and geroi tab visible, still stays IDLE."""
        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="geroi", confidence=0.9, x=100, y=50,
            bounds=(90, 40, 20, 20),
        )
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=0.0,
        ):
            for _ in range(5):
                agent.on_frame(_message(_make_frame(_make_screenshot())))

        assert agent.leveling_state == LevelingState.IDLE

    def test_stays_idle_when_agents_busy(self, agent, matcher, bus):
        """Even with idle user, busy agents block the transition."""
        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="geroi", confidence=0.9, x=100, y=50,
            bounds=(90, 40, 20, 20),
        )
        # Mark combat agent as busy.
        agent._on_agent_status(_agent_status_msg("combat", working=True))
        assert agent.is_blocked is True

        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=120.0,
        ):
            for _ in range(5):
                agent.on_frame(_message(_make_frame(_make_screenshot())))

        assert agent.leveling_state == LevelingState.IDLE

    def test_idle_and_no_busy_triggers_navigate(self, agent, matcher):
        """User idle + no busy agents → IDLE → NAVIGATING."""
        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="geroi", confidence=0.9, x=100, y=50,
            bounds=(90, 40, 20, 20),
        )
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=120.0,
        ):
            agent.on_frame(_message(_make_frame(_make_screenshot())))

        # First tick: IDLE guard fires → NAVIGATING.
        # The geroi match triggers NAVIGATING → SCANNING on the same tick.
        assert agent.leveling_state in (LevelingState.NAVIGATING, LevelingState.SCANNING)


# ---------------------------------------------------------------------------
# State machine — navigation
# ---------------------------------------------------------------------------


class TestNavigation:
    def test_navigates_to_heroes_tab(self, agent, matcher):
        """When trigger fires, geroi tab is clicked on the first frame,
        and the geroi match on the second frame completes the transition."""
        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="geroi", confidence=0.9, x=100, y=50,
            bounds=(90, 40, 20, 20),
        )
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=120.0,
        ):
            # Frame 1: IDLE → NAVIGATING (guard fires, on_enter clicks geroi).
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.NAVIGATING

        # Frame 2: NAVIGATING → SCANNING (geroi still visible).
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.SCANNING

    def test_navigate_timeout_returns_to_idle(self, agent, matcher):
        """If geroi tab never appears, timeout returns to IDLE."""
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=120.0,
        ):
            # Frame 1: IDLE → NAVIGATING.
            agent.on_frame(_message(_make_frame(_make_screenshot())))

        assert agent.leveling_state == LevelingState.NAVIGATING

        # Advance frames without geroi appearing.
        # After navigate_check_frames (5) ticks, timeout guard fires → IDLE.
        # Must keep _get_idle_seconds patched so it doesn't re-trigger.
        matcher.find_one.return_value = None
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=0.0,  # Not idle — prevent re-entry to NAVIGATING.
        ):
            for _ in range(agent._cfg.navigate_check_frames + 3):
                agent.on_frame(_message(_make_frame(_make_screenshot())))

        assert agent.leveling_state == LevelingState.IDLE


# ---------------------------------------------------------------------------
# State machine — scanning & leveling
# ---------------------------------------------------------------------------


class TestScanningAndLeveling:
    def _force_to_scanning(self, agent, matcher):
        """Helper: force the agent into SCANNING state (2 frames needed).

        Frame 1: IDLE → NAVIGATING (needs idle user + geroi match).
        Frame 2: NAVIGATING → SCANNING (geroi match).
        """
        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="geroi", confidence=0.9, x=100, y=50,
            bounds=(90, 40, 20, 20),
        )
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=120.0,
        ):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        # Frame 2: NAVIGATING → SCANNING.
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.SCANNING

    def test_red_button_triggers_leveling(self, agent, matcher):
        """A red button match triggers SCANNING → LEVELING."""
        self._force_to_scanning(agent, matcher)

        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="prokachka", confidence=0.85, x=600, y=300,
            bounds=(590, 290, 20, 20),
        )
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.LEVELING

    def test_scrolling_requests_scroll_action(self, agent, matcher, bus):
        """When no red button is found, the agent scrolls down."""
        self._force_to_scanning(agent, matcher)

        # No matches → agent should scroll.
        matcher.find_one.return_value = None

        # Drive frames until a scroll happens (scan_countdown reaches 0).
        # scan_interval_frames = 2, so it scrolls on the 3rd SCANNING tick.
        scroll_requested = False
        for _ in range(5):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
            # Check the bus history for action requests containing ScrollAction.
            history = bus.history(MessageType.AGENT_ACTION_REQUEST)
            for msg in history:
                if msg.payload is not None:
                    # payload is either a single Action or a list.
                    actions = msg.payload if isinstance(msg.payload, list) else [msg.payload]
                    for action in actions:
                        if type(action).__name__ == "ScrollAction":
                            scroll_requested = True
                            break

        assert scroll_requested, "Expected at least one ScrollAction request"

    def test_level_up_clicks_button(self, agent, matcher, bus):
        """The LEVELING state sends a ClickAction at the red button position."""
        self._force_to_scanning(agent, matcher)

        from src.vision.template_matcher import MatchResult

        # After _force_to_scanning, there might be stale ClickActions from
        # the geroi tab click.  Flush bus history.
        bus.clear_history()

        matcher.find_one.return_value = MatchResult(
            name="prokachka", confidence=0.85, x=600, y=300,
            bounds=(590, 290, 20, 20),
        )
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.LEVELING

        # The leveling enter handler should have sent a ClickAction.
        history = bus.history(MessageType.AGENT_ACTION_REQUEST)
        click_found = False
        for msg in history:
            if msg.payload is not None:
                actions = msg.payload if isinstance(msg.payload, list) else [msg.payload]
                for action in actions:
                    if type(action).__name__ == "ClickAction":
                        if action.x == 600 and action.y == 300:
                            click_found = True
                            break

        assert click_found, "Expected a ClickAction at (600, 300) for the level-up button"

    def test_leveling_returns_to_scanning(self, agent, matcher):
        """After level-up wait, LEVELING → SCANNING."""
        self._force_to_scanning(agent, matcher)

        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="prokachka", confidence=0.85, x=600, y=300,
            bounds=(590, 290, 20, 20),
        )
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.LEVELING

        # Advance frames until LEVELING → SCANNING.
        for _ in range(5):
            agent.on_frame(_message(_make_frame(_make_screenshot())))

        assert agent.leveling_state == LevelingState.SCANNING

    def test_scrolling_done_transitions_to_done(self, agent, matcher):
        """After bidirectional scrolling with no buttons → DONE.

        Now requires 4× max_scrolls: down+up for pass 1, down+up for pass 2.
        """
        self._force_to_scanning(agent, matcher)

        # No red buttons / end template — agent will scroll.
        matcher.find_one.return_value = None

        # 4 passes: down+up (pass 1) + down+up (pass 2).
        frames_needed = agent._cfg.max_scrolls * agent._cfg.scan_interval_frames * 4 + 20
        for _ in range(frames_needed):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
            if agent.leveling_state == LevelingState.DONE:
                break

        assert agent.leveling_state == LevelingState.DONE

    def test_done_returns_to_idle(self, agent, matcher):
        """DONE transitions back to IDLE after cooldown.
        Must keep _get_idle_seconds patched to prevent re-entry."""
        self._force_to_scanning(agent, matcher)

        matcher.find_one.return_value = None
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=0.0,  # Not idle — prevent NAVIGATING re-entry.
        ):
            # 4×: down+up (pass 1) + down+up (pass 2).
            frames_needed = agent._cfg.max_scrolls * agent._cfg.scan_interval_frames * 4 + 20
            for _ in range(frames_needed):
                agent.on_frame(_message(_make_frame(_make_screenshot())))
                if agent.leveling_state == LevelingState.DONE:
                    break

            assert agent.leveling_state == LevelingState.DONE

            # Advance ticks to pass DONE cooldown → IDLE.
            for _ in range(5):
                agent.on_frame(_message(_make_frame(_make_screenshot())))

        assert agent.leveling_state == LevelingState.IDLE

    def test_hire_button_triggers_leveling(self, agent, matcher):
        """A purple hire button also triggers SCANNING → LEVELING."""
        self._force_to_scanning(agent, matcher)

        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="HiringHero", confidence=0.85, x=600, y=300,
            bounds=(590, 290, 20, 20),
        )
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.LEVELING

    def test_level_up_increments_counter(self, agent, matcher):
        """Each level-up increments total_levels (pass 2)."""
        self._force_to_scanning(agent, matcher)
        agent._pass = 2  # Level pass — only red buttons.

        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="prokachka", confidence=0.85, x=600, y=300,
            bounds=(590, 290, 20, 20),
        )
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.total_levels >= 1


# ---------------------------------------------------------------------------
# Agent status tracking
# ---------------------------------------------------------------------------


class TestAgentStatus:
    def test_tracks_busy_agent(self, agent):
        """Receiving AGENT_STATUS with working=True marks the domain busy."""
        msg = _agent_status_msg("combat", working=True, state="fighting")
        agent._on_agent_status(msg)
        assert "combat" in agent._busy_agents

    def test_tracks_idle_agent(self, agent):
        """Receiving AGENT_STATUS with working=False removes the domain."""
        agent._busy_agents.add("combat")
        msg = _agent_status_msg("combat", working=False, state="idle")
        agent._on_agent_status(msg)
        assert "combat" not in agent._busy_agents

    def test_multiple_domains(self, agent):
        """Correctly tracks multiple busy domains simultaneously."""
        agent._on_agent_status(_agent_status_msg("combat", working=True))
        agent._on_agent_status(_agent_status_msg("navigation", working=True))
        assert "combat" in agent._busy_agents
        assert "navigation" in agent._busy_agents
        assert agent.is_blocked is True

        agent._on_agent_status(_agent_status_msg("combat", working=False))
        assert "combat" not in agent._busy_agents
        assert "navigation" in agent._busy_agents
        assert agent.is_blocked is True

    def test_ignores_non_dict_payload(self, agent):
        """Non-dict AGENT_STATUS payloads are ignored gracefully."""
        msg = MagicMock()
        msg.payload = "not a dict"
        msg.type = MessageType.AGENT_STATUS
        # Should not crash.
        agent._on_agent_status(msg)
        assert agent._busy_agents == set()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_null_state_does_not_crash(self, agent):
        msg = MagicMock()
        msg.payload = None
        msg.type = MessageType.FRAME_CAPTURED
        agent.on_frame(msg)
        assert agent.leveling_state == LevelingState.IDLE

    def test_no_screenshot_does_not_crash(self, agent):
        state = GameState(screenshot=None)
        msg = _message(state)
        agent.on_frame(msg)
        assert agent.leveling_state == LevelingState.IDLE

    def test_scanner_exception_does_not_crash(self, agent, matcher):
        matcher.find_one.side_effect = RuntimeError("boom")
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=120.0,
        ):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        # Gracefully handled — stays IDLE (or NAVIGATING if guard fired first).
        assert agent.leveling_state in (LevelingState.IDLE, LevelingState.NAVIGATING)

    def test_leveling_state_property_unknown(self, agent):
        """LevelingState() with unknown string returns IDLE."""
        agent._fsm._current = "garbage"
        assert agent.leveling_state == LevelingState.IDLE


# ---------------------------------------------------------------------------
# End-of-list detection
# ---------------------------------------------------------------------------


class TestEndOfListDetection:
    def _force_to_scanning(self, agent, matcher):
        """Helper: force the agent into SCANNING state."""
        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="geroi", confidence=0.9, x=100, y=50,
            bounds=(90, 40, 20, 20),
        )
        with patch(
            "agents.automatic_leveling_heroes.automatic_leveling_heroes_agent._get_idle_seconds",
            return_value=120.0,
        ):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        assert agent.leveling_state == LevelingState.SCANNING

    def test_end_template_reverses_direction(self, agent, matcher):
        """When end.png is found while scrolling down, direction flips to up."""
        self._force_to_scanning(agent, matcher)
        assert agent._scroll_direction == -1  # Down initially.

        from src.vision.template_matcher import MatchResult

        # Use side_effect so only the "end" template matches, not hire/red.
        end_match = MatchResult(
            name="end", confidence=0.85, x=400, y=500,
            bounds=(390, 490, 20, 20),
        )

        def _find_one(screenshot, name):
            if name == "end":
                return end_match
            return None

        matcher.find_one.side_effect = _find_one
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        # end.png guard fires → self-transition SCANNING → SCANNING.
        assert agent._scroll_direction == 1  # Flipped to up.
        assert agent._scroll_count == 0  # Reset.

    def test_end_ignored_when_scrolling_up(self, agent, matcher):
        """end.png is NOT detected when already scrolling up."""
        self._force_to_scanning(agent, matcher)
        agent._scroll_direction = 1  # Already scrolling up.

        from src.vision.template_matcher import MatchResult

        matcher.find_one.return_value = MatchResult(
            name="end", confidence=0.85, x=400, y=500,
            bounds=(390, 490, 20, 20),
        )
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        # Direction stays +1, no re-trigger.
        assert agent._scroll_direction == 1

    def test_scrolling_up_exhausts_pass(self, agent, matcher):
        """After reversing up, max_scrolls ends the pass."""
        self._force_to_scanning(agent, matcher)
        agent._scroll_direction = 1  # Already reversed up.
        agent._scroll_count = agent._cfg.max_scrolls - 1  # One away from exhaustion.

        matcher.find_one.return_value = None
        # Drive frames until _guard_scrolling_done fires.
        for _ in range(10):
            agent.on_frame(_message(_make_frame(_make_screenshot())))
            if agent._pass == 2:  # Pass 1 → Pass 2 after up exhaust.
                break

        assert agent._pass == 2
        assert agent._scroll_direction == -1  # Reset to down for pass 2.
        assert agent._scroll_count == 0

    def test_end_and_button_both_visible(self, agent, matcher):
        """If both end.png and a hire button are visible, button takes priority."""
        self._force_to_scanning(agent, matcher)

        # First call: geroi (already matched in _force_to_scanning).
        # Then end.png should NOT fire because button guard is checked first.
        # We need end.png to be returned after button match.
        from src.vision.template_matcher import MatchResult

        # Return a hire button — this should trigger LEVELING, not the end guard.
        matcher.find_one.return_value = MatchResult(
            name="HiringHero", confidence=0.85, x=600, y=300,
            bounds=(590, 290, 20, 20),
        )
        agent.on_frame(_message(_make_frame(_make_screenshot())))
        # Button found → LEVELING, not end.png self-transition.
        assert agent.leveling_state == LevelingState.LEVELING
        assert agent._scroll_direction == -1  # Still down, end didn't fire.


# ---------------------------------------------------------------------------
# _get_idle_seconds
# ---------------------------------------------------------------------------


class TestGetIdleSeconds:
    def test_returns_float(self):
        from agents.automatic_leveling_heroes.automatic_leveling_heroes_agent import _get_idle_seconds

        result = _get_idle_seconds()
        assert isinstance(result, float)
        assert result >= 0.0
