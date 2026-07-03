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
        self._hotkey_running = False

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
        """Register CTRL+H as a global hotkey via Win32 API.

        Uses ``RegisterHotKey`` which works at the OS level — reliable
        even when DirectX games have keyboard focus.
        """
        import ctypes
        from ctypes import wintypes

        MOD_CONTROL = 0x0002
        VK_H = 0x48
        WM_HOTKEY = 0x0312
        HOTKEY_ID = 1

        self._hotkey_running = True

        def hotkey_thread() -> None:
            user32 = ctypes.windll.user32

            if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL, VK_H):
                logger.warning(
                    "[TooltipReader] RegisterHotKey failed — hotkey may already "
                    "be registered by another app, or no admin rights."
                )
                return

            logger.debug("[TooltipReader] Hotkey CTRL+H registered via Win32")

            # Windows message loop — blocks until WM_QUIT.
            msg = wintypes.MSG()
            while self._hotkey_running:
                # PeekMessage with a 200ms timeout so we can check _hotkey_running.
                result = user32.PeekMessageW(
                    ctypes.byref(msg), None, 0, 0, 1  # PM_REMOVE
                )
                if result == 0:
                    continue  # No message — loop and check running flag.

                if msg.message == WM_HOTKEY:
                    self._on_hotkey_triggered()
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

            user32.UnregisterHotKey(None, HOTKEY_ID)

        thread = threading.Thread(
            target=hotkey_thread,
            name=f"hotkey-{self.name}",
            daemon=True,
        )
        thread.start()

    def _on_hotkey_triggered(self) -> None:
        """Called when the global hotkey is pressed."""
        with self._trigger_lock:
            if self._state == ReaderState.IDLE:
                self._state = ReaderState.TRIGGERED
                try:
                    x, y = get_cursor_position()
                except Exception:
                    x, y = (-1, -1)
                logger.info(
                    f"\n{'=' * 55}\n"
                    f"  CTRL+H — Tooltip Reader triggered\n"
                    f"  Cursor at screen ({x}, {y})\n"
                    f"  Reading on next frame...\n"
                    f"{'=' * 55}"
                )

    def _stop_hotkey_listener(self) -> None:
        """Signal the hotkey thread to exit."""
        self._hotkey_running = False
        # Post a dummy message to wake up the message loop.
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.PostThreadMessageW(
                ctypes.windll.kernel32.GetCurrentThreadId(), 0, 0, 0
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internals — OCR pipeline
    # ------------------------------------------------------------------

    def _process_trigger(
        self,
        screenshot: np.ndarray,
        window_region: Optional[dict],
    ) -> None:
        """Capture the UI element (clipboard or screen), run OCR, send to review.

        Priority:
        1. If an image is on the clipboard (Win+Shift+S / Snipping Tool),
           use it directly — the user already selected the exact region.
        2. Otherwise, crop a region around the cursor from the screenshot.

        OCR is run with multiple strategies and the best result is kept.
        """
        # 1. Get cursor position.
        try:
            cursor_x, cursor_y = get_cursor_position()
        except Exception:
            logger.exception("[TooltipReader] Failed to get cursor position")
            return

        # 2. Compute window-relative coordinates.
        if window_region:
            win_left = window_region.get("left", 0)
            win_top = window_region.get("top", 0)
        else:
            win_left, win_top = 0, 0

        rel_x = cursor_x - win_left
        rel_y = cursor_y - win_top

        # 3. Try clipboard first (Snipping Tool / Win+Shift+S).
        region, source = self._get_clipboard_image()
        if region is not None:
            region_w, region_h = region.shape[1], region.shape[0]
            window_x, window_y = rel_x, rel_y
            logger.info(
                f"[TooltipReader] Using clipboard image ({region_w}x{region_h})"
            )
        else:
            # 4. Fall back to screen capture.
            h, w = screenshot.shape[:2]
            half_w = self._region_width // 2
            half_h = self._region_height // 2
            x1 = max(0, rel_x - half_w)
            y1 = max(0, rel_y - half_h)
            x2 = min(w, rel_x + half_w)
            y2 = min(h, rel_y + half_h)

            if x1 >= x2 or y1 >= y2:
                logger.warning(
                    f"[TooltipReader] Cursor ({rel_x},{rel_y}) outside "
                    f"screenshot ({w}x{h})"
                )
                return

            region = screenshot[y1:y2, x1:x2]
            region_w = x2 - x1
            region_h = y2 - y1
            window_x = rel_x - half_w
            window_y = rel_y - half_h
            source = f"screen ({region_w}x{region_h})"

        # 5. OCR with multiple strategies — pick the best result.
        raw_text = self._ocr_best_effort(region)

        # 6. Log result.
        if raw_text:
            logger.info(
                f"\n{'─' * 55}\n"
                f"  Source: {source}\n"
                f"  Raw OCR: \"{raw_text}\"\n"
                f"  Position: ({window_x}, {window_y})\n"
                f"  Review at http://localhost:8765\n"
                f"{'─' * 55}"
            )
        else:
            logger.info(
                f"[TooltipReader] No text recognized. "
                f"Source: {source}, pos: ({window_x}, {window_y})"
            )

        # 7. Add to pending review queue.
        try:
            element = self._pending_store.add(
                image=region,
                raw_text=raw_text or "",
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
    # Internals — clipboard
    # ------------------------------------------------------------------

    @staticmethod
    def _get_clipboard_image() -> tuple:
        """Try to read an image from the Windows clipboard.

        Returns:
            ``(image, description)`` where *image* is a BGR numpy array,
            or ``(None, '')`` if no image is on the clipboard.
        """
        try:
            from PIL import ImageGrab
        except ImportError:
            return (None, "")

        try:
            pil_image = ImageGrab.grabclipboard()
        except Exception:
            return (None, "")

        if pil_image is None:
            return (None, "")

        # PIL Image → BGR numpy array.
        import numpy as np
        import cv2

        if pil_image.mode == "RGBA":
            pil_image = pil_image.convert("RGB")
        rgb = np.array(pil_image)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        return (bgr, f"clipboard ({w}x{h})")

    # ------------------------------------------------------------------
    # Internals — OCR best effort
    # ------------------------------------------------------------------

    def _ocr_best_effort(self, region: np.ndarray) -> str:
        """Run OCR with multiple strategies and return the best result.

        Tries different preprocessing pipelines AND languages:
        - eng (English) — for numbers, Latin text
        - rus (Russian) — for Cyrillic (common in the game)
        - rus+eng — combined

        Strategies are tried in order; the first non-empty, non-garbled
        result wins.
        """
        if self._ocr_engine is None:
            return ""

        # Lazy-init additional language engines.
        if not hasattr(self, '_ocr_rus'):
            self._ocr_rus = OCREngine(lang="rus")
        if not hasattr(self, '_ocr_both'):
            self._ocr_both = OCREngine(lang="rus+eng")

        # (label, engine, preprocess_fn, use_default_preprocess)
        strategies = [
            # Tooltip preprocessing (invert + upscale + OTSU)
            ("inv+eng", self._ocr_engine,
             lambda r: self._preprocess_for_tooltip(r), False),
            ("inv+rus+eng", self._ocr_both,
             lambda r: self._preprocess_for_tooltip(r), False),
            ("inv+rus", self._ocr_rus,
             lambda r: self._preprocess_for_tooltip(r), False),
            # Without invert
            ("noinv+eng", self._ocr_engine,
             lambda r: self._preprocess_for(r, invert=False), False),
            ("noinv+rus+eng", self._ocr_both,
             lambda r: self._preprocess_for(r, invert=False), False),
            ("noinv+rus", self._ocr_rus,
             lambda r: self._preprocess_for(r, invert=False), False),
            # Default OCREngine preprocessing
            ("default+eng", self._ocr_engine, None, True),
            ("default+rus+eng", self._ocr_both, None, True),
            ("default+rus", self._ocr_rus, None, True),
        ]

        for name, engine, preprocess_fn, use_default in strategies:
            try:
                if use_default:
                    text = engine.read_text(region, preprocess=True)
                else:
                    processed = preprocess_fn(region)
                    text = engine.read_text(processed, preprocess=False)
                if text and text.strip():
                    logger.debug(f"[TooltipReader] OCR '{name}': \"{text}\"")
                    return text.strip()
            except Exception:
                logger.debug(f"[TooltipReader] OCR strategy '{name}' failed")

        return ""

    def _preprocess_for(self, region: np.ndarray, invert: bool) -> np.ndarray:
        """Preprocess with optional inversion (OTSU-based pipeline)."""
        try:
            import cv2
        except ImportError:
            return region

        image = region.copy()
        if self._scale > 1.0:
            image = cv2.resize(image, None, fx=self._scale, fy=self._scale,
                               interpolation=cv2.INTER_CUBIC)

        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()

        if invert:
            gray = cv2.bitwise_not(gray)

        _, binary = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        return cv2.medianBlur(binary, 3)

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
