"""Configuration contracts for the disclosure search service."""

from .defaults import DEFAULT_SETTINGS, SCHEMA_VERSION
from .settings import Settings, load_settings

__all__ = ["DEFAULT_SETTINGS", "SCHEMA_VERSION", "Settings", "load_settings"]
