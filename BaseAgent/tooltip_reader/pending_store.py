"""Thread-safe store for UI elements awaiting user review.

When the TooltipReaderAgent captures a tooltip (CTRL+H), the element is
placed here instead of being saved directly. The web review interface
reads from this store, lets the user edit fields, and either saves to
the database or discards.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from src.core.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data type
# ---------------------------------------------------------------------------


@dataclass
class PendingElement:
    """A UI element captured by the agent, waiting for user review.

    Attributes:
        id: Unique identifier for this pending item.
        image: Cropped screenshot region (BGR numpy array).
        raw_text: OCR output before user correction.
        edited_text: User-corrected text (starts identical to raw_text).
        name: Human-readable name (auto-generated, user-editable).
        tags: Classification tags (user-editable).
        window_x, window_y: Position relative to game window.
        region_width, region_height: Size of captured region.
        created_at: ISO timestamp of capture.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    image: Optional[np.ndarray] = None
    raw_text: str = ""
    edited_text: str = ""
    name: str = ""
    tags: List[str] = field(default_factory=list)
    window_x: int = 0
    window_y: int = 0
    region_width: int = 0
    region_height: int = 0
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        if not self.edited_text and self.raw_text:
            self.edited_text = self.raw_text
        if not self.name and self.raw_text:
            self.name = self.raw_text.strip()[:60]

    def to_dict(self, include_image: bool = False) -> dict:
        """Serialize to a JSON-safe dict.

        Args:
            include_image: If True, encode the image as base64 PNG.
        """
        data = {
            "id": self.id,
            "raw_text": self.raw_text,
            "edited_text": self.edited_text,
            "name": self.name,
            "tags": self.tags,
            "window_x": self.window_x,
            "window_y": self.window_y,
            "region_width": self.region_width,
            "region_height": self.region_height,
            "created_at": self.created_at,
        }
        if include_image and self.image is not None:
            import base64

            import cv2

            _, buf = cv2.imencode(".png", self.image)
            data["image_base64"] = base64.b64encode(buf).decode("ascii")
        return data


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PendingElementStore:
    """A thread-safe store for elements waiting for user review.

    Shared between the TooltipReaderAgent (producer) and the
    WebReviewServer (consumer/editor).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: Dict[str, PendingElement] = {}

    # ------------------------------------------------------------------
    # Producer (agent)
    # ------------------------------------------------------------------

    def add(
        self,
        image: np.ndarray,
        raw_text: str,
        window_x: int,
        window_y: int,
        region_width: int,
        region_height: int,
    ) -> PendingElement:
        """Add a newly captured element to the review queue.

        Called by the TooltipReaderAgent after OCR.
        """
        element = PendingElement(
            image=image.copy(),  # Defensive copy.
            raw_text=raw_text,
            window_x=window_x,
            window_y=window_y,
            region_width=region_width,
            region_height=region_height,
        )
        with self._lock:
            self._items[element.id] = element
        logger.info(
            f"[PendingStore] Element '{element.id}' added — "
            f"'{raw_text[:50]}' at ({window_x},{window_y})"
        )
        return element

    # ------------------------------------------------------------------
    # Consumer (web server)
    # ------------------------------------------------------------------

    def get_all(self, include_image: bool = False) -> List[dict]:
        """Return all pending elements as dicts (newest first)."""
        with self._lock:
            items = list(self._items.values())
        # Newest first.
        items.reverse()
        return [e.to_dict(include_image=include_image) for e in items]

    def get(self, element_id: str, include_image: bool = False) -> Optional[dict]:
        """Return a single pending element by ID."""
        with self._lock:
            element = self._items.get(element_id)
        if element is None:
            return None
        return element.to_dict(include_image=include_image)

    def update(
        self,
        element_id: str,
        name: Optional[str] = None,
        edited_text: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> bool:
        """Update editable fields of a pending element.

        Returns True if the element existed.
        """
        with self._lock:
            element = self._items.get(element_id)
            if element is None:
                return False
            if name is not None:
                element.name = name
            if edited_text is not None:
                element.edited_text = edited_text
            if tags is not None:
                element.tags = tags
        return True

    def pop(self, element_id: str) -> Optional[PendingElement]:
        """Remove and return an element (for saving to DB or discarding).

        Returns None if the element doesn't exist.
        """
        with self._lock:
            return self._items.pop(element_id, None)

    def remove(self, element_id: str) -> bool:
        """Discard an element without saving.

        Returns True if the element existed.
        """
        with self._lock:
            if element_id in self._items:
                del self._items[element_id]
                logger.info(f"[PendingStore] Element '{element_id}' discarded")
                return True
        return False

    def count(self) -> int:
        """Number of pending elements."""
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        """Remove all pending elements."""
        with self._lock:
            self._items.clear()
