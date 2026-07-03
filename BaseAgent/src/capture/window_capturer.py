"""Screen capture using MSS — window detection and screenshot pipeline."""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..core.exceptions import CaptureError
from ..core.logger import get_logger

logger = get_logger(__name__)


class WindowCapturer:
    """Captures screenshots of a specific window using MSS.

    Designed for capturing the VK Play game window of
    *Dungeon Crusher: Soul Hunters*.
    """

    def __init__(
        self,
        window_title: str = "VK Play",
        window_keywords: Optional[List[str]] = None,
        monitor: int = 0,
        target_fps: int = 10,
    ) -> None:
        """
        Args:
            window_title: Exact window title to search for.
            window_keywords: Fallback keywords if exact title fails.
            monitor: MSS monitor index (0 = primary).
            target_fps: Desired capture rate; used to calculate frame delay.
        """
        self._window_title = window_title
        self._window_keywords = window_keywords or []
        self._monitor_index = monitor
        self._target_fps = target_fps
        self._frame_delay = 1.0 / target_fps

        # Populated after first successful capture.
        self._window_region: Optional[Dict[str, int]] = None
        self._window_hwnd: Optional[int] = None
        self._last_capture_time: float = 0.0

        # Lazy-imported mss instance.
        self._sct: Optional[object] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def locate_window(self) -> Dict[str, int]:
        """Find the game window and return its bounding box.

        Returns:
            A dict with ``left``, ``top``, ``width``, ``height`` keys.

        Raises:
            CaptureError: If the window cannot be found.
        """
        try:
            import mss
        except ImportError as exc:
            raise CaptureError(
                "MSS is required for screen capture. Install: pip install mss"
            ) from exc

        # Strategy 1: try exact title match.
        for monitor in mss.mss().monitors:
            # MSS doesn't expose window titles directly, so we use win32gui
            # on Windows for window enumeration.
            pass

        return self._find_window_via_win32()

    def capture(self) -> np.ndarray:
        """Capture the current game window as a BGR numpy array (OpenCV-ready).

        Returns:
            Screenshot as a ``numpy.ndarray`` in H×W×C (BGR) format.

        Raises:
            CaptureError: If the window was not located first.
        """
        if self._window_region is None:
            self.locate_window()

        self._throttle()

        try:
            import mss

            if self._sct is None:
                self._sct = mss.mss()

            sct = self._sct
            frame = np.array(sct.grab(self._window_region))
            # MSS returns BGRA; drop the alpha channel for OpenCV.
            return frame[:, :, :3]
        except Exception as exc:
            logger.error(f"Screen capture failed: {exc}")
            raise CaptureError(f"Failed to capture screen: {exc}") from exc

    def is_available(self) -> bool:
        """Check whether MSS + the game window are available."""
        try:
            self.locate_window()
            return True
        except CaptureError:
            return False

    @property
    def window_region(self) -> Optional[Dict[str, int]]:
        """The current window bounding box, or ``None`` if not yet located."""
        return self._window_region

    def bring_to_front(self) -> bool:
        """Bring the game window to the foreground (top of Z-order).

        Call this before capturing if the game might be behind other windows.

        Returns:
            ``True`` if the window was successfully brought to front.
        """
        if self._window_hwnd is None:
            logger.warning("No game window HWND — cannot bring to front")
            return False

        try:
            import ctypes
            user32 = ctypes.windll.user32

            # If the window is minimised, restore it first.
            SW_RESTORE = 9
            if user32.IsIconic(self._window_hwnd):
                user32.ShowWindow(self._window_hwnd, SW_RESTORE)

            # Bring to front.
            user32.SetForegroundWindow(self._window_hwnd)
            # Allow the window to render after coming to front.
            import time
            time.sleep(0.15)
            logger.debug("Game window brought to front")
            return True
        except Exception:
            logger.debug("Failed to bring game window to front", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Sleep to meet the target FPS."""
        elapsed = time.perf_counter() - self._last_capture_time
        if elapsed < self._frame_delay:
            time.sleep(self._frame_delay - elapsed)
        self._last_capture_time = time.perf_counter()

    def _find_window_via_win32(self) -> Dict[str, int]:
        """Use Win32 API to enumerate windows and find the game.

        On non-Windows platforms this is a stub that raises immediately.
        """
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            raise CaptureError(
                "Window enumeration via Win32 is only supported on Windows."
            )

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Candidate storage.
        candidates: List[Tuple[str, int, int, int, int, int]] = []  # +hwnd

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def _enum_handler(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top

            # Filter out hidden/ghost windows:
            #   - Minimum size: at least 200x150 to filter tiny overlays.
            #   - Ignore windows placed far off-screen (negative coords < -1000).
            if width >= 200 and height >= 150:
                if rect.left >= -1000 and rect.top >= -1000:
                    candidates.append((title, rect.left, rect.top, width, height, hwnd))
            return True

        enum_proc = WNDENUMPROC(_enum_handler)
        user32.EnumWindows(enum_proc, 0)

        # Sort by size (descending) — the largest matching window is most
        # likely the actual game.
        candidates.sort(key=lambda c: c[3] * c[4], reverse=True)

        def _store_region(title: str, left: int, top: int, width: int, height: int, hwnd: int):
            self._window_hwnd = hwnd
            self._window_region = {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            }

        # Strategy 1: exact title.
        for title, left, top, width, height, hwnd in candidates:
            if title.lower() == self._window_title.lower():
                _store_region(title, left, top, width, height, hwnd)
                logger.info(
                    f"Found window by exact title: '{title}' "
                    f"({width}x{height} at {left},{top})"
                )
                return self._window_region

        # Strategy 2: keyword match.
        for title, left, top, width, height, hwnd in candidates:
            title_lower = title.lower()
            for kw in self._window_keywords:
                if kw.lower() in title_lower:
                    _store_region(title, left, top, width, height, hwnd)
                    logger.info(
                        f"Found window by keyword '{kw}': '{title}' "
                        f"({width}x{height} at {left},{top})"
                    )
                    return self._window_region

        # Debug: list all visible windows to help diagnose.
        visible = [c[0] for c in candidates[:20]]
        logger.warning(f"Visible windows (≥200×150): {visible}")

        raise CaptureError(
            f"Game window not found. Tried exact title '{self._window_title}' "
            f"and keywords {self._window_keywords}. "
            f"Visible windows: {visible}"
        )
