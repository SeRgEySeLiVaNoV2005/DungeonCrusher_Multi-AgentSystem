"""CombatAgent — the first autonomous game-playing agent.

Detects combat on screen and fights enemies by clicking abilities
and targeting priority threats.

Architecture
------------

The agent runs a finite-state machine with four states:

.. code-block:: text

    IDLE ──(combat signal detected)──▶ SCANNING
    SCANNING ──(enemies confirmed)───▶ COMBAT
    SCANNING ──(timeout)──────────────▶ IDLE
    COMBAT ──(no enemies for N frames)▶ CLEANUP
    CLEANUP ──(done)──────────────────▶ IDLE

Detection
---------

Combat detection is **modular** — multiple scanners run each frame and
vote.  This makes the agent resilient to missing templates or unusual
screen layouts.

Currently implemented scanners:

* **Template scanner** — uses :class:`TemplateMatcher` to find known
  UI elements (enemy health bars, ability buttons, combat banners).
* **Colour scanner** — detects red pixels (enemy health bars) in
  candidate regions.
* **OCR scanner** — (planned) reads combat-related text.

The agent only needs **one** scanner to return a positive hit to
trigger the SCANNING → COMBAT transition.

Abilities
---------

Ability rotation is defined via a simple list of hotkeys::

    abilities = ["1", "2", "3", "4"]

The agent cycles through them with a configurable cooldown between
activations.  The cooldown prevents the agent from spamming abilities
faster than the game allows.

Usage
-----

.. code-block:: python

    from agents.combat import CombatAgent

    agent = CombatAgent(
        name="combat_01",
        bus=message_bus,
        matcher=template_matcher,   # shared across agents
        abilities=["1", "2", "3"],
        ability_cooldown=1.5,
    )
    agent.start()
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Dict, List, Optional

import numpy as np

from base.child_agent import ChildAgent
from src.communication.message_bus import Message, MessageType
from src.core.logger import get_logger
from src.core.state_machine import StateMachine
from src.input.emulator import KeyAction, WaitAction
from src.vision.template_matcher import MatchResult, TemplateMatcher

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class CombatState(Enum):
    """Public combat state labels (wraps the FSM string states)."""

    IDLE = "idle"
    SCANNING = "scanning"
    COMBAT = "combat"
    CLEANUP = "cleanup"


# ---------------------------------------------------------------------------
# Scanner result
# ---------------------------------------------------------------------------


@dataclass
class ScannerResult:
    """What a single scanner found on the current frame."""

    enemies_detected: bool = False
    """At least one enemy is visible."""

    combat_ui_visible: bool = False
    """Combat UI elements (ability bar, battle banner) are on screen."""

    enemy_positions: List[tuple] = field(default_factory=list)
    """(x, y) positions of detected enemies in window coordinates."""

    detail: str = ""
    """Human-readable description for logging."""


# A scanner is a callable that takes a BGR screenshot and returns a result.
Scanner = Callable[[np.ndarray], ScannerResult]


# ---------------------------------------------------------------------------
# CombatAgent
# ---------------------------------------------------------------------------


class CombatAgent(ChildAgent):
    """Autonomously detects and fights enemies.

    The agent subscribes to game frames and uses the shared
    :class:`TemplateMatcher` (plus heuristic scanners) to recognise
    combat situations.  When combat is confirmed it executes a simple
    ability rotation via :meth:`request_action`.

    .. note::

        The agent **does not** move the hero — navigation is handled
        by a separate :class:`NavigationAgent` (planned).  CombatAgent
        only fights once combat has started.
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
        abilities: Optional[List[str]] = None,
        ability_cooldown: float = 1.5,
        combat_templates: Optional[List[str]] = None,
        scan_interval: int = 5,
        combat_timeout_frames: int = 30,
        colour_scanner_enabled: bool = True,
    ) -> None:
        """
        Args:
            name: Unique agent name.
            bus: Shared message bus.
            matcher: Shared template matcher (may have zero templates).
            abilities: Hotkey list for ability rotation, e.g.
                       ``['1', '2', '3']``.  Default: ``['1', '2', '3']``.
            ability_cooldown: Minimum seconds between ability activations.
            combat_templates: Template names that indicate combat
                              (e.g. ``'enemy_health_bar'``, ``'battle_banner'``).
            scan_interval: Frames between SCANNING checks.
            combat_timeout_frames: Consecutive frames without enemies
                                   before declaring combat over.
            colour_scanner_enabled: Use red-pixel heuristic for enemy
                                    health-bar detection.
        """
        super().__init__(name, bus, domain="combat")
        self._matcher = matcher
        self._abilities = abilities or ["1", "2", "3"]
        self._ability_cooldown = ability_cooldown
        self._combat_templates = combat_templates or [
            "enemy_health_bar",
            "battle_banner",
            "combat_ability_frame",
        ]
        self._scan_interval = scan_interval
        self._combat_timeout_frames = combat_timeout_frames
        self._colour_scanner_enabled = colour_scanner_enabled

        # Runtime state.
        self._current_screenshot: Optional[np.ndarray] = None
        self._scanners: List[Scanner] = []
        self._ability_index: int = 0
        self._last_ability_time: float = 0.0
        self._frames_without_enemies: int = 0
        self._scan_countdown: int = 0

        # Stats.
        self._combats_fought: int = 0
        self._abilities_used: int = 0

        # State machine.
        self._fsm = StateMachine(CombatState.IDLE.value)
        self._setup_states()
        self._setup_transitions()

    # ------------------------------------------------------------------
    # State machine — setup
    # ------------------------------------------------------------------

    def _setup_states(self) -> None:
        """Register the four combat states and their hooks."""
        self._fsm.add_state(
            CombatState.IDLE.value,
            on_enter=self._on_idle_enter,
            on_update=self._on_idle_update,
        )
        self._fsm.add_state(
            CombatState.SCANNING.value,
            on_enter=self._on_scanning_enter,
            on_update=self._on_scanning_update,
        )
        self._fsm.add_state(
            CombatState.COMBAT.value,
            on_enter=self._on_combat_enter,
            on_update=self._on_combat_update,
            on_exit=self._on_combat_exit,
        )
        self._fsm.add_state(
            CombatState.CLEANUP.value,
            on_enter=self._on_cleanup_enter,
            on_update=self._on_cleanup_update,
        )

    def _setup_transitions(self) -> None:
        """Wire the guarded transitions between states."""
        # IDLE → SCANNING: combat signal from any scanner.
        self._fsm.add_transition(
            CombatState.IDLE.value,
            CombatState.SCANNING.value,
            self._guard_combat_signal,
            "combat signal detected",
        )
        # SCANNING → COMBAT: enemies confirmed.
        self._fsm.add_transition(
            CombatState.SCANNING.value,
            CombatState.COMBAT.value,
            self._guard_enemies_confirmed,
            "enemies confirmed",
        )
        # SCANNING → IDLE: false alarm timeout.
        self._fsm.add_transition(
            CombatState.SCANNING.value,
            CombatState.IDLE.value,
            self._guard_scan_timeout,
            "scan timeout — false alarm",
        )
        # COMBAT → CLEANUP: no enemies for N frames.
        self._fsm.add_transition(
            CombatState.COMBAT.value,
            CombatState.CLEANUP.value,
            self._guard_combat_over,
            "combat ended",
        )
        # CLEANUP → IDLE: cooldown elapsed.
        self._fsm.add_transition(
            CombatState.CLEANUP.value,
            CombatState.IDLE.value,
            self._guard_cleanup_done,
            "cleanup complete",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Build scanners and subscribe to frames."""
        super().on_start()
        self._build_scanners()
        logger.info(
            f"CombatAgent '{self.name}' ready. "
            f"Scanners: {len(self._scanners)}, "
            f"abilities: {self._abilities}, "
            f"cooldown: {self._ability_cooldown}s"
        )

    def on_stop(self) -> None:
        """Log stats and clean up."""
        logger.info(
            f"CombatAgent '{self.name}' stats — "
            f"combats: {self._combats_fought}, "
            f"abilities used: {self._abilities_used}"
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
        self._fsm.update()

    # ------------------------------------------------------------------
    # Scanner construction
    # ------------------------------------------------------------------

    def _build_scanners(self) -> None:
        """Assemble the list of active scanners.

        Scanners are tried in order during SCANNING until one returns
        a positive result.
        """
        # 1. Template-based scanner (always enabled).
        self._scanners.append(self._template_scanner)

        # 2. Colour heuristic for red health bars.
        if self._colour_scanner_enabled:
            self._scanners.append(self._colour_scanner)

    # ------------------------------------------------------------------
    # Scanner: template matching
    # ------------------------------------------------------------------

    def _template_scanner(self, screenshot: np.ndarray) -> ScannerResult:
        """Look for known combat-related UI templates."""
        result = ScannerResult(detail="templates: none found")

        try:
            matches: List[MatchResult] = self._matcher.find_all(screenshot)
        except Exception:
            logger.debug("[CombatAgent] Template matching failed", exc_info=True)
            return result

        if not matches:
            return result

        # Check whether any match belongs to a combat template.
        combat_matches = [
            m for m in matches
            if m.name in self._combat_templates
        ]
        if combat_matches:
            positions = [
                (m.center[0], m.center[1]) for m in combat_matches
            ]
            result.enemies_detected = True
            result.combat_ui_visible = True
            result.enemy_positions = positions
            result.detail = (
                f"templates: {len(combat_matches)} matches "
                f"({', '.join(m.name for m in combat_matches[:3])})"
            )

        return result

    # ------------------------------------------------------------------
    # Scanner: colour heuristic
    # ------------------------------------------------------------------

    def _colour_scanner(self, screenshot: np.ndarray) -> ScannerResult:
        """Heuristic: red pixels in the upper half suggest enemy health bars.

        Dungeon Crusher draws red enemy health bars above enemies.
        A significant cluster of red pixels in the combat area is a
        strong indicator that enemies are present.
        """
        result = ScannerResult(detail="colour: no significant red")

        try:
            import cv2
        except ImportError:
            return result

        h, w = screenshot.shape[:2]

        # Only scan the upper 60% of the screen (enemy area).
        top_h = int(h * 0.6)
        upper = screenshot[:top_h, :, :]

        # Red mask in HSV — two ranges to handle wrap-around.
        hsv = cv2.cvtColor(upper, cv2.COLOR_BGR2HSV)

        # Lower red (0–10°).
        lower_red1 = np.array([0, 80, 80], dtype=np.uint8)
        upper_red1 = np.array([10, 255, 255], dtype=np.uint8)
        mask1 = cv2.inRange(hsv, lower_red1, upper_red1)

        # Upper red (170–180°).
        lower_red2 = np.array([170, 80, 80], dtype=np.uint8)
        upper_red2 = np.array([180, 255, 255], dtype=np.uint8)
        mask2 = cv2.inRange(hsv, lower_red2, upper_red2)

        red_pixels = int(cv2.countNonZero(mask1) + cv2.countNonZero(mask2))

        # Threshold: >0.5% of pixels in the upper region are red.
        threshold = int(top_h * w * 0.005)
        if red_pixels > threshold:
            result.enemies_detected = True
            result.detail = f"colour: {red_pixels} red pixels (threshold {threshold})"

        return result

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _guard_combat_signal(self) -> bool:
        """True if *any* scanner reports a potential combat situation."""
        if self._current_screenshot is None:
            return False

        # Safety: don't engage unless we have at least one combat template.
        # The colour scanner alone can produce false positives.
        has_combat_templates = any(
            name in self._combat_templates
            for name in self._matcher.template_names
        )
        if not has_combat_templates:
            return False

        for scanner in self._scanners:
            try:
                result = scanner(self._current_screenshot)
                if result.enemies_detected or result.combat_ui_visible:
                    logger.debug(
                        f"[CombatAgent] Combat signal: {result.detail}"
                    )
                    return True
            except Exception:
                logger.debug("[CombatAgent] Scanner crashed", exc_info=True)
        return False

    def _guard_enemies_confirmed(self) -> bool:
        """Combat is confirmed — we've seen enemies consistently."""
        # For now: the same signal that got us into SCANNING confirms combat.
        # Future: require N consecutive positive scans.
        return self._guard_combat_signal()

    def _guard_scan_timeout(self) -> bool:
        """True if we've been scanning too long without finding enemies."""
        return self._scan_countdown <= 0

    def _guard_combat_over(self) -> bool:
        """True when no enemies have been detected for N frames."""
        return self._frames_without_enemies >= self._combat_timeout_frames

    def _guard_cleanup_done(self) -> bool:
        """True once the cleanup cooldown has elapsed."""
        return self._fsm.frame_count > 0  # One tick is enough for v1.

    # ------------------------------------------------------------------
    # IDLE
    # ------------------------------------------------------------------

    def _on_idle_enter(self) -> None:
        self._scan_countdown = self._scan_interval
        self.publish(
            MessageType.AGENT_STATUS,
            payload={"domain": "combat", "working": False, "state": "idle"},
        )
        logger.debug("[CombatAgent] Entering IDLE")

    def _on_idle_update(self) -> None:
        # Periodic scan for combat signals.
        self._scan_countdown -= 1

    # ------------------------------------------------------------------
    # SCANNING
    # ------------------------------------------------------------------

    def _on_scanning_enter(self) -> None:
        self._scan_countdown = self._scan_interval * 2
        logger.debug("[CombatAgent] Entering SCANNING")

    def _on_scanning_update(self) -> None:
        self._scan_countdown -= 1

    # ------------------------------------------------------------------
    # COMBAT
    # ------------------------------------------------------------------

    def _on_combat_enter(self) -> None:
        """Combat started — reset counters and log."""
        self._frames_without_enemies = 0
        self._ability_index = 0
        # Set cooldown now so the very first tick doesn't fire instantly.
        self._last_ability_time = time.time()
        self.publish(
            MessageType.AGENT_STATUS,
            payload={"domain": "combat", "working": True, "state": "fighting"},
        )
        logger.info("[CombatAgent] >> COMBAT ENGAGED")

    def _on_combat_update(self) -> None:
        """Fight! Run scanners, use abilities if enemies are present."""
        if self._current_screenshot is None:
            return

        # 1. Scan for enemies.
        enemies_present = False
        for scanner in self._scanners:
            try:
                result = scanner(self._current_screenshot)
                if result.enemies_detected:
                    enemies_present = True
                    break
            except Exception:
                pass

        if enemies_present:
            self._frames_without_enemies = 0
            self._fight_rotation()
        else:
            self._frames_without_enemies += 1

    def _on_combat_exit(self) -> None:
        self._combats_fought += 1

    def _fight_rotation(self) -> None:
        """Execute one step of the ability rotation.

        The rotation cycles through abilities with a cooldown between
        each activation.  This gives the game time to process each
        action before the next one fires.
        """
        now = time.time()
        if now - self._last_ability_time < self._ability_cooldown:
            return  # On cooldown.

        if not self._abilities:
            return

        key = self._abilities[self._ability_index % len(self._abilities)]
        self._ability_index += 1
        self._last_ability_time = now
        self._abilities_used += 1

        self.request_action(
            KeyAction(key),
            WaitAction(0.1),
        )
        logger.debug(
            f"[CombatAgent] Ability '{key}' "
            f"({self._ability_index % len(self._abilities)}/"
            f"{len(self._abilities)})"
        )

    # ------------------------------------------------------------------
    # CLEANUP
    # ------------------------------------------------------------------

    def _on_cleanup_enter(self) -> None:
        logger.info("[CombatAgent] ✓ Combat resolved — cleanup")

    def _on_cleanup_update(self) -> None:
        """Wait one tick, then the transition fires."""
        pass

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def combat_state(self) -> CombatState:
        """The current combat state."""
        raw = self._fsm.current
        try:
            return CombatState(raw)
        except ValueError:
            return CombatState.IDLE

    @property
    def combats_fought(self) -> int:
        """Total number of combat engagements completed."""
        return self._combats_fought

    @property
    def abilities_used(self) -> int:
        """Total number of ability activations."""
        return self._abilities_used

    @property
    def is_fighting(self) -> bool:
        """True while the agent is actively engaged in combat."""
        return self._fsm.current == CombatState.COMBAT.value
