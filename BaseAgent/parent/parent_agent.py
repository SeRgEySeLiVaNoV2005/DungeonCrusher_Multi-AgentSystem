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

        # 2. Capture a fresh screenshot.
        try:
            screenshot = self._capturer.capture()
        except Exception:
            logger.exception("[Cmd] Failed to capture screenshot")
            return

        # 3. Match the template.
        match = self._matcher.find(screenshot, record.id)
        if match is None:
            logger.warning(
                f"[Cmd] Template '{record.id}' not found on screen. "
                f"Confidence threshold: {self._matcher._confidence}"
            )
            return

        # 4. Convert match coords to screen coords and click.
        region = self._capturer.window_region
        if region:
            screen_x = region["left"] + match.center[0]
            screen_y = region["top"] + match.center[1]
        else:
            screen_x, screen_y = match.center

        logger.info(
            f"[Cmd] Clicking '{record.name}' at screen ({screen_x}, {screen_y}) "
            f"(confidence: {match.confidence:.2f})"
        )

        try:
            self._emulator.click(screen_x, screen_y)
        except Exception:
            logger.exception("[Cmd] Click failed")

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
