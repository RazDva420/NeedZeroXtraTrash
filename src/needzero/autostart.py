"""Machine controller and desktop tray Scheduled Task installation.

The installed controller starts at boot as LocalSystem.  The tray is a separate
least-privileged logon client for the user who enabled autostart.  Task
definitions are checked structurally after registration and a failed install is
rolled back; no task XML is written below a user-controlled TEMP directory.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import ntpath
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import uuid
from xml.sax.saxutils import escape

from .runtime_paths import (
    controller_log_path,
    install_manifest_path,
    ipc_token_path,
    machine_config_path,
)
from .windows_security import (
    account_name_for_sid,
    assert_no_reparse_points,
    authenticode_is_valid,
    current_user_sid,
    ensure_install_dir,
    ensure_ipc_token_access,
    ensure_machine_data_dir,
    ensure_protected_directory,
    grant_directory_traverse,
    install_dir,
    is_elevated,
    machine_data_dir,
    roaming_appdata_directory,
    protect_machine_file,
    protect_executable_for_user,
    protected_file_reader_sid,
    require_protected_directory,
    require_protected_executable,
    require_protected_file,
    run_self_elevated,
    sanitized_system_env,
    system_directory,
    system_executable,
    windows_powershell_executable,
)


CONTROLLER_TASK_NAME = "NeedZeroXtraTrash Controller"
UI_TASK_PREFIX = "NeedZeroXtraTrash UI "
TASK_NAME = CONTROLLER_TASK_NAME
SYSTEM_SID = "S-1-5-18"
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_SOURCE = "NeedZeroXtraTrash"
_CONTROLLER_URI = rf"\{CONTROLLER_TASK_NAME}"
_INSTALLED_EXE = "NeedZeroXtraTrash.exe"
_CONTROLLER_ARGUMENTS = "--controller"
_UI_ARGUMENTS = "--tray"
_DEV_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_DEV_RUN_VALUE = "NeedZeroXtraTrash"
_DEV_RUN_ARGUMENTS = "--startup"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_TASK_RUNNER = subprocess.run
_LOGGER = logging.getLogger(__name__)
_TASK_STATE_RUNNING = 4
_TASK_NAME_ENV = "NEEDZERO_TASK_NAME"
# A3 worst case includes worker join, failed in-flight USB I/O, a fresh device
# open (15 s) and final set/readback.  Keep UAC uninstall above that bound.
_CONTROLLER_SHUTDOWN_TIMEOUT = 70.0
_TASK_EXIT_WAIT = 15.0
_CONTROLLER_READY_TIMEOUT = 40.0
_TASK_STATE_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$service=New-Object -ComObject 'Schedule.Service';"
    "$service.Connect();"
    "$name=[Environment]::GetEnvironmentVariable('NEEDZERO_TASK_NAME','Process');"
    "if([string]::IsNullOrWhiteSpace($name)){throw 'Task name is unavailable'};"
    "$task=$service.GetFolder('\\').GetTask($name);"
    "[Console]::Out.Write([int]$task.State)"
)
_LIST_UI_TASKS_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$service=New-Object -ComObject 'Schedule.Service';"
    "$service.Connect();"
    "$folder=$service.GetFolder('\\');"
    "$items=@($folder.GetTasks(1)|ForEach-Object {"
    "if($_.Name.StartsWith('NeedZeroXtraTrash UI ')){"
    "[pscustomobject]@{Name=$_.Name;Xml=$_.Definition.XmlText}}});"
    "[Console]::Out.Write(($items|ConvertTo-Json -Compress -Depth 4))"
)


def _q(name: str) -> str:
    return f"{{{_TASK_NS}}}{name}"


def _ui_task_name(sid: str) -> str:
    return UI_TASK_PREFIX + sid


def _ui_uri(sid: str) -> str:
    return rf"\{_ui_task_name(sid)}"


def _current_exe() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("safe autostart is available only in the packaged EXE")
    path = Path(sys.executable).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _installed_exe() -> Path:
    return install_dir() / _INSTALLED_EXE


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_release_binary(path: Path) -> None:
    from . import BUILD_CHANNEL

    if BUILD_CHANNEL != "release":
        raise RuntimeError("privileged autostart is disabled for unsigned DEV builds")
    if not authenticode_is_valid(path):
        raise RuntimeError("autostart requires a valid Authenticode release binary")


def _dev_autostart_command() -> str:
    return subprocess.list2cmdline([str(_current_exe()), _DEV_RUN_ARGUMENTS])


def _read_dev_autostart() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _DEV_RUN_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            value, kind = winreg.QueryValueEx(key, _DEV_RUN_VALUE)
    except FileNotFoundError:
        return None
    return value if kind == winreg.REG_SZ and isinstance(value, str) else None


def _write_dev_autostart(command: str) -> None:
    if os.name != "nt":
        raise OSError("DEV autostart is available only on Windows")
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, _DEV_RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, _DEV_RUN_VALUE, 0, winreg.REG_SZ, command)


def _delete_dev_autostart() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _DEV_RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _DEV_RUN_VALUE)
    except FileNotFoundError:
        pass


def _dev_autostart_enabled() -> bool:
    try:
        return _read_dev_autostart() == _dev_autostart_command()
    except (OSError, RuntimeError):
        return False


def _install_protected_copy(
    reader_sid: str, rollback_reader_sid: str | None = None
) -> tuple[Path, Path | None]:
    source = _current_exe()
    _assert_release_binary(source)
    directory = ensure_install_dir()
    grant_directory_traverse(directory, reader_sid)
    target = directory / _INSTALLED_EXE
    assert_no_reparse_points(target)
    if target.exists() and not target.is_file():
        raise RuntimeError("installed executable target is not a regular file")
    if target.is_file() and _sha256(source) == _sha256(target):
        if not authenticode_is_valid(target):
            raise RuntimeError("installed executable has an invalid Authenticode signature")
        protect_executable_for_user(target, reader_sid)
        return target, None

    temporary = None
    backup = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="install-", suffix=".exe", dir=directory, delete=False
        ) as handle:
            temporary = Path(handle.name)
        shutil.copy2(source, temporary)
        if _sha256(source) != _sha256(temporary):
            raise RuntimeError("staged executable failed integrity verification")
        if not authenticode_is_valid(temporary):
            raise RuntimeError("staged executable has an invalid Authenticode signature")
        if target.exists():
            backup = directory / f"previous-{uuid.uuid4().hex}.exe"
            os.replace(target, backup)
        try:
            os.replace(temporary, target)
            temporary = None
            if _sha256(source) != _sha256(target) or not authenticode_is_valid(target):
                raise RuntimeError("installed executable failed post-copy verification")
            protect_executable_for_user(target, reader_sid)
        except BaseException:
            try:
                if target.exists():
                    target.unlink()
            finally:
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                    restored_sid = rollback_reader_sid or reader_sid
                    grant_directory_traverse(directory, restored_sid)
                    protect_executable_for_user(target, restored_sid)
                    backup = None
            raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return target, backup


def _settings_xml(restart_count: int) -> str:
    return f"""  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Count>{restart_count}</Count>
      <Interval>PT1M</Interval>
    </RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>"""


def controller_task_xml(command: Path) -> str:
    command = Path(command)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_TASK_NS}">
  <RegistrationInfo>
    <Source>{_SOURCE}</Source>
    <Description>NeedZeroXtraTrash machine fan controller</Description>
    <URI>{_CONTROLLER_URI}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Controller">
      <UserId>{SYSTEM_SID}</UserId><RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
{_settings_xml(999)}
  <Triggers>
    <BootTrigger><Delay>PT15S</Delay></BootTrigger>
    <CalendarTrigger>
      <StartBoundary>2000-01-01T00:00:00</StartBoundary>
      <Repetition><Interval>PT5M</Interval><Duration>P1D</Duration></Repetition>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Controller">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{_CONTROLLER_ARGUMENTS}</Arguments>
      <WorkingDirectory>{escape(str(command.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def ui_task_xml(command: Path, sid: str) -> str:
    command = Path(command)
    sid = escape(sid)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_TASK_NS}">
  <RegistrationInfo>
    <Source>{_SOURCE}</Source>
    <Description>NeedZeroXtraTrash desktop tray client</Description>
    <URI>{_ui_uri(sid)}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="DesktopUser">
      <UserId>{sid}</UserId><LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
{_settings_xml(3)}
  <Triggers><LogonTrigger><UserId>{sid}</UserId></LogonTrigger></Triggers>
  <Actions Context="DesktopUser">
    <Exec>
      <Command>{escape(str(command))}</Command><Arguments>{_UI_ARGUMENTS}</Arguments>
      <WorkingDirectory>{escape(str(command.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _text(parent: ET.Element, path: str) -> str:
    element = parent.find(path)
    return "" if element is None else (element.text or "").strip()


def _same_windows_path(left: str, right: Path) -> bool:
    return bool(left and ntpath.isabs(left)) and ntpath.normcase(
        ntpath.normpath(left)
    ) == ntpath.normcase(ntpath.normpath(str(right)))


def _account_matches_sid(value: str, sid: str) -> bool:
    if value.casefold() == sid.casefold():
        return True
    try:
        return value.casefold() == account_name_for_sid(sid).casefold()
    except (OSError, RuntimeError, ValueError):
        return False


def _parse_task(xml: str) -> ET.Element:
    root = ET.fromstring(xml)
    if root.tag != _q("Task"):
        raise ValueError("unexpected task XML namespace")
    return root


def _children_are(parent: ET.Element | None, *names: str) -> bool:
    return parent is not None and [item.tag for item in parent] == [
        _q(name) for name in names
    ]


def _common_task_valid(
    root: ET.Element, *, command: Path, arguments: str, principal_id: str,
    principal_sid: str, logon_type: str | None, run_level: str | None, uri: str,
    restart_count: str, description: str,
) -> bool:
    if root.attrib != {"version": "1.4"} or not _children_are(
        root, "RegistrationInfo", "Principals", "Settings", "Triggers", "Actions"
    ):
        return False
    registration = root.find(_q("RegistrationInfo"))
    principals = root.find(_q("Principals"))
    settings = root.find(_q("Settings"))
    actions = root.find(_q("Actions"))
    if registration is None or principals is None or settings is None or actions is None:
        return False
    if registration.attrib or not _children_are(
        registration, "Source", "Description", "URI"
    ):
        return False
    principal_nodes, action_nodes = list(principals), list(actions)
    if len(principal_nodes) != 1 or len(action_nodes) != 1:
        return False
    principal, action = principal_nodes[0], action_nodes[0]
    if (
        principal.tag != _q("Principal")
        or action.tag != _q("Exec")
        or principal.attrib != {"id": principal_id}
        or actions.attrib != {"Context": principal_id}
        or action.attrib
    ):
        return False
    principal_children = ["UserId"]
    if logon_type is not None:
        principal_children.append("LogonType")
    if run_level is not None:
        principal_children.append("RunLevel")
    if not _children_are(principal, *principal_children):
        return False
    if not _children_are(action, "Command", "Arguments", "WorkingDirectory"):
        return False
    if not _children_are(
        settings,
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "ExecutionTimeLimit",
        "MultipleInstancesPolicy",
        "RestartOnFailure",
        "StartWhenAvailable",
        "IdleSettings",
        "UseUnifiedSchedulingEngine",
    ):
        return False
    restart = settings.find(_q("RestartOnFailure"))
    idle = settings.find(_q("IdleSettings"))
    if (
        not _children_are(restart, "Count", "Interval")
        or not _children_are(idle, "StopOnIdleEnd", "RestartOnIdle")
    ):
        return False
    # Task Scheduler security/lifecycle definitions are accepted as an exact
    # allowlist.  A second action, hidden trigger setting, or unexpected
    # attribute must not survive a post-registration check.
    for element in root.iter():
        if element in (root, principal, actions):
            continue
        if element.attrib:
            return False
    return all((
        _text(registration, _q("Description")) == description,
        _text(registration, _q("Source")) == _SOURCE,
        _text(registration, _q("URI")) == uri,
        _text(principal, _q("UserId")) == principal_sid,
        logon_type is None or _text(principal, _q("LogonType")) == logon_type,
        run_level is None or _text(principal, _q("RunLevel")) == run_level,
        _same_windows_path(_text(action, _q("Command")), command),
        _text(action, _q("Arguments")) == arguments,
        _same_windows_path(_text(action, _q("WorkingDirectory")), command.parent),
        _text(settings, _q("MultipleInstancesPolicy")) == "IgnoreNew",
        _text(settings, _q("DisallowStartIfOnBatteries")) == "false",
        _text(settings, _q("StopIfGoingOnBatteries")) == "false",
        _text(settings, _q("StartWhenAvailable")) == "true",
        _text(settings, f"{_q('IdleSettings')}/{_q('StopOnIdleEnd')}") == "true",
        _text(settings, f"{_q('IdleSettings')}/{_q('RestartOnIdle')}") == "false",
        _text(settings, _q("UseUnifiedSchedulingEngine")) == "true",
        _text(settings, _q("ExecutionTimeLimit")) == "PT0S",
        _text(settings, f"{_q('RestartOnFailure')}/{_q('Interval')}") == "PT1M",
        _text(settings, f"{_q('RestartOnFailure')}/{_q('Count')}") == restart_count,
    ))


def validate_controller_task(xml: str, command: Path) -> bool:
    try:
        root = _parse_task(xml)
        triggers = root.find(_q("Triggers"))
        if triggers is None or [item.tag for item in triggers] != [
            _q("BootTrigger"), _q("CalendarTrigger")
        ]:
            return False
        boot, watchdog = list(triggers)
        repetition = watchdog.find(_q("Repetition"))
        schedule = watchdog.find(_q("ScheduleByDay"))
        trigger_valid = all((
            _children_are(boot, "Delay"),
            _children_are(
                watchdog,
                "StartBoundary",
                "Repetition",
                "ScheduleByDay",
            ),
            _children_are(
                repetition, "Interval", "Duration"
            ),
            _children_are(schedule, "DaysInterval"),
            _text(boot, _q("Delay")) == "PT15S",
            _text(watchdog, _q("StartBoundary")) == "2000-01-01T00:00:00",
            _text(watchdog, f"{_q('Repetition')}/{_q('Interval')}") == "PT5M",
            _text(watchdog, f"{_q('Repetition')}/{_q('Duration')}") == "P1D",
            _text(watchdog, f"{_q('ScheduleByDay')}/{_q('DaysInterval')}") == "1",
        ))
        return trigger_valid and _common_task_valid(
            root, command=Path(command), arguments=_CONTROLLER_ARGUMENTS,
            principal_id="Controller", principal_sid=SYSTEM_SID,
            logon_type=None, run_level="HighestAvailable",
            uri=_CONTROLLER_URI, restart_count="999",
            description="NeedZeroXtraTrash machine fan controller",
        )
    except (ET.ParseError, ValueError, TypeError):
        return False


def validate_ui_task(xml: str, command: Path, sid: str) -> bool:
    try:
        root = _parse_task(xml)
        triggers = root.find(_q("Triggers"))
        if triggers is None or len(list(triggers)) != 1:
            return False
        trigger = list(triggers)[0]
        trigger_valid = all((
            trigger.tag == _q("LogonTrigger"),
            _children_are(trigger, "UserId"),
            _account_matches_sid(_text(trigger, _q("UserId")), sid),
        ))
        return trigger_valid and _common_task_valid(
            root, command=Path(command), arguments=_UI_ARGUMENTS,
            principal_id="DesktopUser", principal_sid=sid,
            logon_type="InteractiveToken", run_level=None,
            uri=_ui_uri(sid), restart_count="3",
            description="NeedZeroXtraTrash desktop tray client",
        )
    except (ET.ParseError, ValueError, TypeError):
        return False


def _is_owned(xml: str, uri: str) -> bool:
    try:
        registration = _parse_task(xml).find(_q("RegistrationInfo"))
        return registration is not None and all((
            _text(registration, _q("Source")) == _SOURCE,
            _text(registration, _q("URI")) == uri,
        ))
    except (ET.ParseError, ValueError):
        return False


def _schtasks(arguments: list[str]) -> subprocess.CompletedProcess:
    return _TASK_RUNNER(
        [str(system_executable("schtasks.exe")), *arguments],
        capture_output=True, text=True, timeout=20,
        cwd=str(system_directory()), env=sanitized_system_env(),
        creationflags=_NO_WINDOW,
    )


def _task_state(name: str) -> int:
    """Read the locale-independent Task Scheduler state through its COM API."""
    env = sanitized_system_env()
    env[_TASK_NAME_ENV] = name
    proc = _TASK_RUNNER(
        [
            str(windows_powershell_executable()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-Command",
            _TASK_STATE_SCRIPT,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(system_directory()),
        env=env,
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or "Task Scheduler state query failed").strip()
        )
    try:
        state = int(proc.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Task Scheduler returned an invalid state") from exc
    if state not in range(5):
        raise RuntimeError(f"Task Scheduler returned an unknown state: {state}")
    return state


def _list_ui_task_definitions() -> dict[str, str]:
    """Enumerate root-folder UI tasks without parsing localized schtasks text."""
    proc = _TASK_RUNNER(
        [
            str(windows_powershell_executable()),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-Command",
            _LIST_UI_TASKS_SCRIPT,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(system_directory()),
        env=sanitized_system_env(),
        creationflags=_NO_WINDOW,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or "Task Scheduler enumeration failed").strip()
        )
    if not proc.stdout.strip():
        return {}
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Task Scheduler returned invalid enumeration data") from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise RuntimeError("Task Scheduler returned invalid enumeration data")
    result: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise RuntimeError("Task Scheduler returned an invalid task record")
        name, xml = item.get("Name"), item.get("Xml")
        if not isinstance(name, str) or not isinstance(xml, str):
            raise RuntimeError("Task Scheduler returned an invalid task record")
        if not name.startswith(UI_TASK_PREFIX) or name in result:
            raise RuntimeError("Task Scheduler returned an unexpected UI task name")
        result[name] = xml
    return result


def _sid_text_valid(sid: str) -> bool:
    return bool(
        isinstance(sid, str)
        and sid.startswith("S-1-")
        and all(character in "S-0123456789" for character in sid)
    )


def _owned_ui_tasks(definitions: dict[str, str] | None = None) -> dict[str, str]:
    """Return only product-owned UI tasks; similarly named foreign tasks survive."""
    owned = {}
    if definitions is None:
        definitions = _list_ui_task_definitions()
    for name, xml in definitions.items():
        sid = name[len(UI_TASK_PREFIX):]
        if _sid_text_valid(sid) and _is_owned(xml, _ui_uri(sid)):
            owned[name] = xml
    return owned


def _wait_task_stopped(name: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _task_state(name) != _TASK_STATE_RUNNING:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _wait_task_running(name: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _task_state(name) == _TASK_STATE_RUNNING:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _request_safe_controller_stop(token: bytes) -> None:
    """Synchronously request the A3 safe fan write, then await process exit."""
    from .ipc import IpcClient, IpcError

    try:
        acknowledged = IpcClient(token, timeout=_CONTROLLER_SHUTDOWN_TIMEOUT).call(
            "shutdown_controller"
        )
    except IpcError as exc:
        # Releases through v4.2 completed the synchronous safe-speed write but
        # their watchdog could close IPC before serialising the acknowledgement.
        # Accept only that exact response failure and only after Task Scheduler
        # independently confirms that the controller process has exited.
        if exc.code != "connection_closed" or not _wait_task_stopped(
            CONTROLLER_TASK_NAME, timeout=_TASK_EXIT_WAIT
        ):
            raise
        return
    if acknowledged is not True:
        raise RuntimeError("controller did not acknowledge the safe shutdown")
    if not _wait_task_stopped(CONTROLLER_TASK_NAME, timeout=_TASK_EXIT_WAIT):
        raise TimeoutError("controller remained running after safe shutdown acknowledgement")


def _wait_controller_ready(token: bytes, timeout: float = _CONTROLLER_READY_TIMEOUT) -> None:
    """Require authenticated IPC from the newly started controller generation."""
    from .ipc import IpcClient

    deadline = time.monotonic() + max(0.0, timeout)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if _task_state(CONTROLLER_TASK_NAME) != _TASK_STATE_RUNNING:
                raise RuntimeError("controller task exited before authenticated readiness")
            remaining = max(0.05, deadline - time.monotonic())
            state = IpcClient(token, timeout=min(1.0, remaining)).call("get_state")
            if not isinstance(state, dict):
                raise RuntimeError("controller returned an invalid readiness snapshot")
            return
        except RuntimeError as exc:
            if "exited before" in str(exc):
                raise
            last_error = exc
        except Exception as exc:
            last_error = exc
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    raise TimeoutError(
        "controller did not provide authenticated IPC readiness"
        + (f": {last_error}" if last_error else "")
    )


def _start_controller_task(token: bytes) -> None:
    proc = _schtasks(["/Run", "/TN", CONTROLLER_TASK_NAME])
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or "controller task failed to start").strip()
        )
    if not _wait_task_running(CONTROLLER_TASK_NAME, timeout=10.0):
        raise RuntimeError("controller task did not enter the Running state")
    _wait_controller_ready(token)


def _replace_protected_file(
    path: Path, payload: bytes, protector
) -> None:
    """Durably replace a privileged file without exposing inherited weak ACLs."""
    path = assert_no_reparse_points(Path(path))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        protector(temporary)
        os.replace(temporary, path)
        temporary = None
        protector(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _rotate_ipc_token(path: Path, sid: str) -> bytes:
    """Always replace the token; never adopt a preplanted known value."""
    token = secrets.token_bytes(32)
    _replace_protected_file(
        path,
        token.hex().encode("ascii"),
        lambda candidate: ensure_ipc_token_access(candidate, sid),
    )
    from .ipc import read_token

    if read_token(path) != token:
        raise RuntimeError("IPC token failed post-rotation content verification")
    require_protected_directory(path.parent, sid)
    require_protected_file(path, sid)
    return token


def _restore_ipc_token(path: Path, raw: bytes, reader_sid: str) -> None:
    _replace_protected_file(
        path,
        raw,
        lambda candidate: ensure_ipc_token_access(candidate, reader_sid),
    )
    if path.read_bytes() != raw:
        raise RuntimeError("IPC token failed rollback content verification")
    require_protected_directory(path.parent, reader_sid)
    require_protected_file(path, reader_sid)


def _query_task(name: str) -> str | None:
    proc = _schtasks(["/Query", "/TN", name, "/XML"])
    return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None


@contextmanager
def _protected_task_file(xml: str):
    # The machine-data directory already grants one non-inheriting RX ACE to
    # the desktop SID so it can reach ipc.token.  Re-running
    # ensure_machine_data_dir() here would erase that ACE after every install.
    reader_sid = protected_file_reader_sid(ipc_token_path())
    if reader_sid is None:
        raise RuntimeError("IPC token has no authorized desktop reader")
    require_protected_directory(machine_data_dir(), reader_sid)
    staging = ensure_protected_directory(machine_data_dir() / "task-staging")
    path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-16", prefix="task-", suffix=".xml",
            dir=staging, delete=False,
        ) as handle:
            handle.write(xml)
            handle.flush()
            path = Path(handle.name)
        protect_machine_file(path)
        require_protected_file(path)
        yield path
    finally:
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _create_task(name: str, xml: str) -> None:
    with _protected_task_file(xml) as xml_path:
        proc = _schtasks(["/Create", "/TN", name, "/XML", str(xml_path), "/F"])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "schtasks failed").strip())


def _delete_task(name: str, *, require_success: bool = True) -> None:
    proc = _schtasks(["/Delete", "/TN", name, "/F"])
    if require_success and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "schtasks failed").strip())


def _task_snapshot_valid(name: str, xml: str, command: Path) -> bool:
    if name == CONTROLLER_TASK_NAME:
        return validate_controller_task(xml, command)
    if name.startswith(UI_TASK_PREFIX):
        sid = name[len(UI_TASK_PREFIX):]
        return _sid_text_valid(sid) and validate_ui_task(xml, command, sid)
    return False


def _restore_tasks(previous: dict[str, str | None]) -> list[str]:
    errors = []
    command = _installed_exe()
    for name, xml in previous.items():
        try:
            if xml is None:
                _delete_task(name, require_success=False)
                if _query_task(name) is not None:
                    raise RuntimeError("task remained after rollback deletion")
            else:
                if not _task_snapshot_valid(name, xml, command):
                    raise RuntimeError("refusing to restore an invalid task snapshot")
                _create_task(name, xml)
                restored = _query_task(name)
                if restored is None or not _task_snapshot_valid(name, restored, command):
                    raise RuntimeError("restored task failed full definition verification")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return errors


def _write_install_manifest(
    target: Path, sid: str, migration_backup: Path | None = None
) -> None:
    from . import BUILD_CHANNEL, __version__

    path = install_manifest_path()
    require_protected_directory(path.parent, sid)
    assert_no_reparse_points(path)
    payload = {
        "schema": 1, "product": _SOURCE, "version": __version__,
        "build_channel": BUILD_CHANNEL, "executable": str(target),
        "sha256": _sha256(target), "desktop_sid": sid,
        "controller_task": CONTROLLER_TASK_NAME, "ui_task": _ui_task_name(sid),
        "migration_backup": str(migration_backup) if migration_backup else None,
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _replace_protected_file(path, encoded, protect_machine_file)
    require_protected_file(path)


def _install_manifest_matches(target: Path, sid: str) -> bool:
    from . import BUILD_CHANNEL, __version__

    path = install_manifest_path()
    try:
        require_protected_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, RuntimeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("schema") == 1
        and payload.get("product") == _SOURCE
        and payload.get("version") == __version__
        and payload.get("build_channel") == BUILD_CHANNEL == "release"
        and _same_windows_path(str(payload.get("executable") or ""), target)
        and payload.get("sha256") == _sha256(target)
        and payload.get("desktop_sid") == sid
        and payload.get("controller_task") == CONTROLLER_TASK_NAME
        and payload.get("ui_task") == _ui_task_name(sid)
    )


def _require_installed_binary_access(
    target: Path, sid: str, *, allow_legacy_machine_only: bool = False
) -> None:
    """Validate the Program Files directory and EXE as one ACL generation."""
    try:
        require_protected_directory(install_dir(), sid)
        require_protected_executable(target, sid)
    except (OSError, RuntimeError):
        if not allow_legacy_machine_only:
            raise
        # v4.2 and earlier protected the EXE from writes but accidentally also
        # denied the desktop task execute access. Accept that exact legacy pair
        # only long enough for a signed elevated upgrade to repair it.
        require_protected_directory(install_dir())
        require_protected_file(target)


def _validate_and_protect_machine_config() -> None:
    path = machine_config_path()
    if not path.is_file():
        return
    protect_machine_file(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("protected machine configuration is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("protected machine configuration root must be an object")
    from .config import Config

    Config.from_dict(payload)


def _secure_install_is_identical(
    *, source: Path, target: Path, sid: str, controller_xml: str | None,
    ui_xml: str | None, owned_ui: dict[str, str],
    verify_signature: bool = True,
) -> bool:
    """Read-only strict idempotency check for a complete protected install."""
    try:
        if not (
            controller_xml
            and ui_xml
            and len(owned_ui) == 1
            and _ui_task_name(sid) in owned_ui
            and target.is_file()
            and _sha256(source) == _sha256(target)
            and (not verify_signature or authenticode_is_valid(target))
            and validate_controller_task(controller_xml, target)
            and validate_ui_task(ui_xml, target, sid)
        ):
            return False
        reader = protected_file_reader_sid(ipc_token_path())
        if reader != sid:
            return False
        require_protected_directory(machine_data_dir(), sid)
        _require_installed_binary_access(target, sid)
        require_protected_file(ipc_token_path(), sid)
        if machine_config_path().is_file():
            require_protected_file(machine_config_path())
        return _install_manifest_matches(target, sid)
    except (OSError, RuntimeError, ValueError):
        return False


def installed_status() -> bool:
    """Validate the complete installation from the privileged controller."""
    from . import BUILD_CHANNEL

    if BUILD_CHANNEL != "release":
        return False
    try:
        manifest = install_manifest_path()
        require_protected_file(manifest)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        sid = str(payload.get("desktop_sid") or "")
        if not _sid_text_valid(sid):
            return False
        current, target = _current_exe(), _installed_exe()
        if not target.is_file() or _sha256(current) != _sha256(target):
            return False
        controller_xml = _query_task(CONTROLLER_TASK_NAME)
        ui_xml = _query_task(_ui_task_name(sid))
        owned_ui = _owned_ui_tasks()
        return _secure_install_is_identical(
            source=current,
            target=target,
            sid=sid,
            controller_xml=controller_xml,
            ui_xml=ui_xml,
            owned_ui=owned_ui,
            # The controller runs as SYSTEM, whose trust store does not contain
            # a developer certificate installed for the desktop user.  Release
            # installation already requires a trusted signature before this
            # protected copy, hash manifest and exact task set are created.
            verify_signature=False,
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _authenticated_controller_reports_enabled() -> bool:
    from .ipc import ReloadingIpcClient

    return bool(
        ReloadingIpcClient(ipc_token_path(), timeout=10.0).call("get_autostart")
    )


def is_enabled() -> bool:
    from . import BUILD_CHANNEL

    if BUILD_CHANNEL != "release":
        return _dev_autostart_enabled()
    try:
        if installed_status():
            return True
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        return _authenticated_controller_reports_enabled()
    except Exception:
        return False


def enable() -> None:
    from . import BUILD_CHANNEL

    if BUILD_CHANNEL != "release":
        _write_dev_autostart(_dev_autostart_command())
        if not _dev_autostart_enabled():
            raise RuntimeError("DEV autostart registry verification failed")
        return
    if is_elevated():
        install_elevated()
        return
    code = run_self_elevated(["--install-autostart"])
    if code != 0:
        raise RuntimeError(f"elevated autostart installer failed with exit code {code}")
    if not _authenticated_controller_reports_enabled():
        raise RuntimeError("autostart post-elevation verification failed")


def install_elevated() -> None:
    if not is_elevated():
        raise PermissionError("autostart installation requires elevation")
    source = _current_exe()
    _assert_release_binary(source)
    for protected_path in (
        _installed_exe(),
        ipc_token_path(),
        install_manifest_path(),
        machine_config_path(),
    ):
        assert_no_reparse_points(protected_path)
    sid = current_user_sid()
    controller_name, ui_name = CONTROLLER_TASK_NAME, _ui_task_name(sid)
    controller_old = _query_task(controller_name)
    ui_definitions = _list_ui_task_definitions()
    ui_old = ui_definitions.get(ui_name) or _query_task(ui_name)
    owned_ui = _owned_ui_tasks(ui_definitions)
    if controller_old is not None and not _is_owned(controller_old, _CONTROLLER_URI):
        raise RuntimeError("refusing to replace an unowned controller task")
    if ui_old is not None and not _is_owned(ui_old, _ui_uri(sid)):
        raise RuntimeError("refusing to replace an unowned desktop task")
    if ui_old is not None:
        owned_ui.setdefault(ui_name, ui_old)

    target_path = _installed_exe()
    if _secure_install_is_identical(
        source=source,
        target=target_path,
        sid=sid,
        controller_xml=controller_old,
        ui_xml=ui_old,
        owned_ui=owned_ui,
    ):
        # Strict no-op: do not interrupt a healthy controller or rotate the
        # token/tasks merely because the user enabled the same release twice.
        return

    controller_valid = bool(
        controller_old
        and target_path.is_file()
        and authenticode_is_valid(target_path)
        and validate_controller_task(controller_old, target_path)
    )
    controller_was_running = False
    if controller_old is not None:
        controller_was_running = _task_state(controller_name) == _TASK_STATE_RUNNING
        if controller_was_running and not controller_valid:
            raise RuntimeError(
                "refusing to replace a running controller with an invalid task definition"
            )

    old_token_raw = None
    old_token = None
    old_reader_sid = None
    if controller_valid:
        # A valid signed target and exact owner/DACL are required before using
        # the old authentication material across an elevation boundary.
        old_reader_sid = protected_file_reader_sid(ipc_token_path())
        if old_reader_sid is None:
            raise RuntimeError("existing controller token has no desktop reader")
        require_protected_directory(machine_data_dir(), old_reader_sid)
        _require_installed_binary_access(
            target_path, old_reader_sid, allow_legacy_machine_only=True
        )
        require_protected_file(ipc_token_path(), old_reader_sid)
        old_token_raw = ipc_token_path().read_bytes()
        from .ipc import read_token

        old_token = read_token(ipc_token_path())
        if controller_was_running:
            # Upgrades never use /End.  If A3 cannot complete, the signed old
            # install remains untouched and the upgrade aborts.
            _request_safe_controller_stop(old_token)

    valid_ui: dict[str, str] = {}
    for name, xml in owned_ui.items():
        task_sid = name[len(UI_TASK_PREFIX):]
        if target_path.is_file() and validate_ui_task(xml, target_path, task_sid):
            valid_ui[name] = xml
        else:
            _LOGGER.warning("invalid owned UI task will not be restored: %s", name)

    controller_snapshot = controller_old if controller_valid else None
    previous: dict[str, str | None] = {controller_name: controller_snapshot}
    for name in owned_ui:
        previous[name] = valid_ui.get(name)
    previous.setdefault(ui_name, ui_old if ui_name in valid_ui else None)

    target_existed = _installed_exe().is_file()
    manifest_existed = install_manifest_path().is_file()
    manifest_previous = None
    target = None
    target_backup = None
    machine_config_existed = machine_config_path().is_file()
    migration_backup = None
    new_token = None
    start_requested = False
    tasks_mutated = False
    try:
        ensure_machine_data_dir()
        if manifest_existed and controller_valid:
            protect_machine_file(install_manifest_path())
            manifest_previous = install_manifest_path().read_bytes()
        if machine_config_existed:
            _validate_and_protect_machine_config()
        target, target_backup = _install_protected_copy(sid, old_reader_sid)

        token_path = ipc_token_path()
        assert_no_reparse_points(token_path)
        new_token = _rotate_ipc_token(token_path, sid)
        from . import config as cfgmod

        legacy_config = roaming_appdata_directory() / _SOURCE / "config.json"
        migration_backup = cfgmod.migrate_config(
            legacy_config, machine_config_path()
        )
        _validate_and_protect_machine_config()
        _write_install_manifest(target, sid, migration_backup)
        for stale_name in owned_ui:
            if stale_name != ui_name:
                tasks_mutated = True
                _delete_task(stale_name)
                if _query_task(stale_name) is not None:
                    raise RuntimeError(f"stale UI task remained after deletion: {stale_name}")
        tasks_mutated = True
        _create_task(controller_name, controller_task_xml(target))
        _create_task(ui_name, ui_task_xml(target, sid))
        controller_new, ui_new = _query_task(controller_name), _query_task(ui_name)
        if not controller_new or not validate_controller_task(controller_new, target):
            raise RuntimeError("controller task failed definition verification")
        if not ui_new or not validate_ui_task(ui_new, target, sid):
            raise RuntimeError("desktop task failed definition verification")
        remaining_owned_ui = _owned_ui_tasks()
        if set(remaining_owned_ui) != {ui_name}:
            raise RuntimeError("stale owned desktop tasks remained after installation")
        start_requested = True
        _start_controller_task(new_token)
        if target_backup is not None:
            try:
                target_backup.unlink()
                target_backup = None
            except OSError as cleanup_exc:
                # The new controller is already running safely.  A protected
                # previous executable is harmless and can be removed later.
                _LOGGER.warning("previous executable cleanup failed: %s", cleanup_exc)
        return
    except Exception as exc:
        rollback_errors = []
        if start_requested:
            try:
                if _task_state(controller_name) == _TASK_STATE_RUNNING:
                    if new_token is None:
                        raise RuntimeError("new controller token was not available")
                    _request_safe_controller_stop(new_token)
            except Exception as stop_exc:
                rollback_errors.append(f"new controller safe stop: {stop_exc}")
        if not rollback_errors and tasks_mutated:
            rollback_errors.extend(_restore_tasks(previous))
        try:
            if rollback_errors:
                raise RuntimeError("filesystem rollback withheld while task rollback is unsafe")
            if manifest_previous is not None and controller_valid:
                temporary = install_manifest_path().with_name(
                    install_manifest_path().name + ".rollback"
                )
                temporary.write_bytes(manifest_previous)
                protect_machine_file(temporary)
                os.replace(temporary, install_manifest_path())
                protect_machine_file(install_manifest_path())
            else:
                install_manifest_path().unlink(missing_ok=True)
        except (OSError, RuntimeError) as rollback_exc:
            rollback_errors.append(f"install manifest: {rollback_exc}")
        if not rollback_errors:
            try:
                if old_token_raw is not None and old_reader_sid is not None:
                    _restore_ipc_token(ipc_token_path(), old_token_raw, old_reader_sid)
                else:
                    ipc_token_path().unlink(missing_ok=True)
            except (OSError, RuntimeError) as rollback_exc:
                rollback_errors.append(f"IPC token: {rollback_exc}")
        if not rollback_errors:
            try:
                if target_backup is not None and target_backup.exists():
                    if target is not None and target.exists():
                        target.unlink()
                    os.replace(target_backup, _installed_exe())
                    restored_sid = old_reader_sid or sid
                    grant_directory_traverse(install_dir(), restored_sid)
                    protect_executable_for_user(_installed_exe(), restored_sid)
                    target_backup = None
                elif not target_existed and target is not None:
                    target.unlink(missing_ok=True)
                    ensure_install_dir()
            except (OSError, RuntimeError) as rollback_exc:
                rollback_errors.append(f"installed executable: {rollback_exc}")
        if not rollback_errors and not machine_config_existed and migration_backup is not None:
            try:
                machine_config_path().unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"migrated configuration: {rollback_exc}")
        if not rollback_errors and controller_was_running and controller_snapshot is not None:
            try:
                if old_token is None:
                    raise RuntimeError("old controller authentication token was unavailable")
                _start_controller_task(old_token)
            except Exception as restart_exc:
                rollback_errors.append(f"old controller restart: {restart_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"{exc}; protected install rollback also failed: "
                f"{'; '.join(rollback_errors)}"
            ) from exc
        raise


def disable() -> None:
    from . import BUILD_CHANNEL

    if BUILD_CHANNEL != "release":
        _delete_dev_autostart()
        if _read_dev_autostart() is not None:
            raise RuntimeError("DEV autostart registry value remained after deletion")
        return
    if is_elevated():
        raise RuntimeError(
            "start autostart removal from the unelevated desktop client so the "
            "configuration can be exported safely"
        )
    backup_hash = None
    export_error = None
    try:
        backup_hash = _export_config_unelevated()
    except Exception as exc:
        export_error = exc
        _LOGGER.error("configuration export before uninstall failed: %s", exc)
    arguments = ["--uninstall-autostart"]
    if backup_hash is not None:
        arguments.extend(["--config-backup-sha256", backup_hash])
    code = run_self_elevated(arguments)
    if code != 0:
        detail = f"; configuration export failed: {export_error}" if export_error else ""
        raise RuntimeError(
            f"elevated autostart removal was partial or failed with exit code {code}{detail}"
        )


def _legacy_config_path() -> Path:
    return roaming_appdata_directory() / _SOURCE / "config.json"


def _export_config_unelevated() -> str:
    """Export current controller config while still running as the desktop user."""
    from . import config as cfgmod
    from .ipc import ReloadingIpcClient

    payload = ReloadingIpcClient(ipc_token_path(), timeout=5.0).call("get_config")
    if not isinstance(payload, dict):
        raise RuntimeError("controller returned an invalid configuration export")
    payload = dict(payload)
    payload.pop("_status", None)
    config = cfgmod.Config.from_dict(payload)
    destination = _legacy_config_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        previous = destination.with_name(
            f"{destination.name}.pre-uninstall-{time.time_ns()}"
        )
        shutil.copy2(destination, previous)
    cfgmod.save(config, destination)
    return _sha256(destination)


def _verified_config_backup(expected_sha256: str | None) -> Path | None:
    """Validate the known user backup without ever writing to APPDATA elevated."""
    if expected_sha256 is None:
        return None
    expected_sha256 = expected_sha256.strip().lower()
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise ValueError("configuration backup hash must be 64 hexadecimal characters")
    backup = _legacy_config_path()
    assert_no_reparse_points(backup)
    if not backup.is_file() or _sha256(backup).lower() != expected_sha256:
        raise RuntimeError("configuration backup is missing or its hash does not match")
    from . import config as cfgmod

    try:
        payload = json.loads(backup.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("configuration backup is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("configuration backup root must be an object")
    cfgmod.Config.from_dict(payload)
    return backup


def uninstall_elevated(config_backup_sha256: str | None = None) -> None:
    if not is_elevated():
        raise PermissionError("autostart removal requires elevation")
    backup_verified = None
    backup_error = None
    try:
        backup_verified = _verified_config_backup(config_backup_sha256)
    except Exception as exc:
        backup_error = exc
        _LOGGER.error("configuration backup verification failed: %s", exc)
    controller_xml = _query_task(CONTROLLER_TASK_NAME)
    if controller_xml is not None and not _is_owned(controller_xml, _CONTROLLER_URI):
        raise RuntimeError(f"refusing to delete unowned task: {CONTROLLER_TASK_NAME}")
    tasks = {CONTROLLER_TASK_NAME: controller_xml, **_owned_ui_tasks()}
    # Ask the controller to perform its A3 safe write and stop synchronously.
    # Forced task termination is used only as a bounded, visible fallback.
    errors = []
    safe_stop_confirmed = False
    stopped = controller_xml is None
    if not stopped:
        try:
            stopped = _task_state(CONTROLLER_TASK_NAME) != _TASK_STATE_RUNNING
        except Exception as exc:
            _LOGGER.error("controller task state check failed: %s", exc)
    if not stopped:
        try:
            from .ipc import read_token

            reader_sid = protected_file_reader_sid(ipc_token_path())
            if reader_sid is None:
                raise RuntimeError("controller token has no authorized desktop reader")
            require_protected_directory(machine_data_dir(), reader_sid)
            require_protected_file(ipc_token_path(), reader_sid)
            _request_safe_controller_stop(read_token(ipc_token_path()))
            safe_stop_confirmed = True
            stopped = True
        except Exception as exc:
            _LOGGER.error("safe controller stop request failed: %s", exc)
    if not stopped:
        _LOGGER.warning(
            "forcing controller task termination after safe-stop %s",
            "timeout" if safe_stop_confirmed else "failure",
        )
        end_result = _schtasks(["/End", "/TN", CONTROLLER_TASK_NAME])
        try:
            stopped = _wait_task_stopped(CONTROLLER_TASK_NAME, timeout=10.0)
        except Exception:
            stopped = False
        if not stopped:
            errors.append(
                (end_result.stderr or end_result.stdout
                 or "forced controller task stop failed").strip()
            )
    if errors:
        raise RuntimeError("; ".join(errors))
    for name in tasks:
        if _query_task(name) is not None:
            try:
                _delete_task(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))
    if any(_query_task(name) is not None for name in tasks):
        raise RuntimeError("one or more tasks remained after deletion")
    for path in (
        ipc_token_path(),
        install_manifest_path(),
        controller_log_path(),
        *(Path(str(controller_log_path()) + f".{index}") for index in range(1, 4)),
        _installed_exe(),
    ):
        assert_no_reparse_points(path)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    if machine_config_path().is_file():
        if backup_verified is not None:
            assert_no_reparse_points(machine_config_path())
            machine_config_path().unlink()
        else:
            errors.append(
                "protected configuration was preserved because no verified user "
                f"backup was available{': ' + str(backup_error) if backup_error else ''}"
            )
    for directory in (machine_data_dir() / "task-staging", install_dir()):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        machine_data_dir().rmdir()
    except OSError:
        pass
    if errors:
        raise RuntimeError("; ".join(errors))
