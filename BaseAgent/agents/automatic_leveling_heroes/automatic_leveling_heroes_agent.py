"""AutomaticLevelingHeroesAgent — autonomous hero leveling in the Heroes tab.

Detects red level-up buttons via template matching and clicks them to
level up heroes.  Activated when the user is idle for a configurable
timeout **and** no other agents are performing tasks.

Architecture
------------

The agent runs a finite-state machine with five states:

.. code-block:: text

    IDLE ──(user idle + others idle)──▶ NAVIGATING
    NAVIGATING ──(geroi tab found)─────▶ SCANNING
    NAVIGATING ──(timeout)──────────────▶ IDLE
    SCANNING ──(red button found)──────▶ LEVELING
    SCANNING ──(end.png found)─────────▶ SCANNING  (reverse up)
    SCANNING ──(max scrolls reached)───▶ DONE
    LEVELING ──(click done)────────────▶ SCANNING  (continue scanning)
    DONE ──(cooldown)──────────────────▶ IDLE

Detection
---------

* **User idle** — Win32 ``GetLastInputInfo`` API checks system-wide input
  inactivity.
* **Other agents idle** — subscribes to ``AGENT_STATUS`` messages; maintains
  a set of busy agent domains.
* **Red button** — template matching with ``prokachka.png``.
* **Heroes tab** — template matching with ``geroi.png``.

Usage
-----

.. code-block:: python

    from agents.automatic_leveling_heroes import AutomaticLevelingHeroesAgent

    agent = AutomaticLevelingHeroesAgent(
        name="leveling_01",
        bus=message_bus,
        matcher=template_matcher,   # shared across agents
    )
    agent.start()
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from enum import Enum
from typing import Optional, Set

# XXX: debug — remove after scroll issue is diagnosed
_SCROLL_DEBUG = True

import numpy as np

from base.child_agent import ChildAgent
from src.communication.message_bus import Message, MessageType
from src.core.config import AutomaticLevelingConfig
from src.core.logger import get_logger
from src.core.state_machine import StateMachine
from src.input.emulator import ClickAction, ScrollAction, WaitAction, get_cursor_position
from src.vision.template_matcher import MatchResult, TemplateMatcher

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class LevelingState(Enum):
    """Public leveling state labels (wraps the FSM string states)."""

    IDLE = "idle"
    NAVIGATING = "navigating"
    SCANNING = "scanning"
    LEVELING = "leveling"
    DONE = "done"


# ---------------------------------------------------------------------------
# Win32 idle-detection helper
# ---------------------------------------------------------------------------


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def _get_idle_seconds() -> float:
    """Return the number of seconds since the last user input event.

    Uses the Win32 ``GetLastInputInfo`` API.  Returns 0.0 on failure or
    non-Windows platforms (conservative — "user is active").
    """
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return 0.0

    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)

    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0

    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AutomaticLevelingHeroesAgent(ChildAgent):
    """Autonomously levels up heroes in the Heroes tab.

    The agent waits for the user to be inactive (no keyboard/mouse input)
    and for other agents to be idle before navigating to the Heroes tab,
    scrolling through the hero list, and clicking red level-up buttons.

    .. note::

        The agent **does not** navigate the dungeon map — it only works
        inside the Heroes tab.  Navigation to the tab itself is done via
        template matching for the ``geroi`` button.
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(
        self,
        name: str,
        bus,
        matcher: TemplateMatcher,
        *,
        config: Optional[AutomaticLevelingConfig] = None,
    ) -> None:
        """
        Args:
            name: Unique agent name (e.g. ``'leveling_01'``).
            bus: Shared message bus.
            matcher: Shared template matcher (must have ``prokachka``,
                     ``prokachka_gray``, and ``geroi`` templates loaded).
            config: Agent configuration. Uses defaults if omitted.
        """
        super().__init__(name, bus, domain="leveling")
        self._matcher = matcher
        self._cfg = config or AutomaticLevelingConfig()

        # Runtime state.
        self._current_screenshot: Optional[np.ndarray] = None
        self._scroll_count: int = 0
        self._scan_countdown: int = 0
        self._navigate_countdown: int = 0
        self._busy_agents: Set[str] = set()
        self._current_level_button: Optional[MatchResult] = None
        self._pass: int = 1  # 1 = hire pass, 2 = level pass
        self._scrolled_to_top: bool = False  # Initial scroll-up flag.
        self._scroll_direction: int = -1  # -1 = down, +1 = up

        # Stuck detection.
        self._stuck_counter: int = 0
        self._last_button_signature: Optional[tuple] = None

        # Scroll timing (debug).
        self._last_scroll_time: float = 0.0

        # Cursor takeover detection — requires sustained movement over
        # multiple frames to distinguish user from game cursor teleports.
        self._prev_cursor_pos: Optional[tuple] = None
        self._agent_just_clicked: bool = False
        self._cursor_movement_streak: int = 0
        self._cursor_check_countdown: int = 0

        # Stats.
        self._total_hires: int = 0
        self._total_levels: int = 0

        # State machine.
        self._fsm = StateMachine(LevelingState.IDLE.value)
        self._setup_states()
        self._setup_transitions()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to frames and agent status messages."""
        super().on_start()

        # Track which agents are busy working.
        self.bus.add_subscriber(
            self._on_agent_status, MessageType.AGENT_STATUS
        )
        # Manual trigger from console (``levelup`` command).
        self.bus.add_subscriber(
            self._on_force_trigger, MessageType.SYSTEM_TRIGGER_LEVELING
        )
        # Manual stop from console (``stop`` command).
        self.bus.add_subscriber(
            self._on_stop_command, MessageType.SYSTEM_STOP_LEVELING
        )

        logger.info(
            f"LevelingAgent '{self.name}' ready. "
            f"idle_timeout={self._cfg.idle_timeout_seconds}s, "
            f"scrolls={self._cfg.scroll_clicks}, "
            f"max_scrolls={self._cfg.max_scrolls}, "
            f"templates=({self._cfg.red_template}, "
            f"{self._cfg.gray_template}, {self._cfg.hire_template})"
        )

    def on_stop(self) -> None:
        """Log stats and clean up."""
        self.bus.remove_subscriber(
            self._on_agent_status, MessageType.AGENT_STATUS
        )
        self.bus.remove_subscriber(
            self._on_force_trigger, MessageType.SYSTEM_TRIGGER_LEVELING
        )
        self.bus.remove_subscriber(
            self._on_stop_command, MessageType.SYSTEM_STOP_LEVELING
        )
        logger.info(
            f"LevelingAgent '{self.name}' stats — "
            f"hired: {self._total_hires}, "
            f"leveled: {self._total_levels}"
        )
        super().on_stop()

    # ------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------

    def on_frame(self, message: Message) -> None:
        """Feed the state machine with the latest screenshot."""
        state = message.payload
        if state is None:
            return

        screenshot = getattr(state, "screenshot", None)
        if screenshot is None:
            return

        self._current_screenshot = screenshot

        # --- Cursor takeover detection (sustained movement) ---
        # Only active during SCANNING/LEVELING.  Requires TWO consecutive
        # checks with significant movement — a single large jump is a game
        # cursor teleport, not user input.
        if self._fsm.current in (
            LevelingState.SCANNING.value,
            LevelingState.LEVELING.value,
        ):
            try:
                cur_x, cur_y = get_cursor_position()
                if cur_x == 0 and cur_y == 0:
                    pass  # Hidden cursor — ignore, reset streak.
                    self._cursor_movement_streak = 0
                elif self._agent_just_clicked:
                    self._prev_cursor_pos = (cur_x, cur_y)
                    self._agent_just_clicked = False
                    self._cursor_movement_streak = 0
                    self._cursor_check_countdown = 3  # Start checking in 3 frames.
                elif self._cursor_check_countdown > 0:
                    self._cursor_check_countdown -= 1
                elif self._prev_cursor_pos is not None:
                    px, py = self._prev_cursor_pos
                    dist = ((cur_x - px) ** 2 + (cur_y - py) ** 2) ** 0.5
                    self._prev_cursor_pos = (cur_x, cur_y)
                    if dist > self._cfg.cursor_takeover_distance:
                        self._cursor_movement_streak += 1
                        if self._cursor_movement_streak >= 2:
                            logger.info(
                                f"[Leveling] Cursor takeover detected "
                                f"(sustained movement over {self._cursor_movement_streak} checks) — stopping"
                            )
                            self._fsm.force(LevelingState.IDLE.value)
                            return
                    else:
                        self._cursor_movement_streak = max(0, self._cursor_movement_streak - 1)
            except Exception:
                pass

        self._fsm.update()

    # ------------------------------------------------------------------
    # Agent status subscriber
    # ------------------------------------------------------------------

    def _on_agent_status(self, message: Message) -> None:
        """Track which agent domains are busy working."""
        payload = message.payload
        if not isinstance(payload, dict):
            return

        domain = payload.get("domain", "")
        working = payload.get("working", False)

        if working:
            self._busy_agents.add(domain)
        else:
            self._busy_agents.discard(domain)

    def _on_force_trigger(self, message: Message) -> None:
        """Manual trigger from console — force NAVIGATING."""
        logger.info("[Leveling] Manually triggered via console")
        self._fsm.force(LevelingState.NAVIGATING.value)

    def _on_stop_command(self, message: Message) -> None:
        """Manual stop from console — force IDLE."""
        logger.info("[Leveling] Force-stopped via console")
        self._scroll_count = 0
        self._pass = 1
        self._scroll_direction = -1
        self._scrolled_to_top = False
        self._stuck_counter = 0
        self._last_button_signature = None
        self._prev_cursor_pos = None
        self._agent_just_clicked = False
        self._cursor_movement_streak = 0
        self._cursor_check_countdown = 0
        self._fsm.force(LevelingState.IDLE.value)

    # ------------------------------------------------------------------
    # FSM setup
    # ------------------------------------------------------------------

    def _setup_states(self) -> None:
        """Register the five leveling states and their hooks."""
        self._fsm.add_state(
            LevelingState.IDLE.value,
            on_enter=self._on_idle_enter,
            on_update=self._on_idle_update,
        )
        self._fsm.add_state(
            LevelingState.NAVIGATING.value,
            on_enter=self._on_navigating_enter,
            on_update=self._on_navigating_update,
        )
        self._fsm.add_state(
            LevelingState.SCANNING.value,
            on_enter=self._on_scanning_enter,
            on_update=self._on_scanning_update,
        )
        self._fsm.add_state(
            LevelingState.LEVELING.value,
            on_enter=self._on_leveling_enter,
            on_update=self._on_leveling_update,
        )
        self._fsm.add_state(
            LevelingState.DONE.value,
            on_enter=self._on_done_enter,
            on_update=self._on_done_update,
        )

    def _setup_transitions(self) -> None:
        """Wire the guarded transitions between states.

        Transitions are evaluated in order each tick.  The first guard
        that returns ``True`` fires.
        """
        # IDLE → NAVIGATING: user idle + no other agents working.
        self._fsm.add_transition(
            LevelingState.IDLE.value,
            LevelingState.NAVIGATING.value,
            self._guard_should_start,
            "trigger conditions met",
        )
        # NAVIGATING → SCANNING: geroi tab is visible.
        self._fsm.add_transition(
            LevelingState.NAVIGATING.value,
            LevelingState.SCANNING.value,
            self._guard_heroes_tab_visible,
            "heroes tab found",
        )
        # NAVIGATING → IDLE: tab not found within timeout.
        self._fsm.add_transition(
            LevelingState.NAVIGATING.value,
            LevelingState.IDLE.value,
            self._guard_navigate_timeout,
            "navigate timeout — tab not found",
        )
        # SCANNING → LEVELING: red level-up button on screen.
        self._fsm.add_transition(
            LevelingState.SCANNING.value,
            LevelingState.LEVELING.value,
            self._guard_red_button_found,
            "red level-up button found",
        )
        # SCANNING → SCANNING: end-of-list detected, reverse direction.
        self._fsm.add_transition(
            LevelingState.SCANNING.value,
            LevelingState.SCANNING.value,
            self._guard_end_of_list_found,
            "end of list — reverse to scrolling up",
        )
        # SCANNING → DONE: scrolled through entire list.
        self._fsm.add_transition(
            LevelingState.SCANNING.value,
            LevelingState.DONE.value,
            self._guard_scrolling_done,
            "list fully scanned — nothing to level",
        )
        # LEVELING → SCANNING: post-click wait elapsed.
        self._fsm.add_transition(
            LevelingState.LEVELING.value,
            LevelingState.SCANNING.value,
            self._guard_leveling_done,
            "level-up complete, continuing",
        )
        # DONE → IDLE: cooldown tick.
        self._fsm.add_transition(
            LevelingState.DONE.value,
            LevelingState.IDLE.value,
            self._guard_done_cooldown,
            "done — returning to idle",
        )

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _guard_should_start(self) -> bool:
        """True if the user is idle AND no other agents are working."""
        idle_sec = _get_idle_seconds()
        if idle_sec < self._cfg.idle_timeout_seconds:
            return False
        if self._busy_agents:
            return False
        return True

    def _guard_heroes_tab_visible(self) -> bool:
        """True if the ``geroi`` template matches, OR hero-list buttons are visible.

        Fallback: when the Heroes tab is already selected it turns yellow,
        so the default ``geroi`` template won't match.  Instead we check
        whether any hero-list buttons (level-up, hire, end marker) are
        visible — if they are, the tab is open.
        """
        if self._current_screenshot is None:
            return False
        match = self._matcher.find_one(self._current_screenshot, "geroi")
        if match is not None:
            logger.info(
                f"[Leveling] geroi tab FOUND at ({match.center[0]}, {match.center[1]}) "
                f"confidence={match.confidence:.2f}"
            )
            return True
        # Fallback: tab already selected (yellow) → check for hero-list content.
        if self._any_hero_button_visible():
            logger.info("[Leveling] geroi tab not matched, but hero buttons visible — tab is open")
            return True
        return False

    def _any_hero_button_visible(self) -> bool:
        """Return True if any hero-list button is visible on screen.

        Checks all four hero-list templates: level-up (red/gray),
        hire (purple), and end-of-list marker.
        """
        if self._current_screenshot is None:
            return False
        for tpl in (self._cfg.red_template, self._cfg.gray_template,
                     self._cfg.hire_template, self._cfg.end_template):
            if self._matcher.find_one(self._current_screenshot, tpl) is not None:
                return True
        return False

    def _guard_navigate_timeout(self) -> bool:
        """True if we've been navigating too long without finding the tab."""
        return self._navigate_countdown <= 0

    def _guard_end_of_list_found(self) -> bool:
        """True when the ``end`` template is detected while scrolling down.

        Flips scroll direction to up and resets the scroll counter.
        Only fires when scrolling down — prevents re-triggering on the
        way back up.
        """
        if self._scroll_direction != -1:
            return False
        if self._current_screenshot is None:
            return False
        match = self._matcher.find_one(
            self._current_screenshot, self._cfg.end_template
        )
        if match is not None:
            logger.info(
                f"[Leveling] End-of-list FOUND at "
                f"({match.center[0]}, {match.center[1]}) "
                f"confidence={match.confidence:.2f} — reversing up"
            )
            self._scroll_direction = 1
            self._scroll_count = 0
            return True
        return False

    def _guard_red_button_found(self) -> bool:
        """True if a clickable button is visible on the current pass.

        Pass 1 (hire): only looks for hire/purple buttons.
        Pass 2 (level): only looks for red level-up buttons.

        Caches the match result in ``_current_level_button``.
        """
        if self._current_screenshot is None:
            return False

        if self._pass == 1:
            tpl = self._cfg.hire_template
        else:
            tpl = self._cfg.red_template

        match = self._matcher.find_one(self._current_screenshot, tpl)
        if match is not None:
            logger.info(
                f"[Leveling] Button '{tpl}' FOUND at "
                f"({match.center[0]}, {match.center[1]}) "
                f"confidence={match.confidence:.2f}"
            )
            self._current_level_button = match
            return True
        return False

    def _guard_scrolling_done(self) -> bool:
        """True when both passes have exhausted the hero list.

        Scrolling up max reached → at top of the list.
        Pass 1 (hire) up-complete → switches to pass 2 (level down).
        Pass 2 (level) up-complete → transitions to DONE.
        """
        if self._scroll_count < self._cfg.max_scrolls:
            return False
        # Max scrolls reached in current direction.
        # If scrolling down and end.png wasn't found, flip to up anyway.
        if self._scroll_direction == -1:
            self._scroll_direction = 1
            self._scroll_count = 0
            logger.info("[Leveling] Max down scrolls — reversing to scroll up")
            return False
        # Scrolling up max reached = at top of list.
        if self._pass == 1:
            self._pass = 2
            self._scroll_count = 0
            self._scroll_direction = -1
            logger.info(
                f"[Leveling] Hire pass complete ({self._total_hires} hired). "
                f"Starting level pass..."
            )
            return False
        return True

    def _guard_leveling_done(self) -> bool:
        """True once the post-level-up wait has elapsed."""
        return self._scan_countdown <= 0

    def _guard_done_cooldown(self) -> bool:
        """True after one tick in DONE."""
        self._done_ticks += 1
        return self._done_ticks >= 2

    # ------------------------------------------------------------------
    # IDLE
    # ------------------------------------------------------------------

    def _on_idle_enter(self) -> None:
        self._stuck_counter = 0
        self._last_button_signature = None
        self._prev_cursor_pos = None
        self._agent_just_clicked = False
        self._cursor_movement_streak = 0
        self._cursor_check_countdown = 0
        logger.debug("[Leveling] Entering IDLE")

    def _on_idle_update(self) -> None:
        pass  # Guards are evaluated every tick; nothing to do here.

    # ------------------------------------------------------------------
    # NAVIGATING
    # ------------------------------------------------------------------

    def _on_navigating_enter(self) -> None:
        """Click the Heroes tab via template matching."""
        self._navigate_countdown = self._cfg.navigate_check_frames
        self._scroll_count = 0
        self._pass = 1  # Start with hire pass.
        self._scroll_direction = -1  # Start scrolling down.
        self._scrolled_to_top = False

        if self._current_screenshot is not None:
            match = self._matcher.find_one(self._current_screenshot, "geroi")
            if match is not None:
                logger.info(
                    f"[Leveling] Clicking 'Герои' tab "
                    f"at ({match.center[0]}, {match.center[1]})"
                )
                self.request_action(
                    ClickAction(match.center[0], match.center[1]),
                    WaitAction(0.5),
                )
                self._agent_just_clicked = True
            elif self._any_hero_button_visible():
                logger.info(
                    "[Leveling] 'geroi' not matched but hero buttons visible "
                    "— tab already open, skipping to SCANNING"
                )
                self._fsm.force(LevelingState.SCANNING.value)
            else:
                logger.warning(
                    "[Leveling] 'geroi' template not found on screen "
                    "— waiting for navigation"
                )

    def _on_navigating_update(self) -> None:
        self._navigate_countdown -= 1

    # ------------------------------------------------------------------
    # SCANNING
    # ------------------------------------------------------------------

    def _capture_button_signature(self) -> tuple:
        """Return a hashable signature of visible hero-list button positions.

        Used for stuck detection — if the signature doesn't change after
        scrolling, the list has rubber-banded and we're at a boundary.
        """
        if self._current_screenshot is None:
            return ()
        positions = []
        for tpl in (self._cfg.red_template, self._cfg.gray_template,
                     self._cfg.hire_template, self._cfg.end_template):
            match = self._matcher.find_one(self._current_screenshot, tpl)
            if match is not None:
                # Round to nearest 5 px to tolerate minor jitter.
                x = (match.center[0] // 5) * 5
                y = (match.center[1] // 5) * 5
                positions.append((tpl, x, y))
        return tuple(sorted(positions))

    def _on_scanning_enter(self) -> None:
        self._scan_countdown = self._cfg.scan_interval_frames
        # On first entry, scroll to the very top of the hero list.
        if not self._scrolled_to_top:
            self._scrolled_to_top = True
            logger.info("[Leveling] Scrolling to top of hero list")
            self.request_action(
                ScrollAction(dy=1, amount=self._cfg.scroll_up_after_click),
                WaitAction(self._cfg.scroll_delay),
            )
        if _SCROLL_DEBUG:
            logger.info(
                "[Leveling] → ENTER SCANNING "
                f"(countdown={self._scan_countdown}, pass={self._pass}, "
                f"direction={self._scroll_direction}, scrolls={self._scroll_count})"
            )

    def _on_scanning_update(self) -> None:
        self._scan_countdown -= 1
        if _SCROLL_DEBUG:
            # Log every frame to see the countdown ticking.
            logger.info(
                f"[Leveling] SCANNING tick frame=#{self._frame_count} "
                f"countdown={self._scan_countdown}"
            )
        if self._scan_countdown <= 0:
            self._scroll_count += 1
            direction = "↓" if self._scroll_direction == -1 else "↑"
            now = time.perf_counter()
            elapsed = (
                f"{now - self._last_scroll_time:.3f}s"
                if hasattr(self, "_last_scroll_time") and self._last_scroll_time
                else "first"
            )
            self._last_scroll_time = now
            logger.info(
                f"[Leveling] Scroll #{self._scroll_count} {direction} "
                f"(pass {self._pass}, dir={self._scroll_direction}) "
                f"[elapsed={elapsed}, frame={self._frame_count}]"
            )
            self.request_action(
                ScrollAction(
                    dy=self._scroll_direction,
                    amount=self._cfg.scroll_clicks,
                ),
                WaitAction(self._cfg.scroll_delay),
            )
            self._scan_countdown = self._cfg.scan_interval_frames

    # ------------------------------------------------------------------
    # LEVELING
    # ------------------------------------------------------------------

    def _on_leveling_enter(self) -> None:
        """Click the button that was found (hire or level-up).

        On the hire pass, clicks **twice**: the first click hires the
        hero and turns the button red, the second click levels them up
        immediately.  No scroll-back — just continue scanning down.
        """
        if _SCROLL_DEBUG:
            logger.info(
                "[Leveling] → ENTER LEVELING "
                f"(pass={self._pass}, scrolls={self._scroll_count}, "
                f"frame={self._frame_count})"
            )
        button = getattr(self, "_current_level_button", None)
        if button is not None:
            cx, cy = button.center[0], button.center[1]
            if self._pass == 1:
                self._total_hires += 1
                logger.info(
                    f"[Leveling] Hire #{self._total_hires} "
                    f"at ({cx}, {cy}) — double-click to level"
                )
                # Hire click → wait → level-up click (button turns red).
                self.request_action(
                    ClickAction(cx, cy),
                    WaitAction(self._cfg.level_up_wait),
                    ClickAction(cx, cy),
                    WaitAction(self._cfg.level_up_wait),
                )
                self._agent_just_clicked = True
            else:
                self._total_levels += 1
                logger.info(
                    f"[Leveling] Level-up #{self._total_levels} "
                    f"at ({cx}, {cy})"
                )
                self.request_action(
                    ClickAction(cx, cy),
                    WaitAction(self._cfg.level_up_wait),
                )
                self._agent_just_clicked = True
            self._current_level_button = None

        # Continue scanning down from current position — no scroll-up.
        self._scan_countdown = self._cfg.scan_interval_frames

    def _on_leveling_update(self) -> None:
        self._scan_countdown -= 1

    # ------------------------------------------------------------------
    # DONE
    # ------------------------------------------------------------------

    def _on_done_enter(self) -> None:
        self._done_ticks = 0
        self.publish(
            MessageType.AGENT_STATUS,
            payload={"domain": "leveling", "working": False, "state": "done"},
        )
        logger.info(
            f"[Leveling] Done. Hired {self._total_hires}, "
            f"leveled {self._total_levels} hero(es) this session."
        )

    def _on_done_update(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def leveling_state(self) -> LevelingState:
        """The current FSM state."""
        raw = self._fsm.current
        try:
            return LevelingState(raw)
        except ValueError:
            return LevelingState.IDLE

    @property
    def total_levels(self) -> int:
        """Total number of heroes leveled this session."""
        return self._total_levels

    @property
    def is_leveling(self) -> bool:
        """True while the agent is actively leveling heroes."""
        return self._fsm.current in (
            LevelingState.NAVIGATING.value,
            LevelingState.SCANNING.value,
            LevelingState.LEVELING.value,
        )

    @property
    def is_blocked(self) -> bool:
        """True if the agent is blocked by busy agents."""
        return bool(self._busy_agents)
