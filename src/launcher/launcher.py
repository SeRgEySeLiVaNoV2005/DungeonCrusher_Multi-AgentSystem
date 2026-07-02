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
from typing import List, Optional

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

    def bootstrap(self, dry_run: bool = False) -> None:
        """Create the parent agent and wire everything together.

        Args:
            dry_run: If True, skip game window connection (testing mode).
        """
        logger.info("══════════════════════════════════════")
        logger.info("DungeonCrusher Multi-Agent System v0.1.0")
        logger.info("══════════════════════════════════════")

        if dry_run:
            logger.info("DRY-RUN mode — no game connection")

        # Create parent.
        self._parent = ParentAgent(
            name="parent",
            bus=self._bus,
            settings=self._settings,
        )

        # Optionally create stub children.
        self._create_default_children()

        logger.info(
            f"System ready: 1 parent + {len(self._children)} child agent(s)"
        )

    def start(self) -> None:
        """Start the parent agent (main loop). Blocks until interrupted."""
        if self._parent is None:
            raise DungeonCrusherError("Call bootstrap() before start()")

        # Start children.
        for child in self._children:
            child.start()

        # Start parent (synchronous — runs the main loop).
        try:
            self._parent.start()
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
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

        logger.info("All agents stopped. Goodbye!")

    def _create_default_children(self) -> None:
        """Hook to create stub child agents.

        Override or extend in the future to add domain-specific agents.
        """
        # Stub: a watcher agent that just logs frames.
        # Uncomment when you're ready to add real agents:
        # from watcher.watcher_agent import WatcherAgent
        # self._children.append(WatcherAgent("watcher", self._bus))
        pass


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
