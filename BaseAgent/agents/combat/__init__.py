"""CombatAgent — autonomous combat engagement.

Detects enemies on screen, activates abilities, and fights until
combat is resolved.
"""

from .combat_agent import CombatAgent, CombatState

__all__ = ["CombatAgent", "CombatState"]
