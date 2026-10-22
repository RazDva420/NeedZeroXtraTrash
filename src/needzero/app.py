"""Unelevated desktop and tray clients for the headless controller."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME, BUILD_CHANNEL, __version__
from .client_api import ClientApi
from .ipc import ReloadingIpcClient
from .runtime_paths import executable_command, independent_process_env, ipc_token_path
from .single_instance import SingleInstance, show_already_running_message
from .tray import Tray


_LOGGER = logging.getLogger(__name__)
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_IDYES = 6
_MB_OK_ICONWARNING = 0x00000030
_MB_YESNO_ICONQUESTION = 0x00000024
_TRAY_INSTANCE_NAME = r"Local\NeedZeroXtraTrash.Tray"
_TRAY_QUIT_EVENT_NAME = r"Local\NeedZeroXtraTrash.Tray.Quit"
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF


def _tray_event_api():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
    ]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.ResetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _signal_tray_quit() -> bool:
    if sys.platform != "win32":
        return False
    kernel32 = _tray_event_api()
    handle = kernel32.OpenEventW(
        _EVENT_MODIFY_STATE, False, _TRAY_QUIT_EVENT_NAME,
    )
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def webui_path() -> str:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return str(base / "webui" / "index.html")
    return str(Path(__file__).with_name("webui") / "index.html")


def controller_client():
    return ReloadingIpcClient(ipc_token_path())


def _show_native_message(message: str, flags: int) -> int:
    """Show first-run status without importing WebView in the bootstrap path."""
    if sys.platform != "win32":
        return 0
    import ctypes

    return int(ctypes.windll.user32.MessageBoxW(
        None,
        message,
        APP_NAME,
        flags,
    ))


def _controller_reachable(timeout: float = 0.75) -> bool:
    try:
        ReloadingIpcClient(ipc_token_path(), timeout=timeout).call("get_state")
        return True
    except Exception:  # Controller/token may not exist yet on first run.
        return False


def _ensure_tray_client() -> bool:
    """Start the lightweight tray process unless this session already has one."""
    probe = SingleInstance(_TRAY_INSTANCE_NAME)
    if not probe.acquire():
        return True
    probe.release()
    try:
        subprocess.Popen(
            executable_command("--tray"),
            close_fds=True,
            creationflags=_NO_WINDOW,
            env=independent_process_env(),
        )
        return True
    except OSError:
        _LOGGER.exception("system tray client failed to start")
        return False


def _bootstrap_first_run_controller() -> str:
    """Offer the signed machine-controller install before WebView boot.

    The normal UI cannot load Settings until authenticated controller IPC is
    available. A clean Windows installation therefore needs this explicit
    consent path before the WebView starts. An unsigned DEV build does not
    install persistence, but may start its session controller after UAC consent.
    """
    if not getattr(sys, "frozen", False):
        return "source"
    if _controller_reachable():
        try:
            # Safe shutdown can include a bounded USB reconnect/readback and
            # therefore needs longer than the ordinary interactive IPC timeout.
            client = ReloadingIpcClient(ipc_token_path(), timeout=15.0)
            state = client.call("get_state")
            if state.get("controller_version") == __version__:
                return "connected"
            if not client.call("shutdown_controller"):
                raise RuntimeError("old controller rejected safe shutdown")
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and _controller_reachable(timeout=0.2):
                time.sleep(0.1)
            if _controller_reachable(timeout=0.2):
                raise TimeoutError("old controller did not stop within 10 seconds")
            time.sleep(0.5)  # allow the global USB-writer guard to be released
        except Exception as exc:
            _LOGGER.exception("controller replacement failed")
            _show_native_message(
                f"Controller update failed:\n\n{exc}\n\n"
                "Restart Windows, then launch this build again.",
                _MB_OK_ICONWARNING,
            )
            return "release-failed" if BUILD_CHANNEL == "release" else "dev-failed"

    if BUILD_CHANNEL != "release":
        try:
            from .windows_security import launch_self_elevated

            launch_self_elevated(["--controller"])
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                if _controller_reachable(timeout=0.4):
                    return "dev-started"
                time.sleep(0.2)
            raise TimeoutError("controller did not become ready within 25 seconds")
        except Exception as exc:
            _LOGGER.exception("DEV controller startup failed")
            _show_native_message(
                f"Controller startup failed:\n\n{exc}\n\n"
                "Confirm the Windows UAC prompt and close NZXT CAM.",
                _MB_OK_ICONWARNING,
            )
            return "dev-failed"

    from . import autostart

    if autostart.is_enabled():
        # The watchdog may still be starting the boot task.  The WebView boot
        # loop will keep retrying authenticated IPC without owning its life.
        return "installed-waiting"

    answer = _show_native_message(
        "The protected background cooling controller is not installed.\n\n"
        "Install it now so fan control starts with Windows? Windows will ask "
        "for administrator approval.",
        _MB_YESNO_ICONQUESTION,
    )
    if answer != _IDYES:
        return "declined"
    try:
        autostart.enable()
    except Exception as exc:
        _LOGGER.exception("first-run controller installation failed")
        _show_native_message(
            f"Controller installation failed:\n\n{exc}",
            _MB_OK_ICONWARNING,
        )
        return "failed"
    return "installed"


class DesktopApp:
    def __init__(self, client=None) -> None:
        self.window = None
        self._closed = threading.Event()
        self.api = ClientApi(
            client or controller_client(),
            quit_fn=self.quit,
            minimize_fn=self.close,
        )

    def run(self) -> None:
        # Import is intentionally inside the UI-only process. Controller and
        # tray modes never import pywebview or start WebView2.
        import webview

        self.window = webview.create_window(
            APP_NAME,
            url=webui_path(),
            js_api=self.api,
            width=1376,
            height=768,
            background_color="#c0c0c0",
            hidden=False,
            frameless=True,
            easy_drag=False,
            resizable=False,
        )
        self.window.events.closing += self._on_closing
        try:
            webview.start()
        finally:
            self._closed.set()

    def _on_closing(self):
        self._closed.set()
        return True

    def close(self) -> None:
        self._closed.set()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                _LOGGER.exception("desktop window could not be closed")

    def quit(self) -> None:
        if not _signal_tray_quit():
            _LOGGER.warning("tray quit signal was unavailable")
        self.close()


class TrayApp:
    def __init__(self, client=None) -> None:
        self.client = client or controller_client()
        self.api = ClientApi(self.client)
        self.tray = Tray(self)
        self._ui_process = None

    def run(self) -> None:
        if sys.platform != "win32":
            self.tray.run()
            return
        kernel32 = _tray_event_api()
        handle = kernel32.CreateEventW(None, True, False, _TRAY_QUIT_EVENT_NAME)
        if not handle:
            raise OSError("tray quit event could not be created")
        kernel32.ResetEvent(handle)

        def wait_for_quit() -> None:
            if kernel32.WaitForSingleObject(handle, _INFINITE) == _WAIT_OBJECT_0:
                self.tray.stop()

        watcher = threading.Thread(
            target=wait_for_quit, name="needzero-tray-quit", daemon=True,
        )
        watcher.start()
        try:
            self.tray.run()
        finally:
            kernel32.SetEvent(handle)
            watcher.join(timeout=1.0)
            kernel32.CloseHandle(handle)

    def show_window(self) -> None:
        if self._ui_process is not None and self._ui_process.poll() is None:
            return
        self._ui_process = subprocess.Popen(
            executable_command("--ui"),
            close_fds=True,
            creationflags=_NO_WINDOW,
            env=independent_process_env(),
        )

    def toggle_window(self) -> None:
        self.show_window()

    def get_mode(self) -> str:
        try:
            return str(self.client.call("get_config").get("mode", "balanced"))
        except Exception:
            return "balanced"

    def set_mode(self, mode: str) -> None:
        self.client.call("set_mode", mode)
        self.tray.refresh()

    def test_full(self) -> None:
        self.client.call("test_full", 10)

    def get_autostart(self) -> bool:
        return self.api.get_autostart()

    def toggle_autostart(self) -> None:
        result = self.api.set_autostart(not self.get_autostart())
        if not result.get("ok"):
            _LOGGER.error("autostart change failed: %s", result.get("message"))
        self.tray.refresh()

    def quit(self) -> None:
        self.tray.stop()


def run_ui() -> int:
    instance = SingleInstance(r"Local\NeedZeroXtraTrash.UI")
    if not instance.acquire():
        show_already_running_message("The NeedZeroXtraTrash window is already open.")
        return 2
    try:
        _bootstrap_first_run_controller()
        if not _ensure_tray_client():
            _show_native_message(
                "The system tray icon could not be started. The cooling "
                "controller is still running, but minimized windows cannot "
                "be reopened from the tray.",
                _MB_OK_ICONWARNING,
            )
        DesktopApp().run()
        return 0
    except Exception:
        _LOGGER.exception("desktop UI failed")
        return 1
    finally:
        instance.release()


def run_tray() -> int:
    instance = SingleInstance(_TRAY_INSTANCE_NAME)
    if not instance.acquire():
        return 2
    try:
        TrayApp().run()
        return 0
    except Exception:
        _LOGGER.exception("tray client failed")
        return 1
    finally:
        instance.release()


def run_startup() -> int:
    """DEV per-user startup: request controller elevation, then remain in tray."""
    _bootstrap_first_run_controller()
    return run_tray()
