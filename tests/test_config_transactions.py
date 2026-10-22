from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from needzero import config as cfgmod
from needzero.api import Api
from needzero.config_store import ConfigStore
from needzero.controlloop import Controller
from needzero.controller_service import _publish_config
from needzero.fans.mock import MockFanBackend
from needzero.sensors.mock import MockSensorBackend


def test_save_failure_does_not_publish_or_change_store(tmp_path: Path) -> None:
    initial = cfgmod.Config.defaults()
    published = []

    def fail_save(_cfg, _path):
        raise OSError("disk full")

    store = ConfigStore(
        tmp_path / "config.json",
        initial,
        save_fn=fail_save,
        publish_fn=lambda cfg: published.append(cfg.min_duty),
    )

    with pytest.raises(OSError, match="disk full"):
        store.transaction(lambda cfg: setattr(cfg, "min_duty", 55))

    current, revision, _warning = store.get()
    assert current.min_duty == initial.min_duty
    assert revision == 0
    assert published == []


def test_parallel_transactions_do_not_lose_updates(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    initial = cfgmod.Config.defaults()
    cfgmod.save(initial, path)
    store = ConfigStore(path, initial)
    barrier = threading.Barrier(3)

    def set_minimum():
        barrier.wait()
        store.transaction(lambda cfg: setattr(cfg, "min_duty", 37))

    def set_poll():
        barrier.wait()
        store.transaction(lambda cfg: setattr(cfg, "poll_interval", 4))

    first = threading.Thread(target=set_minimum)
    second = threading.Thread(target=set_poll)
    first.start()
    second.start()
    barrier.wait()
    first.join(2)
    second.join(2)

    current, revision, _warning = store.get()
    persisted = cfgmod.load(path)
    assert revision == 2
    assert current.min_duty == persisted.min_duty == 37
    assert current.poll_interval == persisted.poll_interval == 4


def test_broken_config_is_backed_up_and_warning_is_returned(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"sensors": null', encoding="utf-8")

    cfg, warning, backup = cfgmod.load_with_warning(path)

    assert cfg.to_dict() == cfgmod.Config.defaults().to_dict()
    assert warning and "Invalid config moved" in warning
    assert backup is not None and backup.read_text(encoding="utf-8") == '{"sensors": null'
    assert not path.exists()


def test_non_object_config_is_backed_up_instead_of_silently_reset(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("null", encoding="utf-8")

    _config, warning, backup = cfgmod.load_with_warning(path)

    assert warning and "configuration root" in warning
    assert backup is not None and backup.read_text(encoding="utf-8") == "null"
    assert not path.exists()


def test_broken_config_backup_failure_is_fatal_and_preserves_original(
        tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    original = '{"sensors": null'
    path.write_text(original, encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("backup destination unavailable")

    monkeypatch.setattr(cfgmod.os, "replace", fail_replace)
    with pytest.raises(cfgmod.ConfigReadError, match="could not be preserved"):
        cfgmod.load_with_warning(path)

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("config.json.broken-*")) == []


def test_read_error_is_fatal_and_existing_path_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.mkdir()
    marker = path / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(cfgmod.ConfigReadError):
        cfgmod.load_with_warning(path)

    assert marker.read_text(encoding="utf-8") == "preserve"


def test_machine_prepare_failure_happens_before_atomic_config_replace(
        tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = b'{"sentinel":"old"}'
    path.write_bytes(original)
    prepared = []

    def reject_staged(staged: Path) -> None:
        prepared.append(staged)
        assert staged != path
        assert b'"schema_version"' in staged.read_bytes()
        raise OSError("ACL post-check rejected")

    with pytest.raises(OSError, match="ACL post-check rejected"):
        cfgmod.save(cfgmod.Config.defaults(), path, prepare_temp=reject_staged)

    assert prepared
    assert path.read_bytes() == original
    assert not any(item.suffix == ".tmp" for item in tmp_path.iterdir())


@pytest.mark.parametrize("bad_value", [[], {}, 17, True, ""])
def test_sensor_map_rejects_non_string_values_by_normalizing_to_auto(bad_value) -> None:
    cfg = cfgmod.Config.from_dict({"sensors": {"cpu": bad_value}})
    assert cfg.sensors.cpu is None


def test_legacy_config_migration_keeps_backup(tmp_path: Path) -> None:
    source = tmp_path / "legacy" / "config.json"
    target = tmp_path / "machine" / "config.json"
    cfg = cfgmod.Config.defaults()
    cfg.min_duty = 44
    cfgmod.save(cfg, source)

    backup = cfgmod.migrate_config(source, target)

    assert backup is not None and backup.exists()
    assert cfgmod.load(target).min_duty == 44
    assert json.loads(backup.read_text(encoding="utf-8"))["min_duty"] == 44


def test_parallel_api_mutations_are_serialized_and_persisted(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    controller = Controller(cfgmod.Config.defaults(), MockSensorBackend(), MockFanBackend())
    store = ConfigStore(path, controller.get_config(), publish_fn=controller.set_config)
    api = Api(controller, store=store)
    barrier = threading.Barrier(3)

    def set_front():
        barrier.wait()
        api.set_source("front", "cpu")

    def set_top():
        barrier.wait()
        api.set_source("top", "gpu")

    threads = [threading.Thread(target=set_front), threading.Thread(target=set_top)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)

    assert controller.get_config().groups["front"].source == "cpu"
    assert controller.get_config().groups["top"].source == "gpu"
    assert cfgmod.load(path).groups["front"].source == "cpu"
    assert cfgmod.load(path).groups["top"].source == "gpu"


def test_api_save_error_does_not_change_live_configuration(tmp_path: Path) -> None:
    controller = Controller(cfgmod.Config.defaults(), MockSensorBackend(), MockFanBackend())

    def fail_save(_config, _path):
        raise OSError("read-only disk")

    store = ConfigStore(
        tmp_path / "config.json",
        controller.get_config(),
        save_fn=fail_save,
        publish_fn=controller.set_config,
    )
    api = Api(controller, store=store)

    with pytest.raises(OSError, match="read-only disk"):
        api.set_source("front", "cpu")

    assert controller.get_config().groups["front"].source == "max"


def test_partial_live_publish_is_rolled_back_with_persisted_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    controller = Controller(cfgmod.Config.defaults(), MockSensorBackend(), MockFanBackend())

    class FlakySensorMapping:
        def __init__(self):
            self.cpu = None

        def set_manual_ids(self, sensor_map):
            self.cpu = sensor_map.cpu
            if self.cpu == "/bad/provider":
                raise RuntimeError("provider rejected mapping")

    sensors = FlakySensorMapping()
    cfgmod.save(controller.get_config(), path)
    store = ConfigStore(
        path,
        controller.get_config(),
        publish_fn=lambda candidate: _publish_config(controller, sensors, candidate),
    )

    with pytest.raises(RuntimeError, match="provider rejected"):
        store.transaction(
            lambda candidate: setattr(candidate.sensors, "cpu", "/bad/provider"))

    assert controller.get_config().sensors.cpu is None
    assert sensors.cpu is None
    assert cfgmod.load(path).sensors.cpu is None
