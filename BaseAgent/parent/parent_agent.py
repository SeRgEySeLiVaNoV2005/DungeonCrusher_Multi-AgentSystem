"""Parent agent — the central coordinator of the multi-agent system.

The parent agent owns the main loop:

.. code-block:: text

    capture → vision → broadcast state → collect actions → execute → repeat
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from base.base_agent import BaseAgent
from src.capture.window_capturer import WindowCapturer
from src.communication.message_bus import Message, MessageBus, MessageType
from src.core.config import Settings
from src.core.logger import get_logger
from src.game_state.state import GameState, StateTracker
from src.input.emulator import Action, ClickAction, InputEmulator, MouseButton
from src.vision.ocr import OCREngine
from src.vision.template_matcher import MatchResult, TemplateMatcher

logger = get_logger(__name__)


class ParentAgent(BaseAgent):
    """The orchestrator agent that drives the perception–action loop.

    Responsibilities:

    1. Capture screen frames from the VK Play game window.
    2. Run the computer vision pipeline (template matching + OCR).
    3. Build a :class:`GameState` and broadcast it to all child agents.
    4. Collect action requests from children.
    5. Execute approved actions via :class:`InputEmulator`.

    The parent runs the main loop on the calling thread (``start()`` is
    synchronous by design — it blocks until ``stop()`` is called from another
    thread).
    """

    def __init__(
        self,
        name: str,
        bus: MessageBus,
        settings: Settings,
        ui_element_db=None,
    ) -> None:
        super().__init__(name, bus)
        self._settings = settings
        self._running = False

        # Subsystems — created on start.
        self._capturer: Optional[WindowCapturer] = None
        self._matcher: Optional[TemplateMatcher] = None
        self._ocr: Optional[OCREngine] = None
        self._emulator: Optional[InputEmulator] = None
        self._tracker: StateTracker = StateTracker()
        self._ui_element_db = ui_element_db  # For command handling.

        # Action queue — populated by child agents.
        self._pending_actions: List[Action] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Initialize subsystems and start the main loop."""
        # Subsystems.
        self._capturer = WindowCapturer(
            window_title=self._settings.game.window_title,
            window_keywords=self._settings.game.window_keywords,
            monitor=self._settings.capture.monitor,
            target_fps=self._settings.capture.target_fps,
        )
        self._matcher = TemplateMatcher(
            confidence=self._settings.vision.match_confidence,
        )
        self._ocr = OCREngine(lang=self._settings.vision.ocr_lang)
        self._emulator = InputEmulator(
            action_delay=self._settings.input.action_delay,
        )

        # Subscribe to action requests from child agents.
        self.bus.add_subscriber(
            self._on_action_request, MessageType.AGENT_ACTION_REQUEST
        )

        # Subscribe to user console commands.
        self.bus.add_subscriber(
            self._on_user_command, MessageType.USER_COMMAND
        )

        # Broadcast system start.
        self.publish(MessageType.SYSTEM_START)

        # Pre-load templates.
        try:
            count = self._matcher.load_templates()
            logger.info(f"Loaded {count} templates")
        except Exception:
            logger.warning("No templates loaded (resources/templates/ may be empty)")

        # Main loop.
        self._main_loop()

    def on_stop(self) -> None:
        """Shutdown subsystems."""
        self._running = False
        self.publish(MessageType.SYSTEM_STOP)
        self.bus.remove_subscriber(
            self._on_action_request, MessageType.AGENT_ACTION_REQUEST
        )
        logger.info("Parent agent subsystems shut down")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _main_loop(self) -> None:
        """The core perception–action cycle."""
        self._running = True

        # Locate the game window once.
        try:
            self._capturer.locate_window()
            logger.info(f"Game window located: {self._capturer.window_region}")
        except Exception:
            logger.error("Game window not found. Parent will retry each frame.")

        while self._running:
            frame_start = time.perf_counter()

            # --- 1. Capture ---
            try:
                screenshot = self._capturer.capture()
            except Exception:
                logger.debug("Capture failed; retrying next frame")
                time.sleep(0.5)
                continue

            # --- 2. Vision ---
            matches: List[MatchResult] = []
            ocr_texts: Dict[str, str] = {}

            try:
                matches = self._matcher.find_all(screenshot)
            except Exception:
                logger.debug("Template matching skipped (no templates?)")

            # --- 3. Build state ---
            ui_map: Dict[str, List[MatchResult]] = {}
            for m in matches:
                ui_map.setdefault(m.name, []).append(m)

            state = GameState(
                screenshot=screenshot,
                ui_elements=ui_map,
                ocr_texts=ocr_texts,
                metadata={
                    "frame_ms": time.perf_counter() - frame_start,
                    "window_region": self._capturer.window_region,
                },
            )
            self._tracker.push(state)

            # --- 4. Broadcast ---
            self.publish(MessageType.FRAME_CAPTURED, payload=state)

            # --- 5. Execute pending actions ---
            self._drain_actions()

    # ------------------------------------------------------------------
    # Action handling
    # ------------------------------------------------------------------

    def _on_action_request(self, message: Message) -> None:
        """Receive an action request from a child agent."""
        if message.payload is not None:
            self._pending_actions.append(message.payload)
            logger.debug(
                f"Received action from '{message.source}': {type(message.payload).__name__}"
            )

    # ------------------------------------------------------------------
    # User commands (console)
    # ------------------------------------------------------------------

    def _on_user_command(self, message: Message) -> None:
        """Handle a console command from the user.

        Payload is a string — the name or id of a UI element to find and click.
        """
        if self._matcher is None or self._emulator is None:
            logger.warning("[Cmd] Cannot execute — subsystems not ready")
            return

        target = message.payload
        if not target or not isinstance(target, str):
            return

        target = target.strip()
        logger.info(f"[Cmd] Looking for '{target}'...")

        # 1. Look up in the UI element database.
        record = None
        if self._ui_element_db is not None:
            record = self._ui_element_db.get(target)
            if record is None:
                # Try to find by name (case-insensitive).
                for el in self._ui_element_db.list_all():
                    if el.name.lower() == target.lower():
                        record = el
                        break
            if record is None:
                # Try to find by id (transliterated from Russian).
                for el in self._ui_element_db.list_all():
                    if el.id.lower() == target.lower():
                        record = el
                        break

        if record is None:
            logger.warning(
                f"[Cmd] Element '{target}' not found in database. "
                f"Available: {self._db_names()}"
            )
            return

        logger.info(
            f"[Cmd] Found '{record.name}' (id={record.id}) — searching on screen..."
        )

        # 2. Force game window on top for a split-second, capture, then restore.
        self._capturer.force_on_top()
        try:
            screenshot = self._capturer._capture_via_mss()
        except Exception:
            screenshot = None
        self._capturer.restore_z_order()

        if screenshot is None:
            logger.warning("[Cmd] Failed to capture screenshot")
            return

        # 3. Match the template.
        match = self._matcher.find(screenshot, record.id)
        if match is None:
            logger.warning(
                f"[Cmd] Template '{record.id}' not found on screen. "
                f"Confidence threshold: {self._matcher._confidence}"
            )
            # Save a debug image anyway so the user can see what's on screen.
            self._save_debug_image(screenshot, None, record.name, record.id)
            return

        # 4. Draw a red highlight around the match and save as debug image.
        self._save_debug_image(screenshot, match, record.name, record.id)

        region = self._capturer.window_region
        if region:
            screen_x = region["left"] + match.center[0]
            screen_y = region["top"] + match.center[1]
        else:
            screen_x, screen_y = match.center

        logger.info(
            f"[Cmd] Found '{record.name}' at window ({match.center[0]}, {match.center[1]}), "
            f"screen ({screen_x}, {screen_y}), "
            f"confidence={match.confidence:.2f}. "
            f"Debug image saved — check http://localhost:8765"
        )

    def _save_debug_image(
        self,
        screenshot: "np.ndarray",
        match,
        name: str,
        element_id: str,
    ) -> None:
        """Save a copy of the screenshot with an optional red highlight rect.

        Args:
            screenshot: BGR screenshot.
            match: :class:`MatchResult` or ``None``.
            name: Human-readable element name (for the label).
            element_id: Element id (for the filename).
        """
        import cv2
        from pathlib import Path

        image = screenshot.copy()

        if match is not None:
            left, top, w, h = match.bounds
            # Red rectangle — 3 px thick.
            cv2.rectangle(image, (left, top), (left + w, top + h), (0, 0, 255), 3)
            # Label above the rectangle.
            label = f"{name} ({match.confidence:.2f})"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
            label_y = top - 8 if top > th + 8 else top + h + th + 8
            cv2.rectangle(
                image,
                (left, label_y - th - 4),
                (left + tw + 4, label_y + 2),
                (0, 0, 255),
                -1,
            )
            cv2.putText(
                image, label, (left + 2, label_y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
            )

        # Save to a fixed path so the web server can serve it.
        debug_dir = Path("resources")
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / "debug_preview.png"
        cv2.imwrite(str(debug_path), image)
        logger.debug(f"[Cmd] Debug image saved to {debug_path}")

    def _db_names(self) -> str:
        """Return a comma-separated list of DB element names for hints."""
        if self._ui_element_db is None:
            return "(no database)"
        names = [el.name for el in self._ui_element_db.list_all()]
        return ", ".join(names[:10]) + ("..." if len(names) > 10 else "")

    # ------------------------------------------------------------------
    # Action handling
    # ------------------------------------------------------------------

    def _drain_actions(self) -> None:
        """Execute all queued actions."""
        if not self._pending_actions or self._emulator is None:
            return

        actions = self._pending_actions
        self._pending_actions = []

        for action in actions:
            try:
                if isinstance(action, list):
                    # Child sent multiple actions — execute as a sequence.
                    self._emulator.execute(*action)
                else:
                    # Single Action object.
                    self._emulator.execute(action)
            except Exception:
                logger.exception("Failed to execute action")
