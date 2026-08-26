"""AutoCAB package."""

from .cli import main
from .orchestrator import run_demo, run_pipeline

__version__ = "0.1.0"

__all__ = ["__version__", "main", "run_demo", "run_pipeline"]
