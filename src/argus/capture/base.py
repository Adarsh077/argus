"""Abstract Capturer interface.

Every capturer performs one capture and writes exactly one record to the
database. Phase 1 capturers are stubs; later phases replace the internals
without changing this interface or the daemon's loop logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from argus.config import Config
from argus.db import Database


class Capturer(ABC):
    """One capture source (window metadata, screen, camera, ...)."""

    name: str = "capturer"

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    @abstractmethod
    def capture(self) -> None:
        """Perform a single capture and write a row to the database."""
        raise NotImplementedError
