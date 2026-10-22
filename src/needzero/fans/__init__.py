"""Fan output backends (liquidctl for the NZXT controller, plus a mock)."""

from .base import FanBackend, FanStatus


def create_backend(kind: str = "auto", preferred_controller_id=None):
    """Return the best available fan backend.

    ``kind`` may be ``auto`` (retry the NZXT controller without a mock fallback),
    ``liquidctl`` (force real hardware) or ``mock``.
    """
    from .mock import MockFanBackend

    if kind == "mock":
        return MockFanBackend()

    if kind == "auto":
        from .auto import AutoFanBackend

        backend = AutoFanBackend(preferred_controller_id=preferred_controller_id)
        backend.open()
        return backend

    if kind == "liquidctl":
        try:
            from .process import ProcessFanBackend
            backend = ProcessFanBackend(
                preferred_controller_id=preferred_controller_id)
            backend.open()
            return backend
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"NZXT controller unavailable: {exc}") from exc

    raise ValueError(f"unsupported fan backend: {kind}")


__all__ = ["FanBackend", "FanStatus", "create_backend"]
