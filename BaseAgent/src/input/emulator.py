"""Input emulation — mouse, keyboard, and action sequences via pynput."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from ..core.exceptions import InputError
from ..core.logger import get_logger

logger = get_logger(__name__)


def get_cursor_position() -> Tuple[int, int]:
    """Return the current mouse cursor position in screen coordinates.

    Returns:
        (x, y) tuple of absolute screen coordinates.

    Raises:
        InputError: If pynput is unavailable.
    """
    try:
        from pynput.mouse import Controller as MouseController
    except ImportError as exc:
        raise InputError(
            "pynput is required for cursor position. Install: pip install pynput"
        ) from exc
    mc = MouseController()
    return (int(mc.position[0]), int(mc.position[1]))


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class MouseButton(Enum):
    """Supported mouse buttons."""

    LEFT = auto()
    RIGHT = auto()
    MIDDLE = auto()


class KeyModifier(Enum):
    """Keyboard modifier keys."""

    CTRL = auto()
    ALT = auto()
    SHIFT = auto()


@dataclass(frozen=True)
class ClickAction:
    """A single mouse click at a point."""

    x: int
    y: int
    button: MouseButton = MouseButton.LEFT
    clicks: int = 1           # 2 = double-click


@dataclass(frozen=True)
class KeyAction:
    """A single keyboard action."""

    key: str
    modifiers: Tuple[KeyModifier, ...] = ()
    duration: float = 0.05    # How long to hold the key


@dataclass(frozen=True)
class WaitAction:
    """Pause execution for a given duration."""

    seconds: float


# Union type for action sequences.
Action = Union[ClickAction, KeyAction, WaitAction]


# ---------------------------------------------------------------------------
# Emulator
# ---------------------------------------------------------------------------

class InputEmulator:
    """Emulates user input — mouse clicks, keyboard presses, and sequences.

    Uses ``pynput`` under the hood for OS-level event injection, which works
    well with DirectX-based games like *Dungeon Crusher: Soul Hunters*.
    """

    def __init__(self, action_delay: float = 0.05) -> None:
        """
        Args:
            action_delay: Minimum delay (seconds) between actions to avoid
                           overwhelming the game.
        """
        self._action_delay = action_delay
        self._mouse: Optional[Any] = None
        self._keyboard: Optional[Any] = None

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def click(
        self,
        x: int,
        y: int,
        button: MouseButton = MouseButton.LEFT,
        clicks: int = 1,
    ) -> None:
        """Click at screen coordinates.

        Args:
            x, y: Screen coordinates.
            button: Mouse button to click.
            clicks: 1 = single, 2 = double.

        Raises:
            InputError: If pynput is unavailable or the click fails.
        """
        mc = self._get_mouse()
        try:
            mc.position = (x, y)
            time.sleep(self._action_delay)

            btn = self._map_button(button)
            for _ in range(clicks):
                mc.click(btn, 1)
                time.sleep(self._action_delay)
        except Exception as exc:
            raise InputError(f"Mouse click failed at ({x}, {y}): {exc}") from exc

    def move(self, x: int, y: int) -> None:
        """Move the mouse cursor to (x, y) without clicking."""
        mc = self._get_mouse()
        mc.position = (x, y)

    def drag(
        self,
        start: Tuple[int, int],
        end: Tuple[int, int],
        button: MouseButton = MouseButton.LEFT,
        steps: int = 20,
    ) -> None:
        """Click-and-drag from start to end in small increments.

        Args:
            start: (x, y) start coordinates.
            end: (x, y) end coordinates.
            button: Mouse button to hold.
            steps: Number of intermediate positions (higher = smoother).
        """
        mc = self._get_mouse()
        btn = self._map_button(button)
        x0, y0 = start
        x1, y1 = end

        try:
            mc.position = (x0, y0)
            time.sleep(self._action_delay)
            mc.press(btn)

            for i in range(1, steps + 1):
                t = i / steps
                mc.position = (
                    int(x0 + (x1 - x0) * t),
                    int(y0 + (y1 - y0) * t),
                )
                time.sleep(self._action_delay / 2)

            mc.release(btn)
        except Exception as exc:
            raise InputError(f"Drag failed from {start} to {end}: {exc}") from exc

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def press(self, key: str, modifiers: Sequence[KeyModifier] = ()) -> None:
        """Press and release a key, optionally with modifiers.

        Args:
            key: Key name as pynput would understand it
                 (e.g. ``'a'``, ``Key.enter``, ``'1'``).
            modifiers: Modifier keys to hold while pressing ``key``.
        """
        kb = self._get_keyboard()
        try:
            for mod in modifiers:
                kb.press(self._map_modifier(mod))
            kb.press(key)
            time.sleep(self._action_delay)
            kb.release(key)
            for mod in reversed(modifiers):
                kb.release(self._map_modifier(mod))
        except Exception as exc:
            raise InputError(f"Key press failed for '{key}': {exc}") from exc

    def type_text(self, text: str) -> None:
        """Type a string character by character."""
        kb = self._get_keyboard()
        try:
            kb.type(text)
        except Exception as exc:
            raise InputError(f"Type text failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Actions (sequences)
    # ------------------------------------------------------------------

    def execute(self, *actions: Action) -> None:
        """Execute a sequence of actions in order.

        Example::

            emulator.execute(
                ClickAction(100, 200),
                WaitAction(1.0),
                KeyAction('enter'),
            )
        """
        for action in actions:
            if isinstance(action, ClickAction):
                self.click(action.x, action.y, action.button, action.clicks)
            elif isinstance(action, KeyAction):
                self.press(action.key, action.modifiers)
            elif isinstance(action, WaitAction):
                time.sleep(action.seconds)
            else:
                logger.warning(f"Unknown action type: {type(action)}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_mouse(self) -> Any:
        """Lazy-init the pynput mouse controller."""
        if self._mouse is None:
            try:
                from pynput.mouse import Controller as MouseController
            except ImportError as exc:
                raise InputError(
                    "pynput is required for input emulation. Install: pip install pynput"
                ) from exc
            self._mouse = MouseController()
        return self._mouse

    def _get_keyboard(self) -> Any:
        """Lazy-init the pynput keyboard controller."""
        if self._keyboard is None:
            try:
                from pynput.keyboard import Controller as KeyboardController
            except ImportError as exc:
                raise InputError(
                    "pynput is required for input emulation. Install: pip install pynput"
                ) from exc
            self._keyboard = KeyboardController()
        return self._keyboard

    @staticmethod
    def _map_button(button: MouseButton) -> Any:
        """Convert our enum to pynput's button type."""
        try:
            from pynput.mouse import Button
        except ImportError:
            return button  # fallback; will fail later with a clear error
        mapping = {
            MouseButton.LEFT: Button.left,
            MouseButton.RIGHT: Button.right,
            MouseButton.MIDDLE: Button.middle,
        }
        return mapping[button]

    @staticmethod
    def _map_modifier(modifier: KeyModifier) -> Any:
        """Convert our enum to pynput's key type."""
        try:
            from pynput.keyboard import Key
        except ImportError:
            return modifier
        mapping = {
            KeyModifier.CTRL: Key.ctrl,
            KeyModifier.ALT: Key.alt,
            KeyModifier.SHIFT: Key.shift,
        }
        return mapping[modifier]
