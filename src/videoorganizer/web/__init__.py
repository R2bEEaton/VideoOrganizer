"""Local web browser for a media library."""

from .server import create_app, run  # noqa: F401

__all__ = ["create_app", "run"]
