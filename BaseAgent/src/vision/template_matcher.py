"""OpenCV template matching — find UI elements on the game screen."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from ..core.exceptions import VisionError
from ..core.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class MatchResult:
    """A detected template match on the screen."""

    name: str
    """Template file name (without extension)."""

    x: int
    y: int
    """Center coordinates of the match."""

    confidence: float
    """Match confidence (0.0 – 1.0). Higher is better."""

    bounds: Tuple[int, int, int, int]
    """Bounding box (left, top, width, height)."""

    @property
    def center(self) -> Tuple[int, int]:
        """(x, y) of the bounding-box center."""
        left, top, w, h = self.bounds
        return (left + w // 2, top + h // 2)


class TemplateMatcher:
    """Find pre-saved UI-element templates in a screenshot.

    Templates are small PNG images stored under ``resources/templates/``.
    Each file name (without extension) becomes the match ``name``.

    Uses OpenCV ``TM_CCOEFF_NORMED`` which is robust to brightness changes.
    """

    def __init__(
        self,
        templates_dir: str = "resources/templates",
        confidence: float = 0.8,
    ) -> None:
        """
        Args:
            templates_dir: Directory containing template ``.png`` files.
            confidence: Minimum confidence threshold (0.0 – 1.0).
        """
        self._templates_dir = Path(templates_dir)
        self._confidence = confidence
        self._templates: dict[str, np.ndarray] = {}  # name → BGR array
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_templates(self) -> int:
        """Load all templates from the templates directory into memory.

        Returns:
            Number of templates loaded.
        """
        try:
            import cv2
        except ImportError as exc:
            raise VisionError(
                "OpenCV is required for template matching. "
                "Install: pip install opencv-python"
            ) from exc

        self._templates.clear()
        for png_path in self._templates_dir.glob("*.png"):
            template = cv2.imread(str(png_path), cv2.IMREAD_COLOR)
            if template is None:
                logger.warning(f"Could not read template: {png_path}")
                continue
            name = png_path.stem
            self._templates[name] = template
            logger.debug(f"Loaded template '{name}' ({template.shape[1]}x{template.shape[0]}px)")

        logger.info(f"Loaded {len(self._templates)} templates")
        return len(self._templates)

    def find_all(self, screenshot: np.ndarray) -> List[MatchResult]:
        """Find *all* templates in a screenshot.

        Args:
            screenshot: BGR image as a numpy array (H×W×C).

        Returns:
            List of matches, sorted by confidence (highest first).
        """
        try:
            import cv2
        except ImportError as exc:
            raise VisionError(
                "OpenCV is required for template matching. "
                "Install: pip install opencv-python"
            ) from exc

        if not self._loaded:
            self.load_templates()
            self._loaded = True

        results: List[MatchResult] = []

        for name, template in self._templates.items():
            th, tw = template.shape[:2]
            sh, sw = screenshot.shape[:2]

            if th > sh or tw > sw:
                logger.debug(f"Template '{name}' is larger than screenshot; skipping")
                continue

            result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)

            # Find all matches above threshold.
            locations = np.where(result >= self._confidence)
            scores = result[locations]

            # Non-max suppression: group nearby matches.
            # zip produces (col, row, score) triples — unpack as (x, y, score).
            used = set()
            for x, y, score in sorted(
                zip(locations[1], locations[0], scores),
                key=lambda t: t[2],
                reverse=True,
            ):
                # Skip if too close to an already-reported match.
                key = (y // th, x // tw)
                if key in used:
                    continue
                used.add(key)

                results.append(
                    MatchResult(
                        name=name,
                        x=x + tw // 2,
                        y=y + th // 2,
                        confidence=float(score),
                        bounds=(x, y, tw, th),
                    )
                )

        results.sort(key=lambda r: r.confidence, reverse=True)
        return results

    def find(self, screenshot: np.ndarray, template_name: str) -> Optional[MatchResult]:
        """Find a single template by name. Returns the best match or ``None``."""
        return self.find_one(screenshot, template_name)

    def find_one(self, screenshot: np.ndarray, template_name: str) -> Optional[MatchResult]:
        """Fast lookup — match only *one* named template against the screenshot.

        Much faster than :meth:`find_all` when you know which template you
        are looking for (avoids iterating all 14+ templates).

        Args:
            screenshot: BGR image as a numpy array (H×W×C).
            template_name: Name of the template to find.

        Returns:
            The best :class:`MatchResult` above the confidence threshold,
            or ``None``.
        """
        try:
            import cv2
        except ImportError as exc:
            raise VisionError(
                "OpenCV is required for template matching. "
                "Install: pip install opencv-python"
            ) from exc

        template = self._templates.get(template_name)
        if template is None:
            logger.debug(f"Template '{template_name}' not loaded")
            return None

        th, tw = template.shape[:2]
        sh, sw = screenshot.shape[:2]

        if th > sh or tw > sw:
            logger.debug(f"Template '{template_name}' is larger than screenshot; skipping")
            return None

        result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)

        # Find the single best match.
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val < self._confidence:
            return None

        x, y = max_loc
        return MatchResult(
            name=template_name,
            x=x + tw // 2,
            y=y + th // 2,
            confidence=float(max_val),
            bounds=(x, y, tw, th),
        )

    # ------------------------------------------------------------------
    # Runtime template management
    # ------------------------------------------------------------------

    def add_template(self, name: str, image: np.ndarray) -> None:
        """Register a template image at runtime (no disk write).

        Use this when a new UI element is saved to the database — the
        TemplateMatcher can use it immediately without a full reload.

        Args:
            name: Template name (used as the match ``name``).
            image: BGR numpy array of the template.
        """
        if image is None or image.size == 0:
            logger.warning(f"TemplateMatcher.add_template: empty image for '{name}'")
            return
        self._templates[name] = image
        self._loaded = True
        logger.info(
            f"TemplateMatcher: added '{name}' ({image.shape[1]}x{image.shape[0]}px) — "
            f"{len(self._templates)} total"
        )

    def remove_template(self, name: str) -> bool:
        """Remove a template by name. Returns True if it existed."""
        if name in self._templates:
            del self._templates[name]
            logger.info(f"TemplateMatcher: removed '{name}' — {len(self._templates)} remaining")
            return True
        return False

    @property
    def template_count(self) -> int:
        """Number of loaded templates."""
        return len(self._templates)

    @property
    def template_names(self) -> tuple:
        """Tuple of all loaded template names."""
        return tuple(self._templates.keys())
