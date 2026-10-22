"""Lightweight system-tray client. It never owns the control loop or WebView."""

from __future__ import annotations

import logging
from typing import Callable

from .config import MODES
from .icon import make_icon

_LOGGER = logging.getLogger(__name__)


class Tray:
    def __init__(self, app) -> None:
        self._app = app
        self._icon = None

    def _build(self):
        import pystray
        from pystray import MenuItem as Item, Menu

        def mode_item(name: str):
            return Item(
                name.capitalize(),
                lambda icon, item: self._app.set_mode(name),
                checked=lambda item, n=name: self._app.get_mode() == n,
                radio=True,
            )

        menu = Menu(
            Item("Show / Hide window", lambda icon, item: self._app.toggle_window(),
                 default=True),
            Menu.SEPARATOR,
            Item("Mode", Menu(*[mode_item(n) for n in MODES])),
            Item("Test fans 100% (10s)", lambda icon, item: self._app.test_full()),
            Menu.SEPARATOR,
            Item("Start with Windows",
                 lambda icon, item: self._app.toggle_autostart(),
                 checked=lambda item: self._app.get_autostart()),
            Menu.SEPARATOR,
            Item("Quit", lambda icon, item: self._app.quit()),
        )
        return pystray.Icon("NeedZeroXtraTrash", make_icon(64),
                            "NeedZeroXtraTrash - Fan Control", menu)

    def run(self) -> None:
        self._icon = self._build()
        self._icon.run()

    def refresh(self) -> None:
        if self._icon is not None:
            try:
                self._icon.update_menu()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
