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

        Tries ``PrintWindow`` first (captures even when the window is behind
        other windows). Falls back to MSS screen capture if PrintWindow
        returns a blank frame (common with DirectX games).

        Returns:
            Screenshot as a ``numpy.ndarray`` in H×W×C (BGR) format.

        Raises:
            CaptureError: If the window was not located first.
        """
        if self._window_region is None:
            self.locate_window()

        self._throttle()

        # 1. Try PrintWindow (works when the game is behind other windows).
        if self._window_hwnd is not None:
            frame = self._capture_via_printwindow()
            if frame is not None and not self._is_blank_frame(frame):
                return frame
            if frame is not None:
                logger.debug("PrintWindow returned a blank frame — falling back to MSS")

        # 2. Fallback: MSS screen capture (requires window to be visible).
        return self._capture_via_mss()

    def _capture_via_printwindow(self) -> Optional["np.ndarray"]:
        """Capture the game window content via ``PrintWindow``.

        Works even when the window is minimised or behind other windows.
        Returns ``None`` if the call fails, or a blank frame if the game
        renders via DirectX (which PrintWindow cannot capture).
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            hwnd = self._window_hwnd
            region = self._window_region
            width = region["width"]
            height = region["height"]

            # Get the window DC.
            hdc_window = user32.GetDC(hwnd)
            if not hdc_window:
                logger.debug("PrintWindow: GetDC failed")
                return None

            # Create a compatible DC and bitmap.
            hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
            hbitmap = gdi32.CreateCompatibleBitmap(hdc_window, width, height)
            if not hdc_mem or not hbitmap:
                user32.ReleaseDC(hwnd, hdc_window)
                logger.debug("PrintWindow: CreateCompatibleDC/Bitmap failed")
                return None

            old_bmp = gdi32.SelectObject(hdc_mem, hbitmap)

            # PrintWindow — PW_RENDERFULLCONTENT = 2 (requires Windows 8.1+).
            PW_RENDERFULLCONTENT = 0x00000002
            result = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

            if result == 0:
                # Try without the flag (older Windows / fallback).
                result = user32.PrintWindow(hwnd, hdc_mem, 0)

            frame = None
            if result != 0:
                # Convert HBITMAP → numpy array.
                import numpy as np
                bmpinfo = ctypes.create_string_buffer(44)
                gdi32.GetDIBits(
                    hdc_mem, hbitmap, 0, height, None,
                    ctypes.cast(bmpinfo, ctypes.POINTER(wintypes.BITMAPINFO)),
                    0,  # DIB_RGB_COLORS
                )

                buf = ctypes.create_string_buffer(width * height * 4)
                gdi32.GetDIBits(
                    hdc_mem, hbitmap, 0, height, buf,
                    ctypes.cast(bmpinfo, ctypes.POINTER(wintypes.BITMAPINFO)),
                    0,
                )
                # BGRA → BGR (drop alpha).
                raw = np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4)
                frame = raw[:, :, :3].copy()

            # Cleanup.
            gdi32.SelectObject(hdc_mem, old_bmp)
            gdi32.DeleteObject(hbitmap)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(hwnd, hdc_window)

            if frame is not None:
                logger.debug(
                    f"PrintWindow captured {width}x{height} "
                    f"(mean brightness: {frame.mean():.0f})"
                )
            return frame
        except Exception:
            logger.debug("PrintWindow capture failed", exc_info=True)
            return None

    def _capture_via_mss(self) -> "np.ndarray":
        """Fallback: capture via MSS (requires window to be on-screen)."""
        try:
            import mss
            import numpy as np

            if self._sct is None:
                self._sct = mss.mss()

            frame = np.array(self._sct.grab(self._window_region))
            return frame[:, :, :3]  # BGRA → BGR
        except Exception as exc:
            logger.error(f"Screen capture failed: {exc}")
            raise CaptureError(f"Failed to capture screen: {exc}") from exc

    @staticmethod
    def _is_blank_frame(frame: "np.ndarray") -> bool:
        """Heuristic: detect if PrintWindow returned a blank/black frame.

        A completely black frame (mean < 5) or a uniform-color frame
        (std < 3) is treated as blank — PrintWindow cannot capture
        DirectX content.
        """
        import numpy as np
        mean = float(frame.mean())
        if mean < 5.0:
            return True  # Almost entirely black.
        if float(frame.std()) < 3.0:
            return True  # Single solid color.
        return False

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

    def hide_other_windows(self) -> list:
        """Hide all visible windows EXCEPT the game window.

        Returns a list of HWNDs that were hidden — pass to
        :meth:`show_windows` to restore them.

        Use this as a last resort when PrintWindow fails and the game
        must be captured via MSS while other windows (browser) are in front.
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hidden = []

        def _enum_handler(hwnd: int, _lparam: int) -> bool:
            if hwnd == self._window_hwnd:
                return True  # Skip the game window.
            if not user32.IsWindowVisible(hwnd):
                return True

            # Skip taskbar, desktop, etc.
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            # Skip tiny overlay windows.
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 200 or h < 150:
                return True

            # Hide it.
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
            hidden.append(hwnd)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        enum_proc = WNDENUMPROC(_enum_handler)
        user32.EnumWindows(enum_proc, 0)

        logger.debug(f"Hid {len(hidden)} windows for capture")
        return hidden

    def show_windows(self, hwnds: list) -> None:
        """Restore windows hidden by :meth:`hide_other_windows`."""
        import ctypes
        user32 = ctypes.windll.user32
        for hwnd in hwnds:
            try:
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
            except Exception:
                pass
        if hwnds:
            logger.debug(f"Restored {len(hwnds)} windows")

    def force_on_top(self) -> bool:
        """Temporarily make the game window topmost so it renders above
        all other windows — even when the browser has focus.

        Call :meth:`restore_z_order` after capturing.

        Returns:
            ``True`` on success.
        """
        if self._window_hwnd is None:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32

            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040

            region = self._window_region
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW

            user32.SetWindowPos(
                self._window_hwnd,
                HWND_TOPMOST,
                region["left"], region["top"],
                region["width"], region["height"],
                flags,
            )

            # Force the window and its children to repaint immediately.
            RDW_UPDATENOW = 0x0100
            RDW_ALLCHILDREN = 0x0080
            RDW_INVALIDATE = 0x0001
            user32.RedrawWindow(
                self._window_hwnd,
                None, None,
                RDW_UPDATENOW | RDW_ALLCHILDREN | RDW_INVALIDATE,
            )

            # Let DWM composite the frame.
            import time
            time.sleep(0.5)
            logger.debug("Game window set to TOPMOST for capture")
            return True
        except Exception:
            logger.debug("force_on_top failed", exc_info=True)
            return False

    def restore_z_order(self) -> bool:
        """Remove TOPMOST flag — window returns to normal Z-order."""
        if self._window_hwnd is None:
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32

            HWND_NOTOPMOST = -2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010

            user32.SetWindowPos(
                self._window_hwnd,
                HWND_NOTOPMOST,
                0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            logger.debug("Game window restored to normal Z-order")
            return True
        except Exception:
            logger.debug("restore_z_order failed", exc_info=True)
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
