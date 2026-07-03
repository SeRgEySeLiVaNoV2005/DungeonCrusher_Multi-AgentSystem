"""CLI entry point for the multi-agent system.

Usage::

    python -m src.launcher          # Start with defaults
    python -m src.launcher --help   # Show options
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Any, List, Optional

from parent.parent_agent import ParentAgent
from base.child_agent import ChildAgent
from src.communication.message_bus import MessageBus
from ..core.config import Settings, load_config
from ..core.exceptions import DungeonCrusherError
from ..core.logger import get_logger, setup_logging

logger = get_logger(__name__)


def build_argparser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="dungeon-crusher-agents",
        description="Multi-agent automation system for Dungeon Crusher: Soul Hunters",
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to settings.yaml (default: config/settings.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override the log level from config",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Start without connecting to the game (test mode)",
    )
    return parser


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

class SystemLauncher:
    """Manages system startup, agent wiring, and graceful shutdown."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._bus = MessageBus()
        self._parent: Optional[ParentAgent] = None
        self._children: List[ChildAgent] = []

        # Shared infrastructure.
        self._pending_store: Any = None
        self._ui_element_db: Any = None
        self._web_server: Any = None
        self._template_matcher: Any = None
        self._capturer: Any = None

    def bootstrap(self, dry_run: bool = False) -> None:
        """Create the parent agent and wire everything together.

        Args:
            dry_run: If True, skip game window connection (testing mode).
        """
        logger.info("=" * 40)
        logger.info("DungeonCrusher Multi-Agent System v0.1.0")
        logger.info("=" * 40)

        if dry_run:
            logger.info("DRY-RUN mode — no game connection")

        # Shared infrastructure.
        self._setup_infrastructure()

        # Create parent.
        self._parent = ParentAgent(
            name="parent",
            bus=self._bus,
            settings=self._settings,
            ui_element_db=self._ui_element_db,
        )

        # Create child agents.
        self._create_default_children()

        logger.info(
            f"System ready: 1 parent + {len(self._children)} child agent(s)"
        )

    def start(self) -> None:
        """Start the parent agent (main loop) and the command overlay.

        The overlay runs on the **main thread** (tkinter mainloop).
        The parent agent runs on its own daemon thread.
        When the overlay is closed, the system shuts down.
        """
        if self._parent is None:
            raise DungeonCrusherError("Call bootstrap() before start()")

        # Start the web review server first.
        self._start_web_server()

        # Start children.
        for child in self._children:
            child.start()

        # Start parent (creates a daemon thread for the main loop).
        self._parent.start()

        # Start console command reader (stdin, daemon thread).
        self._start_console_reader()

        # Launch the command overlay on the MAIN thread.
        # Blocks with tkinter mainloop until the user closes it.
        self._start_overlay()

        # Overlay closed — shut down everything.
        self.shutdown()

    def shutdown(self) -> None:
        """Gracefully stop all agents and release resources."""
        logger.info("Shutting down...")

        # Stop children first.
        for child in reversed(self._children):
            try:
                child.stop(timeout=3.0)
            except Exception:
                logger.exception(f"Error stopping child '{child.name}'")

        # Stop parent last.
        if self._parent and self._parent.is_running:
            try:
                self._parent.stop(timeout=5.0)
            except Exception:
                logger.exception("Error stopping parent agent")

        # Stop web server.
        self._stop_web_server()

        logger.info("All agents stopped. Goodbye!")

    # ------------------------------------------------------------------
    # Infrastructure
    # ------------------------------------------------------------------

    def _setup_infrastructure(self) -> None:
        """Create shared infrastructure: template matcher, pending store, DB."""
        # Template matcher — shared across all agents.
        try:
            from src.vision.template_matcher import TemplateMatcher

            self._template_matcher = TemplateMatcher(
                templates_dir=self._settings.vision.templates_dir,
                confidence=self._settings.vision.match_confidence,
            )
            loaded = self._template_matcher.load_templates()
            logger.info(f"Template matcher initialized ({loaded} templates loaded)")
        except Exception:
            logger.exception("Failed to initialize template matcher")

        # Window capturer — shared for command handler in web server.
        try:
            from src.capture.window_capturer import WindowCapturer

            self._capturer = WindowCapturer(
                window_title=self._settings.game.window_title,
                window_keywords=self._settings.game.window_keywords,
                monitor=self._settings.capture.monitor,
                target_fps=self._settings.capture.target_fps,
            )
            self._capturer.locate_window()
            logger.info("Window capturer initialized for web commands")
        except Exception:
            logger.warning("Window capturer init failed — Find & Highlight won't work")

        # Pending store + UI element database.
        try:
            from tooltip_reader.pending_store import PendingElementStore
            from tooltip_reader.ui_element_db import UIElementDB

            self._pending_store = PendingElementStore()
            self._ui_element_db = UIElementDB(
                self._settings.web_review.db_path
            )
            logger.info("Infrastructure initialized (pending store + element DB)")
        except Exception:
            logger.exception("Failed to initialize infrastructure")

    def _start_web_server(self) -> None:
        """Launch the web review interface."""
        if self._pending_store is None or self._ui_element_db is None:
            return
        try:
            from tooltip_reader.web_review_server import WebReviewServer

            cfg = self._settings.web_review
            self._web_server = WebReviewServer(
                store=self._pending_store,
                db=self._ui_element_db,
                matcher=self._template_matcher,
                bus=self._bus,
                capturer=self._capturer,
                host=cfg.host,
                port=cfg.port,
            )
            self._web_server.start()
        except OSError:
            logger.warning(
                f"Could not start web server on port {self._settings.web_review.port} "
                f"— port may be in use"
            )
        except Exception:
            logger.exception("Failed to start web review server")

    def _start_overlay(self) -> None:
        """Launch the CommandOverlay on the main thread.

        The overlay's tkinter mainloop blocks the calling thread. When the
        user closes the overlay window, it triggers ``shutdown()`` via the
        ``set_on_shutdown`` callback.
        """
        # Build the game window region from the capturer (if available).
        game_region = None
        if self._capturer is not None and self._capturer.window_region is not None:
            game_region = self._capturer.window_region

        try:
            from src.ui.command_overlay import CommandOverlay

            self._overlay = CommandOverlay(
                bus=self._bus,
                game_window_region=game_region,
            )
            self._overlay.set_on_shutdown(lambda: None)  # shutdown() is called after start()

            logger.info("Command overlay starting — type a button name and press Enter")
            self._overlay.start()
        except Exception:
            logger.exception("Failed to start command overlay")

    def _start_console_reader(self) -> None:
        """Launch a background thread that reads commands from stdin.

        Type a UI element name (from the database) to find and click it.
        Type ``list`` to see available elements.
        Type ``exit`` to shut down.
        """
        import threading

        def _reader() -> None:
            logger.info(
                "Console ready. Type a button name to click, 'list' to see "
                "available elements, 'exit' to quit."
            )
            while self._parent is not None and self._parent.is_running:
                try:
                    line = input()
                except (EOFError, OSError):
                    break
                if not line:
                    continue

                cmd = line.strip()

                if cmd.lower() == "exit":
                    logger.info("Exit command received — shutting down")
                    self.shutdown()
                    break

                if cmd.lower() == "list":
                    self._print_db_list()
                    continue

                # Publish as a user command — ParentAgent handles it.
                from src.communication.message_bus import Message, MessageType
                self._bus.publish(Message(
                    type=MessageType.USER_COMMAND,
                    source="console",
                    payload=cmd,
                ))

        thread = threading.Thread(
            target=_reader,
            name="console-reader",
            daemon=True,
        )
        thread.start()

    def _print_db_list(self) -> None:
        """Print available UI elements to the console."""
        if self._ui_element_db is None:
            print("  (no database loaded)")
            return
        elements = self._ui_element_db.list_all()
        if not elements:
            print("  (database is empty — capture some elements with CTRL+H)")
            return
        print(f"  {len(elements)} element(s) in database:")
        for el in elements:
            tags = ", ".join(el.tags) if el.tags else "—"
            print(f"    [{el.id}] {el.name}  |  tags: {tags}")

    def _stop_web_server(self) -> None:
        """Shut down the web review server."""
        if self._web_server is not None:
            try:
                self._web_server.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Child agents
    # ------------------------------------------------------------------

    def _create_default_children(self) -> None:
        """Create default child agents on bootstrap."""
        self._create_tooltip_reader()
        # CombatAgent disabled for now — enable when templates are ready.
        # self._create_combat_agent()

    def _create_tooltip_reader(self) -> None:
        """Create the TooltipReaderAgent with config from settings."""
        if self._pending_store is None:
            logger.warning("No pending store; skipping TooltipReaderAgent")
            return

        cfg = self._settings.tooltip_reader
        try:
            from tooltip_reader.tooltip_reader_agent import TooltipReaderAgent

            agent = TooltipReaderAgent(
                name="tooltip_reader",
                bus=self._bus,
                pending_store=self._pending_store,
                domain="debug",
                region_width=cfg.region_width,
                region_height=cfg.region_height,
                ocr_lang=cfg.ocr_lang,
                scale=cfg.scale,
                invert=cfg.invert,
            )
            self._children.append(agent)
        except Exception:
            logger.exception("Failed to create TooltipReaderAgent")

    def _create_combat_agent(self) -> None:
        """Create the CombatAgent if templates are available."""
        if self._template_matcher is None:
            logger.warning("No template matcher; skipping CombatAgent")
            return

        try:
            from agents.combat import CombatAgent

            agent = CombatAgent(
                name="combat_01",
                bus=self._bus,
                matcher=self._template_matcher,
                abilities=["1", "2", "3"],
                ability_cooldown=1.5,
            )
            self._children.append(agent)
            logger.info("CombatAgent created (will auto-detect combat via templates)")
        except Exception:
            logger.exception("Failed to create CombatAgent")


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Returns 0 on success, 1 on error."""
    parser = build_argparser()
    args = parser.parse_args(argv)

    # Load config.
    try:
        settings = load_config(args.config)
    except Exception as exc:
        print(f"ERROR: Failed to load config: {exc}", file=sys.stderr)
        return 1

    # Setup logging.
    log_level = args.log_level or settings.logging.level
    setup_logging(
        log_level=log_level,
        log_format=settings.logging.format,
        log_file=settings.logging.file,
    )

    # Bootstrap and run.
    launcher = SystemLauncher(settings)

    try:
        launcher.bootstrap(dry_run=args.dry_run)
    except Exception as exc:
        logger.exception("Bootstrap failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Handle SIGINT / SIGTERM gracefully.
    signal.signal(signal.SIGINT, lambda s, f: launcher.shutdown())
    signal.signal(signal.SIGTERM, lambda s, f: launcher.shutdown())

    try:
        launcher.start()
    except Exception as exc:
        logger.exception("Runtime error")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
