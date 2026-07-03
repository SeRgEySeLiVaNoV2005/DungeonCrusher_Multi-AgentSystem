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

    **Multi-scale matching** (enabled by default) tries each template at
    several scales (e.g. 0.92× – 1.08×) and picks the best match.  This
    makes the system resilient to minor resolution / DPI changes across
    sessions and reboots.
    """

    # Default scale range for multi-scale matching.
    DEFAULT_SCALES: tuple = (1.0, 0.94, 1.06)

    def __init__(
        self,
        templates_dir: str = "resources/templates",
        confidence: float = 0.8,
        multi_scale: bool = True,
        scales: Optional[tuple] = None,
    ) -> None:
        """
        Args:
            templates_dir: Directory containing template ``.png`` files.
            confidence: Minimum confidence threshold (0.0 – 1.0).
            multi_scale: If True, try templates at multiple scales
                         to handle minor rendering differences.
            scales: Tuple of scale factors (e.g. ``(0.9, 1.0, 1.1)``).
                    If omitted, uses :attr:`DEFAULT_SCALES`.
        """
        self._templates_dir = Path(templates_dir)
        self._confidence = confidence
        self._multi_scale = multi_scale
        self._scales = tuple(scales) if scales else self.DEFAULT_SCALES
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

        When ``multi_scale`` is enabled, each template is tried at multiple
        scales and only the best match per template is kept.

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

            if self._multi_scale:
                best = self._match_multi_scale(screenshot, template, name, cv2)
                if best is not None:
                    results.append(best)
            else:
                result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
                locations = np.where(result >= self._confidence)
                scores = result[locations]
                used = set()
                for x, y, score in sorted(
                    zip(locations[1], locations[0], scores),
                    key=lambda t: t[2],
                    reverse=True,
                ):
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

        When ``multi_scale`` is enabled (the default), the template is tried
        at several scales (e.g. 0.90× – 1.10×) and the **best** match across
        all scales is returned.  This makes matching resilient to minor
        resolution / DPI differences between sessions.

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

        if self._multi_scale:
            return self._match_multi_scale(screenshot, template, template_name, cv2)
        else:
            return self._match_single(screenshot, template, template_name, cv2)

    def _match_single(
        self,
        screenshot: np.ndarray,
        template: np.ndarray,
        name: str,
        cv2,
    ) -> Optional[MatchResult]:
        """Match a single template at its native scale."""
        th, tw = template.shape[:2]
        result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val < self._confidence:
            return None

        x, y = max_loc
        return MatchResult(
            name=name,
            x=x + tw // 2,
            y=y + th // 2,
            confidence=float(max_val),
            bounds=(x, y, tw, th),
        )

    def _match_multi_scale(
        self,
        screenshot: np.ndarray,
        template: np.ndarray,
        name: str,
        cv2,
    ) -> Optional[MatchResult]:
        """Try the template at each scale; return the best match.

        Starts at 1.0× and exits early if the match is strong enough,
        avoiding unnecessary resize+match operations.
        """
        th, tw = template.shape[:2]
        sh, sw = screenshot.shape[:2]

        best: Optional[MatchResult] = None

        for scale in self._scales:
            if scale == 1.0:
                # Native scale — no resize needed.
                new_w, new_h = tw, th
                search_img = template
            else:
                new_w = max(4, int(tw * scale))
                new_h = max(4, int(th * scale))
                if new_h > sh or new_w > sw:
                    continue
                search_img = cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

            result = cv2.matchTemplate(screenshot, search_img, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val >= self._confidence:
                x, y = max_loc
                candidate = MatchResult(
                    name=name,
                    x=x + new_w // 2,
                    y=y + new_h // 2,
                    confidence=float(max_val),
                    bounds=(x, y, new_w, new_h),
                )
                if best is None or candidate.confidence > best.confidence:
                    best = candidate
                # Early exit: native scale matched well — skip other scales.
                if scale == 1.0 and max_val >= self._confidence + 0.05:
                    return best

        return best

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
