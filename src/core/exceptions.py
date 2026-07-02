"""Custom exception hierarchy for the multi-agent system."""


class DungeonCrusherError(Exception):
    """Base exception for all project-specific errors."""


class CaptureError(DungeonCrusherError):
    """Raised when screen capture fails (window not found, MSS error, etc.)."""


class InputError(DungeonCrusherError):
    """Raised when input emulation fails (permission denied, invalid coordinates, etc.)."""


class VisionError(DungeonCrusherError):
    """Raised when computer vision processing fails (template not found, OCR error, etc.)."""


class AgentError(DungeonCrusherError):
    """Base exception for agent-related errors."""


class AgentLifecycleError(AgentError):
    """Raised when agent lifecycle transitions are invalid (e.g., start before stop)."""


class AgentDecisionError(AgentError):
    """Raised when an agent fails to produce a valid decision."""


class CommunicationError(DungeonCrusherError):
    """Raised when the message bus encounters an error."""


class ConfigError(DungeonCrusherError):
    """Raised when configuration is missing or invalid."""


class GameStateError(DungeonCrusherError):
    """Raised when game state is inconsistent or unreachable."""
