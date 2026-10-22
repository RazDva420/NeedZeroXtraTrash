from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from needzero import autostart
import needzero
from needzero import controller_logging
from needzero import ipc
from needzero import single_instance
from needzero import windows_security
from needzero import runtime_paths
from needzero.sensors import lhm


SID_A = "S-1-5-21-100"
SID_B = "S-1-5-21-200"
SID_C = "S-1-5-21-300"


def test_unsigned_dev_autostart_uses_current_user_registry_without_elevation(
    monkeypatch, tmp_path
):
    executable = tmp_path / "NeedZeroXtraTrash.exe"
    executable.write_bytes(b"dev")
    stored = {"value": None}
    monkeypatch.setattr(needzero, "BUILD_CHANNEL", "dev")
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(autostart.sys, "executable", str(executable))
    monkeypatch.setattr(autostart, "_read_dev_autostart", lambda: stored["value"])
    monkeypatch.setattr(
        autostart, "_write_dev_autostart",
        lambda value: stored.__setitem__("value", value),
    )
    monkeypatch.setattr(
        autostart, "_delete_dev_autostart",
        lambda: stored.__setitem__("value", None),
    )
    monkeypatch.setattr(
        autostart, "run_self_elevated",
        lambda *_args, **_kwargs: pytest.fail("DEV autostart requested elevation"),
    )

    autostart.enable()
    assert autostart.is_enabled()
    assert str(executable) in stored["value"]
    assert "--startup" in stored["value"]
    autostart.disable()
    assert not autostart.is_enabled()


def test_direct_privileged_dev_install_remains_blocked(monkeypatch, tmp_path):
    executable = tmp_path / "NeedZeroXtraTrash.exe"
    executable.write_bytes(b"dev")
    monkeypatch.setattr(needzero, "BUILD_CHANNEL", "dev")
    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    monkeypatch.setattr(autostart, "_current_exe", lambda: executable)
    with pytest.raises(RuntimeError, match="unsigned DEV"):
        autostart.install_elevated()


def _command() -> Path:
    return Path(r"C:\Program Files\NeedZeroXtraTrash\NeedZeroXtraTrash.exe")


def test_task_definitions_are_exact_allowlists():
    command = _command()
    controller = autostart.controller_task_xml(command)
    desktop = autostart.ui_task_xml(command, SID_A)
    assert autostart.validate_controller_task(controller, command)
    assert autostart.validate_ui_task(desktop, command, SID_A)
    # schtasks replaces RegistrationInfo/URI with the flat /TN registration
    # path. Keep generated and validated identities identical to that export.
    assert f"<URI>\\{autostart.CONTROLLER_TASK_NAME}</URI>" in controller
    assert f"<URI>\\{autostart._ui_task_name(SID_A)}</URI>" in desktop
    assert controller.index("<Source>") < controller.index("<Description>")
    assert "<IdleSettings>" in controller
    assert "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>" in controller
    assert not autostart.validate_controller_task(
        controller.replace(
            "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>",
            "<UseUnifiedSchedulingEngine>false</UseUnifiedSchedulingEngine>",
        ),
        command,
    )
    # Windows infers the SYSTEM service-account logon type. Explicitly writing
    # ServiceAccount makes schtasks reject this otherwise valid task XML.
    assert "<LogonType>" not in controller
    assert "<LogonType>InteractiveToken</LogonType>" in desktop

    assert not autostart.validate_controller_task(
        controller.replace(
            "<StartWhenAvailable>true</StartWhenAvailable>",
            "<StartWhenAvailable>false</StartWhenAvailable>",
        ),
        command,
    )
    assert not autostart.validate_ui_task(
        desktop.replace(SID_A, SID_B), command, SID_A
    )
    assert not autostart.validate_controller_task(
        controller.replace(
            "</Actions>",
            "<Exec><Command>C:\\evil.exe</Command><Arguments>x</Arguments>"
            "<WorkingDirectory>C:\\</WorkingDirectory></Exec></Actions>",
        ),
        command,
    )
    assert not autostart.validate_controller_task(
        controller.replace(
            "</Task>",
            "<Actions Context=\"Controller\"><Exec><Command>C:\\evil.exe</Command>"
            "<Arguments>x</Arguments><WorkingDirectory>C:\\</WorkingDirectory>"
            "</Exec></Actions></Task>",
        ),
        command,
    )
    assert not autostart.validate_controller_task(
        controller.replace(
            "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>",
            "<ExecutionTimeLimit debug=\"1\">PT0S</ExecutionTimeLimit>",
        ),
        command,
    )


