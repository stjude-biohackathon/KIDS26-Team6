#!/usr/bin/env python3
"""Compatibility entry point for packaged AutoCAB CBD configuration."""

from autocab.forge.cbd_config import *  # noqa: F403
from autocab.forge.cbd_config import main


if __name__ == "__main__":
    raise SystemExit(main())
