"""OCR — extract text and numbers from screen regions using Tesseract."""

from __future__ import annotations

import re
from typing import List, Optional

import numpy as np

from ..core.exceptions import VisionError
from ..core.logger import get_logger

logger = get_logger(__name__)


class OCREngine:
    """Optical character recognition wrapper around Tesseract.

    Designed to extract game numbers (gold, hero level, damage) and
    text labels from screen regions.
    """

    def __init__(self, lang: str = "eng") -> None:
        """
        Args:
            lang: Tesseract language code (e.g. ``'eng'``, ``'rus'``, ``'eng+rus'``).
        """
        self._lang = lang

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_text(self, image: np.ndarray,  preprocess: bool = True) -> str:
        """Extract all text from an image region.

        Args:
            image: BGR or grayscale image region.
            preprocess: Apply thresholding + denoising before OCR.

        Returns:
            Extracted text (may be empty).
        """
        try:
            import pytesseract  # noqa: F401
        except ImportError as exc:
            raise VisionError(
                "pytesseract is required for OCR. "
                "Install: pip install pytesseract"
            ) from exc

        if preprocess:
            image = self._preprocess(image)

        try:
            import pytesseract
            text = pytesseract.image_to_string(image, lang=self._lang)
            return text.strip()
        except Exception as exc:
            logger.error(f"OCR failed: {exc}")
            raise VisionError(f"OCR processing error: {exc}") from exc

    def read_number(self, image: np.ndarray, preprocess: bool = True) -> Optional[int]:
        """Extract a single integer from an image region.

        Returns ``None`` if no digits are found.

        Useful for reading gold, hero level, damage counters, etc.
        """
        text = self.read_text(image, preprocess=preprocess)
        numbers = re.findall(r"\d+", text)
        if not numbers:
            return None
        # Return the first number found (most likely the relevant one).
        return int(numbers[0])

    def read_numbers(self, image: np.ndarray, preprocess: bool = True) -> List[int]:
        """Extract all integers from an image region."""
        text = self.read_text(image, preprocess=preprocess)
        return [int(n) for n in re.findall(r"\d+", text)]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _preprocess(image: np.ndarray) -> np.ndarray:
        """Convert to grayscale, threshold, and denoise for better OCR accuracy."""
        try:
            import cv2
        except ImportError as exc:
            raise VisionError(
                "OpenCV is required for image preprocessing. "
                "Install: pip install opencv-python"
            ) from exc

        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()

        # Adaptive thresholding works well with game UI text.
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )

        # Remove small noise.
        denoised = cv2.medianBlur(binary, 3)

        return denoised
