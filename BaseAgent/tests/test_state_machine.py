"""Tests for the lightweight finite-state machine."""

from __future__ import annotations

import pytest

from src.core.state_machine import StateMachine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fsm() -> StateMachine:
    """A simple three-state machine with no guards (manual transitions)."""
    sm = StateMachine("A")
    sm.add_state("A")
    sm.add_state("B")
    sm.add_state("C")
    return sm


@pytest.fixture
def fsm_with_hooks() -> StateMachine:
    """A machine with enter/update/exit hooks that record calls."""
    log: list[str] = []

    sm = StateMachine("idle")
    sm.add_state("idle",
                 on_enter=lambda: log.append("enter:idle"),
                 on_update=lambda: log.append("update:idle"),
                 on_exit=lambda: log.append("exit:idle"))
    sm.add_state("active",
                 on_enter=lambda: log.append("enter:active"),
                 on_update=lambda: log.append("update:active"),
                 on_exit=lambda: log.append("exit:active"))
    sm._hook_log = log  # type: ignore[attr-defined]
    return sm


# ---------------------------------------------------------------------------
# Construction & registration
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_initial_state(self, fsm):
        assert fsm.current == "A"

    def test_previous_is_none_initially(self, fsm):
        assert fsm.previous is None

    def test_frame_count_starts_at_zero(self, fsm):
        assert fsm.frame_count == 0

    def test_states_tuple(self, fsm):
        assert set(fsm.states) == {"A", "B", "C"}

    def test_any_reserved_name_raises(self):
        sm = StateMachine("A")
        with pytest.raises(ValueError, match="reserved"):
            sm.add_state("*")


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_guard_fires(self, fsm):
        fsm.add_transition("A", "B", lambda: True, "go B")
        result = fsm.update()
        assert result == "B"
        assert fsm.current == "B"
        assert fsm.previous == "A"

    def test_guard_does_not_fire(self, fsm):
        fsm.add_transition("A", "B", lambda: False)
        fsm.update()
        assert fsm.current == "A"

    def test_first_guard_wins(self, fsm):
        fsm.add_transition("A", "B", lambda: True)
        fsm.add_transition("A", "C", lambda: True)
        fsm.update()
        assert fsm.current == "B"  # B was added first.

    def test_any_transition(self, fsm):
        fsm.add_transition("*", "C", lambda: True)
        fsm.update()
        assert fsm.current == "C"

    def test_current_state_priority_over_any(self, fsm):
        """Current-state transitions are checked before ANY transitions."""
        fsm.add_transition("*", "C", lambda: True)      # ANY → C (always true)
        fsm.add_transition("A", "B", lambda: True)       # A → B (same tick)
        fsm.update()
        assert fsm.current == "B"  # A's transition wins over ANY.


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


class TestHooks:
    def test_on_enter_fires_on_transition(self, fsm_with_hooks):
        sm = fsm_with_hooks
        sm.add_transition("idle", "active", lambda: True)
        sm.update()
        log = sm._hook_log  # type: ignore[attr-defined]
        assert "exit:idle" in log
        assert "enter:active" in log
        # Order: exit old → enter new → update new.
        exit_idx = log.index("exit:idle")
        enter_idx = log.index("enter:active")
        assert exit_idx < enter_idx

    def test_on_update_runs_every_tick(self, fsm_with_hooks):
        sm = fsm_with_hooks
        sm.update()  # No transition — stay in idle.
        log = sm._hook_log  # type: ignore[attr-defined]
        assert log.count("update:idle") == 1
        sm.update()
        assert log.count("update:idle") == 2

    def test_just_entered_flag(self, fsm):
        sm = fsm
        sm.add_transition("A", "B", lambda: True)
        sm.update()
        assert sm.just_entered is True
        sm.update()
        assert sm.just_entered is False


# ---------------------------------------------------------------------------
# Force transition
# ---------------------------------------------------------------------------


class TestForce:
    def test_force_skips_guard(self, fsm):
        fsm.add_transition("A", "B", lambda: False)  # Guard blocks.
        fsm.force("B")
        assert fsm.current == "B"

    def test_force_runs_hooks(self, fsm_with_hooks):
        sm = fsm_with_hooks
        sm.force("active")
        log = sm._hook_log  # type: ignore[attr-defined]
        assert "exit:idle" in log
        assert "enter:active" in log


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_update_with_no_states_returns_none(self):
        sm = StateMachine("A")
        assert sm.update() is None

    def test_guard_exception_does_not_crash_machine(self, fsm):
        def broken_guard():
            raise RuntimeError("boom")
        fsm.add_transition("A", "B", broken_guard)
        fsm.add_transition("A", "C", lambda: True)
        fsm.update()  # Broken guard is skipped, then C fires.
        assert fsm.current == "C"

    def test_on_enter_exception_does_not_crash_machine(self):
        sm = StateMachine("idle")
        sm.add_state("idle")
        sm.add_state("broken", on_enter=lambda: 1 / 0)
        sm.add_state("safe")
        sm.add_transition("idle", "broken", lambda: True)
        sm.add_transition("broken", "safe", lambda: True)
        # idle → broken (on_enter crashes, but state still switches).
        sm.update()
        assert sm.current == "broken"
        # broken → safe.
        sm.update()
        assert sm.current == "safe"

    def test_force_to_unregistered_state(self):
        """Force to a state not yet added — state switches, no hooks run."""
        sm = StateMachine("A")
        sm.add_state("A")
        sm.force("B")  # B not registered.
        assert sm.current == "B"
        # Should not crash on update (no state object for 'B', so no hooks).
        result = sm.update()
        assert result == "B"
