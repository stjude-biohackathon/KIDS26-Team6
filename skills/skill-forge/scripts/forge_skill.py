#!/usr/bin/env python3
"""Compatibility entry point for the packaged AutoCAB forge renderer."""

from autocab.forge.renderer import *  # noqa: F403
from autocab.forge.renderer import main


if __name__ == "__main__":
    raise SystemExit(main())
