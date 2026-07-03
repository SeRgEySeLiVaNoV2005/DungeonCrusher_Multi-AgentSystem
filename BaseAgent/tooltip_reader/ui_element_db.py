"""JSON-based database for recognized UI elements.

Stores element metadata (name, text, position) and saves cropped
screenshots as template images for future template matching.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.core.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class UIElementRecord:
    """A single recognized UI element."""

    id: str
    """Unique identifier (auto-generated or user-provided)."""

    name: str
    """Human-readable label (auto-generated from text or user-provided)."""

    text: str
    """Recognized OCR text at capture time."""

    window_x: int
    """X coordinate relative to the game window."""

    window_y: int
    """Y coordinate relative to the game window."""

    region_width: int
    """Width of the captured region."""

    region_height: int
    """Height of the captured region."""

    template_path: str
    """Path to the saved template image (relative to resources/)."""

    tags: List[str] = field(default_factory=list)
    """Classification tags for search."""

    notes: str = ""
    """Optional user notes."""

    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "text": self.text,
            "window_x": self.window_x,
            "window_y": self.window_y,
            "region_width": self.region_width,
            "region_height": self.region_height,
            "template_path": self.template_path,
            "tags": self.tags,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UIElementRecord":
        return cls(**data)


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------


class UIElementDB:
    """Persistent storage for recognized UI elements.

    Stores elements in a JSON file and saves cropped screenshots as
    template images for the :class:`~src.vision.template_matcher.TemplateMatcher`.

    Usage::

        db = UIElementDB("resources/ui_elements_db.json")
        record = db.add_element(
            name="gold_display",
            text="1,234",
            region_image=<numpy array>,
            window_x=120, window_y=35,
        )
    """

    def __init__(self, db_path: str = "resources/ui_elements_db.json") -> None:
        """
        Args:
            db_path: Path to the JSON database file (relative to project root).
        """
        self._db_path = Path(db_path)
        self._elements: Dict[str, UIElementRecord] = {}
        self._load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_element(
        self,
        text: str,
        region_image: np.ndarray,
        window_x: int,
        window_y: int,
        region_width: int,
        region_height: int,
        name: str = "",
        tags: Optional[List[str]] = None,
    ) -> UIElementRecord:
        """Add a new recognized UI element to the database.

        Args:
            text: OCR-recognized text.
            region_image: Cropped screenshot of the element (BGR numpy array).
            window_x, window_y: Position relative to game window (top-left).
            region_width, region_height: Size of the captured region.
            name: Human-readable name. Auto-generated if empty.
            tags: Optional classification tags.

        Returns:
            The created record.
        """
        # Auto-generate id and name from text.
        element_id = self._text_to_id(text)
        if not name:
            name = self._text_to_name(text)

        # Save template image.
        template_filename = f"{element_id}.png"
        template_dir = self._db_path.parent / "templates"
        template_dir.mkdir(parents=True, exist_ok=True)
        template_path = template_dir / template_filename

        # Save as PNG via OpenCV.
        try:
            import cv2

            cv2.imwrite(str(template_path), region_image)
            logger.info(f"Template saved: {template_path}")
        except ImportError:
            logger.warning("OpenCV not available; template image not saved")
        except Exception:
            logger.exception("Failed to save template image")

        # Relative path for storage.
        rel_path = f"templates/{template_filename}"

        record = UIElementRecord(
            id=element_id,
            name=name,
            text=text,
            window_x=window_x,
            window_y=window_y,
            region_width=region_width,
            region_height=region_height,
            template_path=rel_path,
            tags=tags or [],
        )

        self._elements[record.id] = record
        self._save()
        logger.info(
            f"UI element '{record.name}' saved (id={record.id}, "
            f"text='{record.text}', pos=({record.window_x},{record.window_y}))"
        )
        return record

    def get(self, element_id: str) -> Optional[UIElementRecord]:
        """Retrieve an element by ID."""
        return self._elements.get(element_id)

    def find_by_tag(self, tag: str) -> List[UIElementRecord]:
        """Find all elements with a given tag."""
        return [e for e in self._elements.values() if tag in e.tags]

    def find_by_text(self, substring: str) -> List[UIElementRecord]:
        """Find elements whose recognized text contains the substring."""
        lower = substring.lower()
        return [e for e in self._elements.values() if lower in e.text.lower()]

    def list_all(self) -> List[UIElementRecord]:
        """Return all stored elements."""
        return list(self._elements.values())

    def remove(self, element_id: str) -> bool:
        """Remove an element by ID. Returns True if it existed."""
        if element_id in self._elements:
            del self._elements[element_id]
            self._save()
            return True
        return False

    def count(self) -> int:
        """Return the number of stored elements."""
        return len(self._elements)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load elements from the JSON file."""
        if not self._db_path.exists():
            logger.info(f"UI element database not found at {self._db_path}; starting fresh.")
            return

        try:
            with open(self._db_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Failed to load UI element DB: {exc}; starting fresh.")
            return

        elements_list = data.get("elements", [])
        for item in elements_list:
            try:
                record = UIElementRecord.from_dict(item)
                self._elements[record.id] = record
            except Exception:
                logger.warning(f"Skipping invalid DB entry: {item.get('id', '?')}")

        logger.info(f"Loaded {len(self._elements)} UI element(s) from database")

    def _save(self) -> None:
        """Persist all elements to the JSON file."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(self._elements),
            "elements": [e.to_dict() for e in self._elements.values()],
        }

        with open(self._db_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Name/id helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _text_to_id(text: str) -> str:
        """Convert recognized text to a safe identifier."""
        # Transliterate common Cyrillic to Latin.
        translit = {
            "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
            "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
            "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
            "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
            "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
            "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
            "э": "e", "ю": "yu", "я": "ya",
            "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D",
            "Е": "E", "Ё": "Yo", "Ж": "Zh", "З": "Z", "И": "I",
            "Й": "Y", "К": "K", "Л": "L", "М": "M", "Н": "N",
            "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T",
            "У": "U", "Ф": "F", "Х": "H", "Ц": "Ts", "Ч": "Ch",
            "Ш": "Sh", "Щ": "Sch", "Ъ": "", "Ы": "Y", "Ь": "",
            "Э": "E", "Ю": "Yu", "Я": "Ya",
        }
        clean = "".join(translit.get(c, c) for c in text)
        clean = re.sub(r"[^a-zA-Z0-9_]+", "_", clean)
        clean = clean.strip("_").lower()
        if not clean:
            clean = f"element_{uuid.uuid4().hex[:6]}"
        return clean

    @staticmethod
    def _text_to_name(text: str) -> str:
        """Generate a human-readable name from recognized text."""
        if not text:
            return "Unnamed Element"
        # Truncate and clean.
        name = text.strip()[:60]
        # Replace newlines with spaces.
        name = " ".join(name.split())
        return name if name else "Unnamed Element"
