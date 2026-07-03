"""AutomaticLevelingHeroesAgent — autonomously levels up heroes.

Detects red level-up buttons in the Heroes tab and clicks them
when the user is idle and no other agents are working.
"""

from .automatic_leveling_heroes_agent import (
    AutomaticLevelingHeroesAgent,
    LevelingState,
)

__all__ = ["AutomaticLevelingHeroesAgent", "LevelingState"]