def test_ui_trigger_accepts_only_the_account_name_for_its_sid(monkeypatch):
    command = _command()
    desktop = autostart.ui_task_xml(command, SID_A)
    normalized = desktop.replace(
        f"<Triggers><LogonTrigger><UserId>{SID_A}</UserId>",
        r"<Triggers><LogonTrigger><UserId>DOMAIN\user</UserId>",
    )
    monkeypatch.setattr(
        autostart, "account_name_for_sid", lambda sid: r"DOMAIN\user"
        if sid == SID_A else r"DOMAIN\other",
    )
    assert autostart.validate_ui_task(normalized, command, SID_A)
    assert not autostart.validate_ui_task(
        normalized.replace(r"DOMAIN\user", r"DOMAIN\attacker"), command, SID_A
    )


def test_rollback_rejects_invalid_snapshot_before_registration(monkeypatch):
    command = _command()
    invalid = autostart.controller_task_xml(command).replace(
        "<StartWhenAvailable>true</StartWhenAvailable>",
        "<StartWhenAvailable>false</StartWhenAvailable>",
    )
    monkeypatch.setattr(autostart, "_installed_exe", lambda: command)
    monkeypatch.setattr(
        autostart, "_create_task", lambda *_: pytest.fail("invalid snapshot registered")
    )
    errors = autostart._restore_tasks({autostart.CONTROLLER_TASK_NAME: invalid})
    assert errors and "invalid task snapshot" in errors[0]


def test_rollback_requires_full_post_query_validation(monkeypatch):
    command = _command()
    valid = autostart.controller_task_xml(command)
    disabled = valid.replace(
        "<StartWhenAvailable>true</StartWhenAvailable>",
        "<StartWhenAvailable>false</StartWhenAvailable>",
    )
    monkeypatch.setattr(autostart, "_installed_exe", lambda: command)
    monkeypatch.setattr(autostart, "_create_task", lambda *_: None)
    monkeypatch.setattr(autostart, "_query_task", lambda _name: disabled)
    errors = autostart._restore_tasks({autostart.CONTROLLER_TASK_NAME: valid})
    assert errors and "full definition verification" in errors[0]


def test_owned_ui_enumeration_covers_all_sids_but_preserves_foreign():
    command = _command()
    definitions = {
        autostart._ui_task_name(SID_A): autostart.ui_task_xml(command, SID_A),
        autostart._ui_task_name(SID_B): autostart.ui_task_xml(command, SID_B),
        autostart._ui_task_name(SID_C): autostart.ui_task_xml(command, SID_C).replace(
            "<Source>NeedZeroXtraTrash</Source>", "<Source>SomeoneElse</Source>"
        ),
        "Unrelated": "<Task/>",
    }
    assert set(autostart._owned_ui_tasks(definitions)) == {
        autostart._ui_task_name(SID_A),
        autostart._ui_task_name(SID_B),
    }


def test_exact_acl_resets_then_rejects_foreign_ace_or_wrong_owner(monkeypatch):
    calls = []
    trusted = Path(r"C:\Windows\System32")
    monkeypatch.setattr(windows_security, "system_directory", lambda: trusted)
    monkeypatch.setattr(
        windows_security, "system_executable", lambda name: trusted / name
    )
    monkeypatch.setattr(windows_security, "sanitized_system_env", lambda: {"PATH": str(trusted)})

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    expected = frozenset({("S-1-5-18", 1, 0)})
    windows_security._apply_exact_dacl(
        Path(r"C:\secure\item"),
        ["*S-1-5-18:F"],
        expected,
        runner=runner,
        dacl_reader=lambda _path: (True, expected),
        owner_reader=lambda _path: "S-1-5-32-544",
    )
    assert [call[0][2] for call in calls] == ["/setowner", "/reset", "/inheritance:r"]
    assert all(call[0][0] == str(trusted / "icacls.exe") for call in calls)
    assert all(call[1]["cwd"] == str(trusted) for call in calls)

    foreign = expected | {("S-1-5-21-999", 1, 0)}
    with pytest.raises(RuntimeError, match="security verification failed"):
        windows_security._apply_exact_dacl(
            Path(r"C:\secure\item"),
            ["*S-1-5-18:F"],
            expected,
            runner=runner,
            dacl_reader=lambda _path: (True, foreign),
            owner_reader=lambda _path: "S-1-5-32-544",
        )
    with pytest.raises(RuntimeError, match="security verification failed"):
        windows_security._apply_exact_dacl(
            Path(r"C:\secure\item"),
            ["*S-1-5-18:F"],
            expected,
            runner=runner,
            dacl_reader=lambda _path: (True, expected),
            owner_reader=lambda _path: "S-1-5-18",
        )


