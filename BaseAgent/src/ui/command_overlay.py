"""Command overlay — a small always-on-top window for typing button-click commands.

Positioned at the top of the game window so the user can write a button name
and press Enter to click it — without alt-tabbing to the terminal.

Flow::

    User types "настройки" → Enter → USER_COMMAND → ParentAgent → click
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any, Callable, Optional, Tuple

from ..core.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OVERLAY_WIDTH = 350
OVERLAY_HEIGHT = 30
OVERLAY_ALPHA = 0.85            # Semi-transparent when unfocused.
OVERLAY_ALPHA_FOCUSED = 1.0     # Fully opaque when typing.
BG_COLOR = "#0d0d1a"            # Very dark blue-black.
FG_COLOR = "#c0c0c0"            # Light grey text.
ENTRY_BG = "#1a1a2e"            # Slightly lighter entry background.
ACCENT_COLOR = "#e94560"        # Red accent for the arrow label.
FONT_FAMILY = "Consolas"
FONT_SIZE = 11
AUTO_HIDE_DELAY = 4.0           # Seconds to keep result message visible.
HOTKEY_ID = 1
HOTKEY_MOD = 0x0002 | 0x0004    # MOD_CONTROL | MOD_SHIFT
HOTKEY_KEY = 0x4A               # 'J'


class CommandOverlay:
    """A tkinter-based floating command bar that stays on top of the game.

    The overlay is a frameless, semi-transparent window positioned at the
    top edge of the game window. It publishes ``USER_COMMAND`` messages
    on Enter and displays results from ``COMMAND_RESULT`` messages.

    Uses Win32 ``RegisterHotKey`` for a global Ctrl+Shift+J shortcut that
    focuses the input field from anywhere (including inside the game).
    """

    def __init__(
        self,
        bus: Any,               # MessageBus (avoid circular import).
        game_window_region: Optional[dict] = None,
    ) -> None:
        """
        Args:
            bus: Shared message bus for inter-agent communication.
            game_window_region: ``{left, top, width, height}`` of the game
                                window. Used to position the overlay.
        """
        self._bus = bus
        self._game_region = game_window_region or {}
        self._root: Any = None
        self._entry: Any = None
        self._label: Any = None
        self._running = False
        self._hotkey_registered = False
        self._hide_timer_id: Optional[str] = None
        self._on_shutdown: Optional[Callable[[], None]] = None

        # Subscribe to command results so we can show feedback.
        from ..communication.message_bus import MessageType
        self._bus.add_subscriber(self._on_command_result, MessageType.COMMAND_RESULT)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the overlay window. Blocks the calling thread (mainloop)."""
        import tkinter as tk

        self._root = tk.Tk()
        self._root.title("DC Commander")
        self._running = True

        # --- Window style ---
        self._root.overrideredirect(True)          # No title bar / border.
        self._root.attributes("-topmost", True)      # Always on top.
        self._root.attributes("-alpha", OVERLAY_ALPHA)
        self._root.configure(bg=BG_COLOR)

        # Prevent the overlay from appearing in the taskbar.
        try:
            hwnd = ctypes.windll.user32.GetParent(self._root.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style |= WS_EX_TOOLWINDOW
            style |= WS_EX_NOACTIVATE
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass  # Non-critical — window will just appear in taskbar.

        # --- Position ---
        self._set_position()

        # --- Widgets ---
        # A thin frame to hold everything.
        frame = tk.Frame(self._root, bg=BG_COLOR, bd=0)
        frame.pack(fill="both", expand=True, padx=1, pady=1)

        # Arrow label "→".
        self._label = tk.Label(
            frame,
            text="→",
            bg=BG_COLOR,
            fg=ACCENT_COLOR,
            font=(FONT_FAMILY, FONT_SIZE, "bold"),
            padx=6,
        )
        self._label.pack(side="left")

        # Text entry.
        self._entry = tk.Entry(
            frame,
            bg=ENTRY_BG,
            fg=FG_COLOR,
            insertbackground=FG_COLOR,
            font=(FONT_FAMILY, FONT_SIZE),
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        self._entry.pack(side="left", fill="both", expand=True)
        self._entry.bind("<Return>", self._on_submit)
        self._entry.bind("<Escape>", self._on_escape)
        self._entry.bind("<FocusIn>", self._on_focus_in)
        self._entry.bind("<FocusOut>", self._on_focus_out)

        # Hint label (right side).
        self._hint = tk.Label(
            frame,
            text="Ctrl+Shift+J",
            bg=BG_COLOR,
            fg="#555555",
            font=(FONT_FAMILY, 8),
            padx=6,
        )
        self._hint.pack(side="right")

        # --- Hotkey ---
        self._register_hotkey()

        # --- Bind close ---
        self._root.protocol("WM_DELETE_WINDOW", self.stop)

        # --- Periodic position sync ---
        self._root.after(2000, self._sync_position_loop)

        # Block.
        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        """Close the overlay window and clean up."""
        self._running = False
        self._unregister_hotkey()

        if self._root is not None:
            try:
                self._root.destroy()
            except Exception:
                pass
            self._root = None

        if self._on_shutdown is not None:
            self._on_shutdown()

        logger.info("Command overlay stopped")

    def set_on_shutdown(self, callback: Callable[[], None]) -> None:
        """Register a callback that runs when the overlay is closed.

        Use this to trigger full system shutdown from the launcher.
        """
        self._on_shutdown = callback

    # ------------------------------------------------------------------
    # Hotkey (Win32 RegisterHotKey)
    # ------------------------------------------------------------------

    def _register_hotkey(self) -> None:
        """Register Ctrl+Shift+J as a global hotkey."""
        if self._hotkey_registered:
            return
        try:
            hwnd = self._get_hwnd()
            if hwnd is None:
                return

            user32 = ctypes.windll.user32

            # We need to pump WM_HOTKEY messages on the tkinter thread.
            # Register the hotkey with the overlay's HWND.
            result = user32.RegisterHotKey(hwnd, HOTKEY_ID, HOTKEY_MOD, HOTKEY_KEY)
            if result == 0:
                logger.warning(
                    "Could not register Ctrl+Shift+J hotkey — "
                    "it may be in use by another program"
                )
                return

            self._hotkey_registered = True

            # Install a custom message filter via tkinter's createfilehandler
            # (not available on Windows). Instead, poll via tkinter's after().
            self._poll_hotkey()

            logger.info("Hotkey registered: Ctrl+Shift+J → focus overlay")
        except Exception:
            logger.debug("Hotkey registration failed", exc_info=True)

    def _unregister_hotkey(self) -> None:
        """Release the global hotkey."""
        if not self._hotkey_registered:
            return
        try:
            hwnd = self._get_hwnd()
            if hwnd is not None:
                ctypes.windll.user32.UnregisterHotKey(hwnd, HOTKEY_ID)
        except Exception:
            pass
        self._hotkey_registered = False

    def _poll_hotkey(self) -> None:
        """Periodically check for WM_HOTKEY via PeekMessage (polled by tk after)."""
        if not self._running or not self._hotkey_registered:
            return

        try:
            import tkinter as tk

            user32 = ctypes.windll.user32
            WM_HOTKEY = 0x0312

            # PeekMessage to pull WM_HOTKEY out of the queue.
            PM_REMOVE = 0x0001
            msg = (ctypes.c_int * 7)()  # Small buffer for MSG struct.

            while user32.PeekMessageW(
                ctypes.cast(msg, ctypes.c_void_p),
                self._get_hwnd(),
                WM_HOTKEY, WM_HOTKEY,
                PM_REMOVE,
            ):
                # Hotkey pressed — focus the entry.
                try:
                    self._root.deiconify()
                    self._root.lift()
                    self._root.focus_force()
                    self._entry.focus_set()
                    self._entry.selection_range(0, "end")
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if self._running:
                self._root.after(200, self._poll_hotkey)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_submit(self, event: Any) -> None:
        """Enter pressed — send the command."""
        cmd = self._entry.get().strip()
        self._entry.delete(0, "end")
        if cmd:
            self._send_command(cmd)

    def _on_escape(self, event: Any) -> None:
        """Escape pressed — clear and return focus to game."""
        self._entry.delete(0, "end")
        # Return focus to the game window.
        try:
            hwnd = self._get_hwnd()
            if hwnd:
                # Minimize then restore the overlay to lose focus gracefully.
                pass
        except Exception:
            pass
        # Blur the entry.
        self._root.focus_set()
        # Actually, focus the game window.
        try:
            from ..capture.window_capturer import WindowCapturer
            # We don't have easy access to the capturer here, so just
            # defocus by setting focus to the root (invisible).
            pass
        except Exception:
            pass

    def _on_focus_in(self, event: Any) -> None:
        """Entry gained focus — make overlay fully opaque."""
        try:
            self._root.attributes("-alpha", OVERLAY_ALPHA_FOCUSED)
        except Exception:
            pass

    def _on_focus_out(self, event: Any) -> None:
        """Entry lost focus — make overlay semi-transparent."""
        try:
            self._root.attributes("-alpha", OVERLAY_ALPHA)
        except Exception:
            pass

    def _on_command_result(self, message: Any) -> None:
        """Receive feedback from ParentAgent about the command."""
        if self._root is None:
            return

        payload = message.payload or {}
        success = payload.get("success", False)
        name = payload.get("name", "")

        def _update_ui() -> None:
            if self._entry is None:
                return
            if success:
                self._flash_label(f"✓ {name}", "#4ecca3")   # Green.
            else:
                self._flash_label(f"✗ {name}", ACCENT_COLOR)  # Red.

        try:
            self._root.after(0, _update_ui)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_command(self, text: str) -> None:
        """Publish a USER_COMMAND message."""
        from ..communication.message_bus import Message, MessageType

        self._bus.publish(Message(
            type=MessageType.USER_COMMAND,
            source="overlay",
            payload=text,
        ))
        logger.info(f"[Overlay] Command sent: '{text}'")

    def _flash_label(self, text: str, color: str) -> None:
        """Briefly change the arrow label to show a result."""
        if self._label is None or self._root is None:
            return

        self._label.configure(text=text, fg=color)

        # Cancel any existing hide timer.
        if self._hide_timer_id is not None:
            try:
                self._root.after_cancel(self._hide_timer_id)
            except Exception:
                pass

        def _restore() -> None:
            try:
                if self._label is not None:
                    self._label.configure(text="→", fg=ACCENT_COLOR)
            except Exception:
                pass
            self._hide_timer_id = None

        self._hide_timer_id = self._root.after(
            int(AUTO_HIDE_DELAY * 1000), _restore
        )

    def _set_position(self) -> None:
        """Position the overlay at the top-center of the game window."""
        if self._root is None:
            return

        left = self._game_region.get("left", 100)
        top = self._game_region.get("top", 100)
        width = self._game_region.get("width", 800)

        x = left + max((width - OVERLAY_WIDTH) // 2, 0)
        y = top  # Top edge of the game window.

        self._root.geometry(f"{OVERLAY_WIDTH}x{OVERLAY_HEIGHT}+{x}+{y}")

    def _sync_position_loop(self) -> None:
        """Periodically re-sync position in case the game window moved."""
        if not self._running or self._root is None:
            return
        self._set_position()
        self._root.after(5000, self._sync_position_loop)

    def _get_hwnd(self) -> Optional[int]:
        """Return the Win32 HWND of the overlay window, or None."""
        if self._root is None:
            return None
        try:
            import tkinter as tk
            frame_id = self._root.winfo_id()
            # winfo_id returns the X11/XCB window ID on Linux, but on Windows
            # it's actually the HWND of the frame. We still need to go through
            # GetParent to get the actual top-level window.
            return ctypes.windll.user32.GetParent(frame_id)
        except Exception:
            return None
