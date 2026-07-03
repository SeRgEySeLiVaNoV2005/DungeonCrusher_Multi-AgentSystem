"""Configuration loader — reads settings from a YAML file into typed objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .exceptions import ConfigError


# ---------------------------------------------------------------------------
# Typed configuration blocks
# ---------------------------------------------------------------------------

@dataclass
class GameConfig:
    """Settings for locating the game window."""

    window_title: str = "VK Play"
    window_keywords: List[str] = field(
        default_factory=lambda: [
            "Dungeon", "Crusher", "Soul Hunters",
            "Крушители", "Подземелий", "VK Play",
        ]
    )


@dataclass
class CaptureConfig:
    """Settings for screen capture."""

    target_fps: int = 10
    monitor: int = 0


@dataclass
class InputConfig:
    """Settings for mouse / keyboard emulation."""

    action_delay: float = 0.05
    double_click_interval: float = 0.3


@dataclass
class VisionConfig:
    """Settings for computer vision."""

    templates_dir: str = "resources/templates"
    """Directory containing template PNG images for TemplateMatcher."""

    match_confidence: float = 0.8
    ocr_lang: str = "eng"


@dataclass
class AgentConfig:
    """Settings for agent behaviour."""

    max_children: int = 8
    decision_timeout: float = 1.0


@dataclass
class TooltipReaderConfig:
    """Settings for the TooltipReaderAgent (CTRL+H OCR tool)."""

    region_width: int = 320
    """Width of the capture region around the cursor."""

    region_height: int = 90
    """Height of the capture region around the cursor."""

    hover_delay: float = 0.0
    """Not used in manual mode; reserved for future auto-hover."""

    ocr_lang: str = "rus+eng"
    """Tesseract language code."""

    scale: float = 2.0
    """Upscale factor for OCR (1.0 = no scaling)."""

    invert: bool = True
    """Invert colors for light-on-dark game tooltips."""


@dataclass
class WebReviewConfig:
    """Settings for the local web review interface."""

    host: str = "127.0.0.1"
    """Server bind address."""

    port: int = 8765
    """Server port."""

    db_path: str = "resources/ui_elements_db.json"
    """Path to the UI element database JSON file."""


@dataclass
class LoggingConfig:
    """Settings for logging."""

    level: str = "INFO"
    format: str = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    file: str = "logs/system.log"


@dataclass
class Settings:
    """Top-level configuration aggregating all blocks."""

    game: GameConfig = field(default_factory=GameConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    input: InputConfig = field(default_factory=InputConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    agents: AgentConfig = field(default_factory=AgentConfig)
    tooltip_reader: TooltipReaderConfig = field(default_factory=TooltipReaderConfig)
    web_review: WebReviewConfig = field(default_factory=WebReviewConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _nested_get(data: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dicts."""
    for key in keys:
        if not isinstance(data, dict):
            return default
        data = data.get(key, {})
    return data if data != {} else default


def load_config(path: Optional[str] = None) -> Settings:
    """Load configuration from a YAML file, falling back to defaults.

    Args:
        path: Absolute or relative path to a ``settings.yaml`` file.
              If omitted, looks for ``config/settings.yaml`` relative to the
              project root (two levels up from this module).

    Returns:
        A fully-populated :class:`Settings` object.

    Raises:
        ConfigError: If the file exists but cannot be parsed.
    """
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            "PyYAML is required to load config files. Install it with: "
            "pip install PyYAML"
        ) from exc

    if path is None:
        # Default: config/settings.yaml relative to project root.
        # This file lives in src/core/config.py, so go up 2 levels.
        project_root = Path(__file__).resolve().parent.parent.parent
        path = str(project_root / "config" / "settings.yaml")

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        try:
            raw: Dict[str, Any] = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML config: {exc}") from exc

    if raw is None:
        raw = {}

    # Build each block, merging file data over defaults.
    game_raw = raw.get("game", {})
    capture_raw = raw.get("capture", {})
    input_raw = raw.get("input", {})
    vision_raw = raw.get("vision", {})
    agents_raw = raw.get("agents", {})
    tooltip_raw = raw.get("tooltip_reader", {})
    web_review_raw = raw.get("web_review", {})
    logging_raw = raw.get("logging", {})

    return Settings(
        game=GameConfig(
            window_title=game_raw.get("window_title", "VK Play"),
            window_keywords=game_raw.get(
                "window_keywords", ["Dungeon", "Crusher", "Soul Hunters"]
            ),
        ),
        capture=CaptureConfig(
            target_fps=capture_raw.get("target_fps", 10),
            monitor=capture_raw.get("monitor", 0),
        ),
        input=InputConfig(
            action_delay=input_raw.get("action_delay", 0.05),
            double_click_interval=input_raw.get("double_click_interval", 0.3),
        ),
        vision=VisionConfig(
            templates_dir=vision_raw.get("templates_dir", "resources/templates"),
            match_confidence=vision_raw.get("match_confidence", 0.8),
            ocr_lang=vision_raw.get("ocr_lang", "eng"),
        ),
        agents=AgentConfig(
            max_children=agents_raw.get("max_children", 8),
            decision_timeout=agents_raw.get("decision_timeout", 1.0),
        ),
        logging=LoggingConfig(
            level=logging_raw.get("level", "INFO"),
            format=logging_raw.get(
                "format",
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            ),
            file=logging_raw.get("file", "logs/system.log"),
        ),
        tooltip_reader=TooltipReaderConfig(
            region_width=tooltip_raw.get("region_width", 320),
            region_height=tooltip_raw.get("region_height", 90),
            hover_delay=tooltip_raw.get("hover_delay", 0.0),
            ocr_lang=tooltip_raw.get("ocr_lang", "eng"),
            scale=tooltip_raw.get("scale", 2.0),
            invert=tooltip_raw.get("invert", True),
        ),
        web_review=WebReviewConfig(
            host=web_review_raw.get("host", "127.0.0.1"),
            port=web_review_raw.get("port", 8765),
            db_path=web_review_raw.get("db_path", "resources/ui_elements_db.json"),
        ),
    )
