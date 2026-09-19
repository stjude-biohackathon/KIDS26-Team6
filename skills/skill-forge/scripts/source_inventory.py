#!/usr/bin/env python3
"""Compatibility entry point for the packaged AutoCAB source inventory."""

from autocab.forge.source_inventory import *  # noqa: F403
from autocab.forge.source_inventory import main


if __name__ == "__main__":
    raise SystemExit(main())
