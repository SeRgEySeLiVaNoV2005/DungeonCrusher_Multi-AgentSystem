"""Lightweight finite-state machine for agent decision-making.

Each agent (CombatAgent, NavigationAgent, etc.) can embed a
:class:`StateMachine` to manage its internal behaviour.

Usage::

    class CombatAgent(ChildAgent):
        def on_start(self):
            self._fsm = StateMachine("IDLE")
            self._fsm.add_state("IDLE", on_update=self._scan_for_enemies)
            self._fsm.add_state("COMBAT", on_enter=self._engage, on_update=self._fight)
            self._fsm.add_state("CLEANUP", on_update=self._collect_loot)
            self._fsm.add_transition("IDLE", "COMBAT", lambda: self._enemy_detected)
            self._fsm.add_transition("COMBAT", "CLEANUP", lambda: self._combat_over)
            self._fsm.add_transition("CLEANUP", "IDLE", lambda: self._cleanup_done)

        def on_frame(self, message):
            self._fsm.update()
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# on_enter / on_exit receive no arguments.
StateCallback = Callable[[], None]

# A guard condition — if it returns True the transition fires.
Guard = Callable[[], bool]


@dataclass
class _Transition:
    """Internal representation of a transition."""

    target: str
    guard: Guard
    description: str = ""


@dataclass
class _State:
    """Internal representation of a state."""

    name: str
    on_enter: Optional[StateCallback] = None
    on_update: Optional[StateCallback] = None
    on_exit: Optional[StateCallback] = None
    transitions: Dict[str, _Transition] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class StateMachine:
    """A simple, explicit FSM for agent decision logic.

    Features:
    - Arbitrary user-defined states (strings).
    - ``on_enter`` / ``on_update`` / ``on_exit`` hooks per state.
    - Guarded transitions — only fire when a condition is ``True``.
    - Thread-safe for use inside an agent's frame loop.
    - ``ANY`` pseudo-state for transitions that match regardless of
      current state (useful for interrupt / global conditions).

    .. note::

        Transitions are evaluated in the order they were added.
        The **first** transition whose guard returns ``True`` wins.
    """

    # Sentinel that matches any current state.
    ANY: str = "*"

    def __init__(self, initial: str) -> None:
        """
        Args:
            initial: The starting state name.
        """
        self._states: Dict[str, _State] = {}
        self._any_transitions: Dict[str, _Transition] = {}
        self._current = initial
        self._previous: Optional[str] = None
        self._lock = threading.RLock()
        self._frame_count: int = 0
        self._entered_this_frame: bool = False  # True only on the frame a transition fires.

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def add_state(
        self,
        name: str,
        *,
        on_enter: Optional[StateCallback] = None,
        on_update: Optional[StateCallback] = None,
        on_exit: Optional[StateCallback] = None,
    ) -> None:
        """Register a state.

        Args:
            name: Unique state name. ``'*'`` is reserved (``ANY``).
            on_enter: Called once when entering this state.
            on_update: Called every :meth:`update` tick while active.
            on_exit: Called once when leaving this state.

        Raises:
            ValueError: If *name* is ``'*'`` (reserved).
        """
        if name == self.ANY:
            raise ValueError(f"'{self.ANY}' is reserved for the ANY pseudo-state")
        with self._lock:
            self._states[name] = _State(
                name=name,
                on_enter=on_enter,
                on_update=on_update,
                on_exit=on_exit,
            )

    def add_transition(
        self,
        from_state: str,
        to_state: str,
        guard: Guard,
        description: str = "",
    ) -> None:
        """Add a guarded transition.

        Args:
            from_state: Source state name, or ``'*'`` for any-state.
            to_state: Target state name (must be registered).
            guard: Callable that returns ``True`` when the transition
                   should fire. Keep these cheap — they run every frame.
            description: Human-readable label for debugging.
        """
        with self._lock:
            trans = _Transition(
                target=to_state,
                guard=guard,
                description=description,
            )

            if from_state == self.ANY:
                # ANY transitions are stored separately and evaluated
                # after current-state-specific transitions.
                self._any_transitions[to_state] = trans
            else:
                state = self._states.setdefault(from_state, _State(name=from_state))
                state.transitions[to_state] = trans

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def update(self) -> Optional[str]:
        """Process one tick: run ``on_update``, then evaluate transitions.

        Called once per agent frame.  The logic is:

        1. Run ``on_update`` of the current state (process frame data).
        2. Check outgoing transitions (current state first, then ANY).
        3. If a guard fires → exit old state, enter new state.
           The new state's ``on_enter`` runs, but its ``on_update``
           waits until the next tick.

        Returns:
            The name of the active state after the tick, or ``None`` if
            no states are registered.
        """
        with self._lock:
            if not self._states:
                return None

            self._frame_count += 1
            self._entered_this_frame = False

            # 1. Run on_update of the current state (process this frame).
            current_state = self._states.get(self._current)
            if current_state is not None and current_state.on_update is not None:
                try:
                    current_state.on_update()
                except Exception:
                    logger.exception(
                        f"[StateMachine] on_update crashed in state '{self._current}'"
                    )

            # 2. Check transitions (react to updated state).
            self._evaluate_transitions()

            return self._current

    # ------------------------------------------------------------------
    # Force transition
    # ------------------------------------------------------------------

    def force(self, target: str) -> None:
        """Immediately switch to *target*, bypassing guards.

        ``on_exit`` of the current state and ``on_enter`` of the target
        both run synchronously.
        """
        with self._lock:
            self._transition_to(target)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _evaluate_transitions(self) -> None:
        """Walk outgoing transitions; fire the first whose guard returns True.

        Current-state transitions are evaluated first; ANY transitions
        are checked only if no explicit transition fires.
        """
        # 1) Explicit transitions from the current state.
        state = self._states.get(self._current)
        if state is not None:
            for trans in list(state.transitions.values()):
                if self._try_transition(trans):
                    return

        # 2) ANY transitions (global, evaluated second).
        for trans in list(self._any_transitions.values()):
            if self._try_transition(trans):
                return

    def _try_transition(self, trans: _Transition) -> bool:
        """Evaluate a single transition's guard.  Return ``True`` if it fired."""
        try:
            if trans.guard():
                logger.debug(
                    f"[StateMachine] {self._current} → {trans.target}"
                    + (f"  ({trans.description})" if trans.description else "")
                )
                self._transition_to(trans.target)
                return True
        except Exception:
            logger.exception(
                f"[StateMachine] Guard crashed for transition "
                f"'{self._current}' → '{trans.target}'"
            )
        return False

    def _transition_to(self, target: str) -> None:
        """Exit old state, switch, enter new state."""
        old = self._states.get(self._current)

        # Exit old.
        if old is not None and old.on_exit is not None:
            try:
                old.on_exit()
            except Exception:
                logger.exception(
                    f"[StateMachine] on_exit crashed in state '{self._current}'"
                )

        self._previous = self._current
        self._current = target
        self._entered_this_frame = True

        # Enter new.
        new_state = self._states.get(target)
        if new_state is not None and new_state.on_enter is not None:
            try:
                new_state.on_enter()
            except Exception:
                logger.exception(
                    f"[StateMachine] on_enter crashed in state '{target}'"
                )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current(self) -> Optional[str]:
        """The active state name."""
        return self._current

    @property
    def previous(self) -> Optional[str]:
        """The previous state name (``None`` if no transition has occurred)."""
        return self._previous

    @property
    def frame_count(self) -> int:
        """How many :meth:`update` calls have been processed."""
        return self._frame_count

    @property
    def just_entered(self) -> bool:
        """``True`` during the very first tick after a transition fires.

        Useful for one-shot setup logic inside ``on_update``.
        """
        return self._entered_this_frame

    @property
    def states(self) -> tuple:
        """Tuple of all registered state names."""
        return tuple(self._states.keys())