def test_installed_executable_grants_only_read_execute_to_desktop_user(
    monkeypatch, tmp_path
):
    executable = tmp_path / "NeedZeroXtraTrash.exe"
    executable.write_bytes(b"signed")
    calls = []
    expected = windows_security._executable_file_acl(SID_A)

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    windows_security.protect_executable_for_user(
        executable,
        SID_A,
        runner=runner,
        dacl_reader=lambda _path: (True, expected),
        owner_reader=lambda _path: "S-1-5-32-544",
    )

    grants = calls[-1][0]
    assert f"*{SID_A}:(RX)" in grants
    assert f"*{SID_A}:F" not in grants
    monkeypatch.setattr(
        windows_security, "read_owner_sid", lambda _path: "S-1-5-32-544"
    )
    monkeypatch.setattr(
        windows_security, "read_exact_dacl", lambda _path: (True, expected)
    )
    assert windows_security.require_protected_executable(executable, SID_A) == executable


def test_release_autostart_status_falls_back_to_authenticated_controller(monkeypatch):
    import needzero

    monkeypatch.setattr(needzero, "BUILD_CHANNEL", "release")
    monkeypatch.setattr(autostart, "installed_status", lambda: False)
    monkeypatch.setattr(
        autostart, "_authenticated_controller_reports_enabled", lambda: True
    )

    assert autostart.is_enabled() is True


def test_privileged_installed_status_does_not_depend_on_user_certificate_store(
    monkeypatch, tmp_path
):
    import needzero

    target = tmp_path / "NeedZeroXtraTrash.exe"
    target.write_bytes(b"protected-release")
    manifest = tmp_path / "install.json"
    manifest.write_text('{"desktop_sid":"' + SID_A + '"}', encoding="utf-8")
    observed = {}

    monkeypatch.setattr(needzero, "BUILD_CHANNEL", "release")
    monkeypatch.setattr(autostart, "install_manifest_path", lambda: manifest)
    monkeypatch.setattr(autostart, "require_protected_file", lambda path, *args: path)
    monkeypatch.setattr(autostart, "_current_exe", lambda: target)
    monkeypatch.setattr(autostart, "_installed_exe", lambda: target)
    monkeypatch.setattr(autostart, "_query_task", lambda _name: "task")
    monkeypatch.setattr(
        autostart, "_owned_ui_tasks", lambda: {autostart._ui_task_name(SID_A): "task"}
    )
    monkeypatch.setattr(
        autostart,
        "authenticode_is_valid",
        lambda _path: pytest.fail("SYSTEM status must not use the desktop trust store"),
    )

    def identical(**kwargs):
        observed.update(kwargs)
        return True

    monkeypatch.setattr(autostart, "_secure_install_is_identical", identical)
    assert autostart.installed_status() is True
    assert observed["verify_signature"] is False


def test_token_rotation_replaces_planted_value_and_rollback_restores_sid_acl(
    tmp_path, monkeypatch
):
    token_path = tmp_path / "ipc.token"
    planted = b"11" * 32
    token_path.write_bytes(planted)
    new_token = b"\x42" * 32
    protected = []
    checked_dirs = []
    checked_files = []
    monkeypatch.setattr(autostart.secrets, "token_bytes", lambda count: new_token)
    monkeypatch.setattr(
        autostart,
        "assert_no_reparse_points",
        lambda path: Path(path),
    )
    monkeypatch.setattr(
        autostart,
        "ensure_ipc_token_access",
        lambda path, sid: protected.append((Path(path), sid)) or Path(path),
    )
    monkeypatch.setattr(
        autostart,
        "require_protected_directory",
        lambda path, sid=None: checked_dirs.append((Path(path), sid)) or Path(path),
    )
    monkeypatch.setattr(
        autostart,
        "require_protected_file",
        lambda path, sid=None: checked_files.append((Path(path), sid)) or Path(path),
    )

    assert autostart._rotate_ipc_token(token_path, SID_B) == new_token
    assert token_path.read_text("ascii") == new_token.hex()
    assert token_path.read_bytes() != planted
    assert checked_dirs[-1] == (tmp_path, SID_B)
    assert checked_files[-1] == (token_path, SID_B)

    autostart._restore_ipc_token(token_path, planted, SID_A)
    assert token_path.read_bytes() == planted
    assert protected[-1][1] == SID_A
    assert checked_dirs[-1] == (tmp_path, SID_A)
    assert checked_files[-1] == (token_path, SID_A)


