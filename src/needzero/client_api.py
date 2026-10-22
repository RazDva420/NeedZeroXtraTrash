"""Unelevated desktop API proxy; controller operations cross authenticated IPC."""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import autostart
from .ipc import IpcClient, IpcError, ReloadingIpcClient


class ClientApi:
    def __init__(
        self,
        client: IpcClient | ReloadingIpcClient,
        *,
        quit_fn: Optional[Callable[[], None]] = None,
        minimize_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self._client = client
        self._quit_fn = quit_fn
        self._minimize_fn = minimize_fn

    def _object_call(self, method: str, *args: object) -> dict[str, Any]:
        result = self._client.call(method, *args)
        if not isinstance(result, dict):
            raise IpcError("invalid_response", "Controller response must be an object")
        return result

    def get_state(self) -> dict[str, Any]:
        return self._object_call("get_state")

    def get_config(self) -> dict[str, Any]:
        return self._object_call("get_config")

    def list_sensors(self) -> list[dict[str, Any]]:
        result = self._client.call("list_sensors")
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise IpcError("invalid_response", "Controller sensor response must be a list of objects")
        return result

    def set_curve(self, group: str, points: list[list[float]]) -> dict[str, Any]:
        return self._object_call("set_curve", group, points)

    def set_source(self, group: str, source: str) -> dict[str, Any]:
        return self._object_call("set_source", group, source)

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._object_call("set_mode", mode)

    def apply_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return self._object_call("apply_settings", settings)

    def reset_defaults(self) -> dict[str, Any]:
        return self._object_call("reset_defaults")

    def apply_now(self) -> bool:
        return self._client.call("apply_now") is True

    def test_full(self, seconds: float = 10) -> bool:
        return self._client.call("test_full", seconds) is True

    def get_autostart(self) -> bool:
        return bool(autostart.is_enabled())

    def set_autostart(self, enabled: bool) -> dict[str, Any]:
        try:
            current = self.get_autostart()
            if bool(enabled) == current:
                return {"ok": True, "enabled": current, "message": ""}
            if enabled:
                autostart.enable()
            else:
                autostart.disable()
            return {"ok": True, "enabled": self.get_autostart(), "message": ""}
        except Exception as exc:
            return {"ok": False, "enabled": self.get_autostart(), "message": str(exc)}

    def minimize_window(self) -> bool:
        if self._minimize_fn is not None:
            self._minimize_fn()
        return True

    def quit(self) -> bool:
        if self._quit_fn is not None:
            self._quit_fn()
        return True
