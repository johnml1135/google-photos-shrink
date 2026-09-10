"""Safely shrink a Google Photos library while retaining originals."""

__version__ = "0.1.0"

from .config import ConfigError, Settings, load_config
from .state import StateError, StateStore

__all__ = ["ConfigError", "Settings", "StateError", "StateStore", "load_config"]
