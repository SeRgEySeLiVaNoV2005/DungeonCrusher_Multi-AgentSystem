"""TooltipReaderAgent — OCR debug agent triggered by CTRL+H hotkey.

Captures the screen region around the mouse cursor, runs OCR,
logs the result to the console, and places the element into the
**pending review queue**. Use the web interface at
``http://localhost:8765`` to review, edit, and save to the database.

Usage (manual, during gameplay):
    1. Hover the mouse over a UI element (e.g., gold counter, button).
    2. Press CTRL+H.
    3. The agent logs the recognized text to the console.
    4. Open http://localhost:8765 in a browser.
    5. Review the element: correct the name, text, and tags.
    6. Click "Save to DB" — the element is stored with its template image.
"""

from __future__ import annotations

import threading
from enum import Enum, auto
from typing import Optional, Tuple

import numpy as np

from base.child_agent import ChildAgent
from src.communication.message_bus import Message, MessageBus, MessageType
from src.core.logger import get_logger
from src.input.emulator import get_cursor_position
from src.vision.ocr import OCREngine

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class ReaderState(Enum):
    """Internal states of the TooltipReaderAgent."""

    IDLE = auto()
    """Waiting for a hotkey trigger."""

    TRIGGERED = auto()
    """CTRL+H was pressed; will OCR on the next frame."""

    SHUTDOWN = auto()
    """Agent is stopping."""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class TooltipReaderAgent(ChildAgent):
    """A debug agent that reads game tooltips on demand via CTRL+H.

    The **user** moves the mouse manually, then presses CTRL+H.
    The agent captures the region around the cursor, runs OCR, logs
    the result, and stores the element in the UI element database.

    In the future, autonomous agents can look up stored elements by
    template matching against the saved images.
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(
        self,
        name: str,
        bus: MessageBus,
        pending_store: "PendingElementStore",
        domain: str = "debug",
        region_width: int = 320,
        region_height: int = 90,
        ocr_lang: str = "eng",
        scale: float = 2.0,
        invert: bool = True,
    ) -> None:
        """
        Args:
            name: Unique agent name (e.g. ``'tooltip_reader'``).
            bus: Shared message bus.
            pending_store: Shared store for elements awaiting user review.
            domain: Agent domain (default ``'debug'``).
            region_width: Width of the capture region around the cursor.
            region_height: Height of the capture region around the cursor.
            ocr_lang: Tesseract language code.
            scale: Scale factor to upscale the region before OCR (1.0 = no scaling).
            invert: If True, invert colors for light-on-dark game tooltips.
        """
        super().__init__(name, bus, domain)
        self._region_width = region_width
        self._region_height = region_height
        self._scale = scale
        self._invert = invert
        self._pending_store = pending_store

        # OCR engine (lazy-init).
        self._ocr_engine: Optional[OCREngine] = None
        self._ocr_lang = ocr_lang

        # State.
        self._state = ReaderState.IDLE
        self._trigger_lock = threading.Lock()

        # Hotkey listener.
        self._hotkey_listener = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Subscribe to frames and start the hotkey listener."""
        # Inherit ChildAgent's frame subscription.
        super().on_start()

        # Lazy-init OCR engine.
        self._ocr_engine = OCREngine(lang=self._ocr_lang)

        # Start the CTRL+H hotkey listener in a background thread.
        self._start_hotkey_listener()

        logger.info(
            f"TooltipReaderAgent '{self.name}' ready. "
            f"Press CTRL+H to read text under cursor."
        )

    def on_stop(self) -> None:
        """Stop the hotkey listener and unsubscribe."""
        self._state = ReaderState.SHUTDOWN
        self._stop_hotkey_listener()

        # Inherit ChildAgent's cleanup.
        super().on_stop()
        logger.info(f"TooltipReaderAgent '{self.name}' stopped")

    # ------------------------------------------------------------------
    # Frame handling
    # ------------------------------------------------------------------

    def on_frame(self, message: Message) -> None:
        """Process a new game frame. If CTRL+H was pressed, run OCR on the
        region around the cursor and save the element."""
        if self._state != ReaderState.TRIGGERED:
            return

        state = message.payload
        if state is None:
            return

        screenshot = getattr(state, "screenshot", None)
        if screenshot is None:
            logger.warning("[TooltipReader] No screenshot available — skipping OCR")
            self._state = ReaderState.IDLE
            return

        # Get window region from metadata for coordinate conversion.
        window_region = state.metadata.get("window_region") if hasattr(state, "metadata") else None

        try:
            self._process_trigger(screenshot, window_region)
        except Exception:
            logger.exception("[TooltipReader] OCR processing failed")

        self._state = ReaderState.IDLE

    # ------------------------------------------------------------------
    # Internals — hotkey
    # ------------------------------------------------------------------

    def _start_hotkey_listener(self) -> None:
        """Launch the pynput GlobalHotKeys listener for CTRL+H."""
        try:
            from pynput.keyboard import GlobalHotKeys
        except ImportError:
            logger.warning(
                "[TooltipReader] pynput not available; hotkey disabled. "
                "Install: pip install pynput"
            )
            return

        def on_activate() -> None:
            with self._trigger_lock:
                # Avoid queuing multiple triggers.
                if self._state == ReaderState.IDLE:
                    self._state = ReaderState.TRIGGERED
                    x, y = get_cursor_position()
                    logger.info(
                        f"\n{'=' * 55}\n"
                        f"  CTRL+H — Tooltip Reader triggered\n"
                        f"  Cursor at screen ({x}, {y})\n"
                        f"  Reading on next frame...\n"
                        f"{'=' * 55}"
                    )

        try:
            self._hotkey_listener = GlobalHotKeys({"<ctrl>+h": on_activate})
            # Run in daemon thread so it doesn't block shutdown.
            thread = threading.Thread(
                target=self._hotkey_listener.run,
                name=f"hotkey-{self.name}",
                daemon=True,
            )
            thread.start()
        except Exception:
            logger.exception("[TooltipReader] Failed to start hotkey listener")

    def _stop_hotkey_listener(self) -> None:
        """Stop the hotkey listener if running."""
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None

    # ------------------------------------------------------------------
    # Internals — OCR pipeline
    # ------------------------------------------------------------------

    def _process_trigger(
        self,
        screenshot: np.ndarray,
        window_region: Optional[dict],
    ) -> None:
        """Crop region around cursor, run OCR, log and send to review queue.

        Args:
            screenshot: BGR numpy array of the game window.
            window_region: Dict with ``left``, ``top``, ``width``, ``height``
                           of the game window on screen, or None.
        """
        # 1. Get cursor position.
        try:
            cursor_x, cursor_y = get_cursor_position()
        except Exception:
            logger.exception("[TooltipReader] Failed to get cursor position")
            return

        # 2. Convert to window-relative coordinates.
        if window_region:
            win_left = window_region.get("left", 0)
            win_top = window_region.get("top", 0)
        else:
            win_left, win_top = 0, 0
            logger.debug("[TooltipReader] No window_region; using raw coordinates")

        rel_x = cursor_x - win_left
        rel_y = cursor_y - win_top
        h, w = screenshot.shape[:2]

        # 3. Compute crop bounds (clamped to screenshot).
        half_w = self._region_width // 2
        half_h = self._region_height // 2
        x1 = max(0, rel_x - half_w)
        y1 = max(0, rel_y - half_h)
        x2 = min(w, rel_x + half_w)
        y2 = min(h, rel_y + half_h)

        if x1 >= x2 or y1 >= y2:
            logger.warning(
                f"[TooltipReader] Cursor ({rel_x},{rel_y}) is outside "
                f"screenshot bounds ({w}x{h})"
            )
            return

        region = screenshot[y1:y2, x1:x2]

        # 4. Preprocess for game text.
        processed = self._preprocess_for_tooltip(region)

        # 5. OCR.
        if self._ocr_engine is None:
            logger.warning("[TooltipReader] OCR engine not initialized")
            return

        text = self._ocr_engine.read_text(processed, preprocess=False)
        # Also try with default preprocessing as fallback.
        if not text:
            text_alt = self._ocr_engine.read_text(region, preprocess=True)
            if text_alt:
                text = text_alt
                logger.debug("[TooltipReader] Used fallback preprocessing")

        # 6. Log result.
        if text:
            logger.info(
                f"\n{'─' * 55}\n"
                f"  Recognized text: \"{text}\"\n"
                f"  Region: ({rel_x},{rel_y}) ± ({half_w},{half_h})px\n"
                f"  Window pos: ({rel_x},{rel_y})\n"
                f"  Review at http://localhost:8765\n"
                f"{'─' * 55}"
            )
        else:
            logger.info(
                f"[TooltipReader] No text recognized at ({rel_x},{rel_y}) — "
                f"region may be empty or text is too stylized"
            )

        # 7. Add to pending review queue (not auto-saved).
        window_x = rel_x - half_w
        window_y = rel_y - half_h
        region_w = x2 - x1
        region_h = y2 - y1

        try:
            element = self._pending_store.add(
                image=region,
                raw_text=text or "",
                window_x=window_x,
                window_y=window_y,
                region_width=region_w,
                region_height=region_h,
            )
            logger.info(
                f"[TooltipReader] Added to review queue (id={element.id}). "
                f"Pending: {self._pending_store.count()}"
            )
        except Exception:
            logger.exception("[TooltipReader] Failed to add element to pending store")

    # ------------------------------------------------------------------
    # Internals — image preprocessing
    # ------------------------------------------------------------------

    def _preprocess_for_tooltip(self, region: np.ndarray) -> np.ndarray:
        """Apply game-tailored preprocessing to improve OCR accuracy.

        Steps:
        1. Upscale (if scale > 1.0) — helps Tesseract with small fonts.
        2. Convert to grayscale.
        3. Invert if needed (light text on dark background → dark text on light).
        4. Adaptive threshold + denoise.
        """
        try:
            import cv2
        except ImportError:
            logger.warning("OpenCV not available for preprocessing; returning raw region")
            return region

        image = region.copy()

        # Step 1: Upscale.
        if self._scale > 1.0:
            image = cv2.resize(
                image, None,
                fx=self._scale, fy=self._scale,
                interpolation=cv2.INTER_CUBIC,
            )

        # Step 2: Grayscale.
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()

        # Step 3: Invert for light-on-dark text.
        if self._invert:
            gray = cv2.bitwise_not(gray)

        # Step 4: OTSU threshold (handles varied game text better than adaptive).
        _, binary = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )

        # Step 5: Denoise.
        denoised = cv2.medianBlur(binary, 3)

        return denoised