def test_task_staging_preserves_machine_directory_reader(monkeypatch, tmp_path):
    data_dir = tmp_path / "machine"
    data_dir.mkdir()
    token = data_dir / "ipc.token"
    token.write_text("00" * 32, encoding="ascii")
    required = []
    protected = []
    monkeypatch.setattr(autostart, "machine_data_dir", lambda: data_dir)
    monkeypatch.setattr(autostart, "ipc_token_path", lambda: token)
    monkeypatch.setattr(autostart, "protected_file_reader_sid", lambda _path: SID_A)
    monkeypatch.setattr(
        autostart,
        "require_protected_directory",
        lambda path, sid=None: required.append((Path(path), sid)) or Path(path),
    )
    monkeypatch.setattr(
        autostart,
        "ensure_protected_directory",
        lambda path: Path(path).mkdir(parents=True, exist_ok=True) or Path(path),
    )
    monkeypatch.setattr(
        autostart,
        "protect_machine_file",
        lambda path: protected.append(Path(path)) or Path(path),
    )
    monkeypatch.setattr(
        autostart,
        "require_protected_file",
        lambda path, sid=None: Path(path),
    )

    with autostart._protected_task_file("<Task/>") as staged:
        assert staged.is_file()
        assert staged in protected
    assert required[0] == (data_dir, SID_A)
    assert not staged.exists()


def test_install_manifest_preserves_machine_directory_reader(monkeypatch, tmp_path):
    target = tmp_path / "NeedZeroXtraTrash.exe"
    target.write_bytes(b"signed")
    manifest = tmp_path / "install.json"
    required = []
    monkeypatch.setattr(autostart, "install_manifest_path", lambda: manifest)
    monkeypatch.setattr(
        autostart,
        "ensure_protected_directory",
        lambda _path: pytest.fail("manifest reset the machine directory DACL"),
    )
    monkeypatch.setattr(
        autostart,
        "require_protected_directory",
        lambda path, sid=None: required.append((Path(path), sid)) or Path(path),
    )
    monkeypatch.setattr(autostart, "assert_no_reparse_points", lambda path: Path(path))
    monkeypatch.setattr(autostart, "protect_machine_file", lambda path: Path(path))
    monkeypatch.setattr(autostart, "require_protected_file", lambda path: Path(path))

    autostart._write_install_manifest(target, SID_A)

    assert required == [(tmp_path, SID_A)]
    assert manifest.is_file()


