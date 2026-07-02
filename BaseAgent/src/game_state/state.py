"""Immutable game state representation and history tracking."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from ..core.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Region:
    """A named rectangular region on the game screen."""

    name: str
    x: int
    y: int
    width: int
    height: int

    @property
    def bounds(self) -> Tuple[int, int, int, int]:
        """(left, top, width, height)."""
        return (self.x, self.y, self.width, self.height)

    @property
    def center(self) -> Tuple[int, int]:
        """(cx, cy) of the region center."""
        return (self.x + self.width // 2, self.y + self.height // 2)


@dataclass(frozen=True)
class GameState:
    """A point-in-time snapshot of the game.

    Immutable by design — every frame produces a new instance.
    """

    timestamp: datetime = field(default_factory=datetime.now)

    # Current screenshot (BGR numpy array, stored as bytes for immutability).
    screenshot: Optional[bytes] = None

    # High-level game data populated by vision pipeline.
    gold: Optional[int] = None
    hero_level: Optional[int] = None
    current_stage: Optional[str] = None

    # Detected UI elements.
    ui_elements: Dict[str, List[MatchResult]] = field(default_factory=dict)

    # Raw OCR text from designated regions.
    ocr_texts: Dict[str, str] = field(default_factory=dict)

    # Arbitrary extra data.
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_vision_data(self) -> bool:
        """True if vision pipeline has populated this state."""
        return bool(self.ui_elements) or bool(self.ocr_texts)


# Forward reference for dataclass.
from ..vision.template_matcher import MatchResult  # noqa: E402


class StateTracker:
    """Tracks a rolling window of :class:`GameState` snapshots.

    Provides helpers for diffing consecutive states and querying history.
    """

    def __init__(self, max_history: int = 300):
        """
        Args:
            max_history: Maximum number of states to retain.
        """
        self._states: Deque[GameState] = deque(maxlen=max_history)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def push(self, state: GameState) -> None:
        """Record a new state snapshot."""
        self._states.append(state)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @property
    def current(self) -> Optional[GameState]:
        """The most recent state, or ``None`` if nothing recorded yet."""
        return self._states[-1] if self._states else None

    @property
    def previous(self) -> Optional[GameState]:
        """The second-most-recent state, or ``None``."""
        return self._states[-2] if len(self._states) >= 2 else None

    def history(self, count: Optional[int] = None) -> List[GameState]:
        """Return recent states, oldest first.

        Args:
            count: Limit to the last N states. ``None`` = all.
        """
        states = list(self._states)
        if count is not None:
            states = states[-count:]
        return states

    def diff_gold(self) -> Optional[int]:
        """Return the change in gold between the last two states, or ``None``."""
        if len(self._states) < 2:
            return None
        prev = self._states[-2].gold
        curr = self._states[-1].gold
        if prev is None or curr is None:
            return None
        return curr - prev

    # ------------------------------------------------------------------
    # State checks
    # ------------------------------------------------------------------

    def changed(self, key: str) -> bool:
        """Check whether a metadata value changed between the last two states."""
        if len(self._states) < 2:
            return False
        return self._states[-2].metadata.get(key) != self._states[-1].metadata.get(key)

    def stable_for(self, frames: int) -> bool:
        """True if we have at least ``frames`` recorded."""
        return len(self._states) >= frames
