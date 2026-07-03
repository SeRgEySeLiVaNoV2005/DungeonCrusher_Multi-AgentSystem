"""Allow ``python -m src.launcher``."""
from __future__ import annotations

import sys

from .launcher import main

if __name__ == "__main__":
    sys.exit(main())
