#!/usr/bin/env python3
"""Compatibility entry point for the packaged AutoCAB skill validator."""

from autocab.forge.package_validator import *  # noqa: F403
from autocab.forge.package_validator import main


if __name__ == "__main__":
    raise SystemExit(main())
