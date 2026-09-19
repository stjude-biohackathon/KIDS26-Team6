"""Run the AutoCAB recording command directly as a Python module."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
