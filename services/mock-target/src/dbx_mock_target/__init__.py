"""Mock destination HRMS: a real service the migration talks to over HTTP."""
from .main import app
from .store import Store

__all__ = ["Store", "app"]