def test_schtasks_and_powershell_ignore_path_and_cwd_planting(monkeypatch, tmp_path):
    planted = tmp_path / "schtasks.exe"
    planted.write_text("not executable", encoding="ascii")
    trusted = Path(r"C:\Windows\System32")
    calls = []
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(autostart, "system_directory", lambda: trusted)
    monkeypatch.setattr(
        autostart, "system_executable", lambda name: trusted / name
    )
    monkeypatch.setattr(
        autostart,
        "windows_powershell_executable",
        lambda: trusted / "WindowsPowerShell" / "v1.0" / "powershell.exe",
    )
    monkeypatch.setattr(autostart, "sanitized_system_env", lambda: {"PATH": str(trusted)})

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        stdout = "4" if "powershell.exe" in command[0].lower() else "ok"
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(autostart, "_TASK_RUNNER", runner)
    assert autostart._schtasks(["/Query"]).returncode == 0
    task_name = "NeedZeroXtraTrash Controller"
    assert autostart._task_state(task_name) == 4
    assert calls[0][0][0] == str(trusted / "schtasks.exe")
    assert calls[1][0][0] == str(
        trusted / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    assert all(call[1]["cwd"] == str(trusted) for call in calls)
    assert calls[0][1]["env"] == {"PATH": str(trusted)}
    assert calls[1][1]["env"] == {
        "PATH": str(trusted), autostart._TASK_NAME_ENV: task_name,
    }
    assert calls[1][0][-1] == autostart._TASK_STATE_SCRIPT
    assert task_name not in calls[1][0]


def test_frozen_lhm_ignores_env_cwd_and_explicit_override(monkeypatch, tmp_path):
    bundle = tmp_path / "bundle"
    planted = tmp_path / "planted"
    (bundle / "lib").mkdir(parents=True)
    planted.mkdir()
    (planted / "LibreHardwareMonitorLib.dll").write_bytes(b"evil")
    monkeypatch.chdir(planted)
    monkeypatch.setenv("NZXT_LHM_DLL", str(planted))
    monkeypatch.setattr(lhm.sys, "frozen", True, raising=False)
    monkeypatch.setattr(lhm.sys, "_MEIPASS", str(bundle), raising=False)
    assert lhm._candidate_dirs(planted) == [bundle / "lib"]
    assert lhm._find_dll(planted) is None


def test_controller_guard_uses_protected_global_mutex(monkeypatch):
    handles = iter([1234, None])
    monkeypatch.setattr(
        single_instance,
        "_create_protected_controller_mutex",
        lambda name: next(handles),
    )
    closed = []
    monkeypatch.setattr(
        single_instance,
        "_kernel32",
        lambda: SimpleNamespace(CloseHandle=lambda handle: closed.append(handle)),
    )
    first = single_instance.SingleInstance(r"Global\NeedZeroXtraTrash.Controller")
    second = single_instance.SingleInstance(r"Global\NeedZeroXtraTrash.Controller")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert closed == [1234]
    assert "SY" in single_instance._CONTROLLER_MUTEX_SDDL
    assert "BA" in single_instance._CONTROLLER_MUTEX_SDDL


def test_release_controller_guard_uses_protected_programdata_file(monkeypatch, tmp_path):
    lock_path = tmp_path / "controller.lock"
    lock_path.touch()
    handles = iter([9876, None])
    monkeypatch.setattr(needzero, "BUILD_CHANNEL", "release")
    monkeypatch.setattr(
        single_instance, "_prepare_protected_controller_lock", lambda: lock_path
    )
    monkeypatch.setattr(
        single_instance, "_open_exclusive_controller_file", lambda _path: next(handles)
    )
    monkeypatch.setattr(
        single_instance,
        "_create_protected_controller_mutex",
        lambda _name: pytest.fail("release controller used the squattable mutex path"),
    )
    closed = []
    monkeypatch.setattr(
        single_instance,
        "_kernel32",
        lambda: SimpleNamespace(CloseHandle=lambda handle: closed.append(handle)),
    )
    first = single_instance.SingleInstance(r"Global\NeedZeroXtraTrash.Controller")
    second = single_instance.SingleInstance(r"Global\NeedZeroXtraTrash.Controller")
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert closed == [9876]


def test_release_lock_file_is_created_only_below_verified_programdata(
    monkeypatch, tmp_path
):
    directory = tmp_path / "machine"
    directory.mkdir()
    token = directory / "ipc.token"
    token.write_text("00" * 32, encoding="ascii")
    protected = []
    checked = []
    monkeypatch.setattr(runtime_paths, "machine_dir", lambda: directory)
    monkeypatch.setattr(runtime_paths, "ipc_token_path", lambda: token)
    monkeypatch.setattr(
        windows_security, "protected_file_reader_sid", lambda _path: SID_A
    )
    monkeypatch.setattr(
        windows_security,
        "require_protected_directory",
        lambda path, sid: checked.append((Path(path), sid)) or Path(path),
    )
    monkeypatch.setattr(
        windows_security,
        "protect_machine_file",
        lambda path: protected.append(Path(path)) or Path(path),
    )
    monkeypatch.setattr(
        windows_security,
        "require_protected_file",
        lambda path: checked.append((Path(path), None)) or Path(path),
    )
    monkeypatch.setattr(
        windows_security, "assert_no_reparse_points", lambda path: Path(path)
    )
    lock = single_instance._prepare_protected_controller_lock()
    assert lock == directory / "controller.lock"
    assert lock.is_file()
    assert protected == [lock]
    assert checked == [(directory, SID_A), (lock, None)]


def test_new_controller_start_requires_authenticated_readiness(monkeypatch):
    token = b"n" * 32
    observed = []
    monkeypatch.setattr(
        autostart,
        "_schtasks",
        lambda args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(autostart, "_wait_task_running", lambda *_args, **_kw: True)
    monkeypatch.setattr(
        autostart,
        "_wait_controller_ready",
        lambda value, timeout=autostart._CONTROLLER_READY_TIMEOUT: observed.append(value),
    )
    autostart._start_controller_task(token)
    assert observed == [token]


def test_authenticated_readiness_uses_new_token(monkeypatch):
    token = b"r" * 32
    calls = []

    class Client:
        def __init__(self, value, timeout):
            calls.append((value, timeout))

        def call(self, method):
            calls.append(method)
            return {"controller": "ready"}

    monkeypatch.setattr(ipc, "IpcClient", Client)
    monkeypatch.setattr(
        autostart, "_task_state", lambda _name: autostart._TASK_STATE_RUNNING
    )
    autostart._wait_controller_ready(token, timeout=1.0)
    assert calls[0][0] == token
    assert calls[1] == "get_state"


def test_safe_stop_uses_seventy_second_controller_budget(monkeypatch):
    token = b"s" * 32
    observed = []

    class Client:
        def __init__(self, value, timeout):
            observed.append((value, timeout))

        def call(self, method):
            observed.append(method)
            return True

    monkeypatch.setattr(ipc, "IpcClient", Client)
    monkeypatch.setattr(autostart, "_wait_task_stopped", lambda *_a, **_kw: True)
    autostart._request_safe_controller_stop(token)
    assert observed[0] == (token, 70.0)
    assert observed[1] == "shutdown_controller"


def test_safe_stop_accepts_legacy_early_close_only_after_task_exit(monkeypatch):
    token = b"l" * 32

    class LegacyClient:
        def __init__(self, value, timeout):
            assert value == token
            assert timeout == 70.0

        def call(self, method):
            assert method == "shutdown_controller"
            raise ipc.IpcError("connection_closed", "Controller connection closed early")

    monkeypatch.setattr(ipc, "IpcClient", LegacyClient)
    monkeypatch.setattr(autostart, "_wait_task_stopped", lambda *_a, **_kw: True)
    autostart._request_safe_controller_stop(token)


def test_safe_stop_rejects_early_close_while_task_is_running(monkeypatch):
    class BrokenClient:
        def __init__(self, _value, timeout):
            assert timeout == 70.0
            pass

        def call(self, _method):
            raise ipc.IpcError("connection_closed", "Controller connection closed early")

    monkeypatch.setattr(ipc, "IpcClient", BrokenClient)
    monkeypatch.setattr(autostart, "_wait_task_stopped", lambda *_a, **_kw: False)
    with pytest.raises(ipc.IpcError, match="closed early"):
        autostart._request_safe_controller_stop(b"l" * 32)


def test_identical_secure_install_is_a_strict_noop(monkeypatch, tmp_path):
    source, target = tmp_path / "source.exe", tmp_path / "installed.exe"
    source.write_bytes(b"signed")
    target.write_bytes(b"signed")
    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    monkeypatch.setattr(autostart, "_current_exe", lambda: source)
    monkeypatch.setattr(autostart, "_installed_exe", lambda: target)
    monkeypatch.setattr(autostart, "_assert_release_binary", lambda _path: None)
    monkeypatch.setattr(autostart, "assert_no_reparse_points", lambda path: Path(path))
    monkeypatch.setattr(autostart, "current_user_sid", lambda: SID_B)
    controller_xml = autostart.controller_task_xml(target)
    ui_xml = autostart.ui_task_xml(target, SID_B)
    monkeypatch.setattr(
        autostart,
        "_query_task",
        lambda name: controller_xml if name == autostart.CONTROLLER_TASK_NAME else ui_xml,
    )
    monkeypatch.setattr(
        autostart,
        "_list_ui_task_definitions",
        lambda: {autostart._ui_task_name(SID_B): ui_xml},
    )
    monkeypatch.setattr(autostart, "_secure_install_is_identical", lambda **_kw: True)
    monkeypatch.setattr(
        autostart,
        "_install_protected_copy",
        lambda *_args: pytest.fail("identical install copied or interrupted the controller"),
    )
    autostart.install_elevated()


def test_install_cleans_all_owned_old_sid_ui_tasks(monkeypatch, tmp_path):
    source, target = tmp_path / "source.exe", tmp_path / "installed.exe"
    source.write_bytes(b"signed-new")
    target.write_bytes(b"signed-old")
    token_path = tmp_path / "ipc.token"
    manifest_path = tmp_path / "install.json"
    config_path = tmp_path / "config.json"
    tasks = {
        autostart._ui_task_name(SID_A): autostart.ui_task_xml(target, SID_A),
        autostart._ui_task_name(SID_B): autostart.ui_task_xml(target, SID_B),
        autostart._ui_task_name(SID_C): autostart.ui_task_xml(target, SID_C).replace(
            "<Source>NeedZeroXtraTrash</Source>", "<Source>SomeoneElse</Source>"
        ),
    }
    started = []
    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    monkeypatch.setattr(autostart, "_current_exe", lambda: source)
    monkeypatch.setattr(autostart, "_installed_exe", lambda: target)
    monkeypatch.setattr(autostart, "ipc_token_path", lambda: token_path)
    monkeypatch.setattr(autostart, "install_manifest_path", lambda: manifest_path)
    monkeypatch.setattr(autostart, "machine_config_path", lambda: config_path)
    monkeypatch.setattr(autostart, "machine_data_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "roaming_appdata_directory", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_assert_release_binary", lambda _path: None)
    monkeypatch.setattr(autostart, "authenticode_is_valid", lambda _path: True)
    monkeypatch.setattr(autostart, "assert_no_reparse_points", lambda path: Path(path))
    monkeypatch.setattr(autostart, "current_user_sid", lambda: SID_B)
    monkeypatch.setattr(
        autostart, "_query_task", lambda name: tasks.get(name)
    )
    monkeypatch.setattr(
        autostart,
        "_list_ui_task_definitions",
        lambda: {name: xml for name, xml in tasks.items() if name.startswith(autostart.UI_TASK_PREFIX)},
    )
    monkeypatch.setattr(autostart, "_secure_install_is_identical", lambda **_kw: False)
    monkeypatch.setattr(autostart, "ensure_machine_data_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "ensure_install_dir", lambda: tmp_path)
    monkeypatch.setattr(
        autostart, "_install_protected_copy", lambda *_args: (target, None)
    )
    monkeypatch.setattr(autostart, "_rotate_ipc_token", lambda *_args: b"n" * 32)
    monkeypatch.setattr(autostart, "_validate_and_protect_machine_config", lambda: None)
    monkeypatch.setattr(autostart, "_write_install_manifest", lambda *_args: None)
    monkeypatch.setattr(
        autostart,
        "_delete_task",
        lambda name, require_success=True: tasks.pop(name, None),
    )
    monkeypatch.setattr(
        autostart, "_create_task", lambda name, xml: tasks.__setitem__(name, xml)
    )
    monkeypatch.setattr(
        autostart, "_start_controller_task", lambda token: started.append(token)
    )
    from needzero import config as config_module

    monkeypatch.setattr(config_module, "migrate_config", lambda *_args: None)
    autostart.install_elevated()
    assert autostart._ui_task_name(SID_A) not in tasks
    assert autostart._ui_task_name(SID_B) in tasks
    assert autostart._ui_task_name(SID_C) in tasks  # foreign task is untouched
    assert autostart.CONTROLLER_TASK_NAME in tasks
    assert started == [b"n" * 32]


def test_upgrade_safe_stops_before_binary_replace(monkeypatch, tmp_path):
    source, target = tmp_path / "source.exe", tmp_path / "installed.exe"
    source.write_bytes(b"signed-new")
    target.write_bytes(b"signed-old")
    token_path = tmp_path / "ipc.token"
    old_token = b"o" * 32
    token_path.write_text(old_token.hex(), encoding="ascii")
    controller_xml = autostart.controller_task_xml(target)
    ui_xml = autostart.ui_task_xml(target, SID_A)
    order = []

    monkeypatch.setattr(autostart, "is_elevated", lambda: True)
    monkeypatch.setattr(autostart, "_current_exe", lambda: source)
    monkeypatch.setattr(autostart, "_installed_exe", lambda: target)
    monkeypatch.setattr(autostart, "ipc_token_path", lambda: token_path)
    monkeypatch.setattr(autostart, "install_manifest_path", lambda: tmp_path / "manifest")
    monkeypatch.setattr(autostart, "machine_config_path", lambda: tmp_path / "config")
    monkeypatch.setattr(autostart, "machine_data_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_assert_release_binary", lambda _path: None)
    monkeypatch.setattr(autostart, "authenticode_is_valid", lambda _path: True)
    monkeypatch.setattr(autostart, "assert_no_reparse_points", lambda path: Path(path))
    monkeypatch.setattr(autostart, "current_user_sid", lambda: SID_A)
    monkeypatch.setattr(
        autostart,
        "_query_task",
        lambda name: controller_xml if name == autostart.CONTROLLER_TASK_NAME else ui_xml,
    )
    monkeypatch.setattr(
        autostart,
        "_list_ui_task_definitions",
        lambda: {autostart._ui_task_name(SID_A): ui_xml},
    )
    monkeypatch.setattr(autostart, "_secure_install_is_identical", lambda **_kw: False)
    monkeypatch.setattr(
        autostart, "_task_state", lambda _name: autostart._TASK_STATE_RUNNING
    )
    monkeypatch.setattr(autostart, "protected_file_reader_sid", lambda _path: SID_A)
    monkeypatch.setattr(autostart, "require_protected_directory", lambda *args: args[0])
    monkeypatch.setattr(autostart, "require_protected_file", lambda *args: args[0])
    monkeypatch.setattr(
        autostart,
        "_request_safe_controller_stop",
        lambda token: order.append(("safe-stop", token)),
    )
    monkeypatch.setattr(autostart, "ensure_machine_data_dir", lambda: tmp_path)
    monkeypatch.setattr(autostart, "ensure_install_dir", lambda: tmp_path)

    class CopyFailed(RuntimeError):
        pass

    def fail_copy(*_args):
        order.append(("copy", None))
        raise CopyFailed("copy failed")

    monkeypatch.setattr(autostart, "_install_protected_copy", fail_copy)
    monkeypatch.setattr(autostart, "_restore_ipc_token", lambda *_args: None)
    monkeypatch.setattr(
        autostart,
        "_start_controller_task",
        lambda token: order.append(("restart-old", token)),
    )
    with pytest.raises(CopyFailed):
        autostart.install_elevated()
    assert order == [
        ("safe-stop", old_token),
        ("copy", None),
        ("restart-old", old_token),
    ]


def test_controller_logging_preserves_reader_and_reprotects_rotations(
    monkeypatch, tmp_path
):
    path = tmp_path / "controller.log"
    token = tmp_path / "ipc.token"
    token.write_text("00" * 32, encoding="ascii")
    required = []
    protected = []
    monkeypatch.setattr(controller_logging, "controller_log_path", lambda: path)
    monkeypatch.setattr(controller_logging, "ipc_token_path", lambda: token)
    monkeypatch.setattr(
        controller_logging, "protected_file_reader_sid", lambda _path: SID_A
    )
    monkeypatch.setattr(
        controller_logging,
        "require_protected_directory",
        lambda directory, sid: required.append((Path(directory), sid)) or Path(directory),
    )
    monkeypatch.setattr(
        controller_logging,
        "ensure_protected_directory",
        lambda _path: pytest.fail("logger reset the machine directory ACL"),
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, controller_logging._HANDLER_MARKER, False):
            root.removeHandler(existing)
            existing.close()
    handler = controller_logging.configure_controller_logging(
        max_bytes=32,
        backup_count=2,
        file_protector=lambda candidate: protected.append(Path(candidate)) or Path(candidate),
        path_checker=lambda candidate: Path(candidate),
    )
    try:
        for index in range(4):
            handler.emit(logging.LogRecord(
                "needzero.test", logging.INFO, __file__, 1, f"message-{index}-xxxxxxxx", (), None
            ))
        existing_files = {candidate for candidate in (path, Path(f"{path}.1"), Path(f"{path}.2")) if candidate.exists()}
        assert existing_files
        assert existing_files.issubset(set(protected))
        assert required == [(tmp_path, SID_A)]
    finally:
        root.removeHandler(handler)
        handler.close()
