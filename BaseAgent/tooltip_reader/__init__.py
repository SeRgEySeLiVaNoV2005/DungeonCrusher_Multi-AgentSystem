"""Tooltip Reader — OCR debug tools for manual UI element capture.

Press CTRL+H in-game, then review and save elements at http://localhost:8765.
"""

from tooltip_reader.pending_store import PendingElement, PendingElementStore
from tooltip_reader.tooltip_reader_agent import TooltipReaderAgent
from tooltip_reader.ui_element_db import UIElementDB, UIElementRecord
from tooltip_reader.web_review_server import WebReviewServer

__all__ = [
    "TooltipReaderAgent",
    "UIElementDB",
    "UIElementRecord",
    "PendingElementStore",
    "PendingElement",
    "WebReviewServer",
]
