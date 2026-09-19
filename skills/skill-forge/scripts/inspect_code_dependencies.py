#!/usr/bin/env python3
"""Compatibility entry point for packaged dependency inspection."""

from autocab.forge.dependency_inspector import *  # noqa: F403
from autocab.forge.dependency_inspector import main


if __name__ == "__main__":
    raise SystemExit(main())
