"""Thread-safe, durable publication of controller configuration."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import config as cfgmod
from .config import Config


class ConfigStore:
    """Own the persisted and live configuration as one transaction boundary.

    A candidate is cloned, mutated, validated and durably saved before it is
    published to the control loop.  The lock deliberately covers publication,
    so concurrent API requests cannot be reordered after persistence.
    """

    def __init__(
        self,
        path: Path,
        initial: Config,
        *,
        save_fn: Callable[[Config, Path], None] = cfgmod.save,
        publish_fn: Optional[Callable[[Config], None]] = None,
        warning: Optional[str] = None,
    ) -> None:
        self._path = Path(path)
        self._config = cfgmod.clone(initial)
        self._save_fn = save_fn
        self._publish_fn = publish_fn
        self._warning = warning
        self._revision = 0
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> Tuple[Config, int, Optional[str]]:
        with self._lock:
            return cfgmod.clone(self._config), self._revision, self._warning

    def transaction(self, mutate: Callable[[Config], None]) -> Tuple[Config, int]:
        with self._lock:
            candidate = cfgmod.clone(self._config)
            mutate(candidate)
            candidate.validate()
            self._save_fn(candidate, self._path)
            if self._publish_fn is not None:
                try:
                    self._publish_fn(cfgmod.clone(candidate))
                except Exception:
                    # Persistence happened first by design. Restore the last
                    # published value before exposing the failure to the API.
                    self._save_fn(self._config, self._path)
                    raise
            self._config = candidate
            self._revision += 1
            self._warning = None
            return cfgmod.clone(candidate), self._revision
