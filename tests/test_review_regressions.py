"""Hardware-free regressions for the 2026-10-18 review."""
import time

import pytest

from needzero.config import Config, SensorMap
from needzero.client_api import ClientApi
from needzero.ipc import IpcError
from needzero.controlloop import Controller, _ChannelState
from needzero.fans.auto import AutoFanBackend
from needzero.fans.base import FanStatus
from needzero.fans.mock import MockFanBackend
from needzero.fans.process import FanProcessError, ProcessFanBackend
from needzero.sensors import _auto_specs, _explicit_spec
from needzero.sensors.base import SensorReading, resolve_temps
from needzero.sensors.deadline import DeadlineSensorBackend
from needzero.sensors.mock import MockSensorBackend


def controller(fans=None):
    return Controller(Config.defaults(), MockSensorBackend(), fans or MockFanBackend())


@pytest.mark.parametrize('duty', [None, 100])
def test_zero_rpm_is_fault_even_with_matching_pwm(duty):
    ctrl = controller()
    state = _ChannelState(desired=100, commanded=100, target_started_at=0)
    ctrl._channels['fan1'] = state
    status = {'fan1': FanStatus('fan1', rpm=0, duty=duty)}
    for now in (0, 1, 4):
        ctrl._verify_status_locked(status, 2, now)
        assert not state.fault
    for now in (6, 8, 10):
        ctrl._verify_status_locked(status, 2, now)
    assert state.fault
    for now in (11, 12, 13):
        ctrl._verify_status_locked({'fan1': FanStatus('fan1', rpm=None, duty=duty)}, 2, now)
    assert state.fault
    for now in (12, 14, 16):
        ctrl._verify_status_locked({'fan1': FanStatus('fan1', rpm=900, duty=duty)}, 2, now)
    assert not state.fault


def test_continuous_fault_does_not_continuously_wake_loop():
    class BrokenFans(MockFanBackend):
        def set_many(self, duties):
            return dict.fromkeys(duties, False)
        def read_status(self):
            raise OSError('disconnected')
    ctrl = controller(BrokenFans())
    for _ in range(5):
        ctrl._wake.clear()
        ctrl._cycle()
    assert ctrl.snapshot()['fan_fault']
    assert not ctrl._wake.is_set()
    assert ctrl._retry_delay(1000000) == 30


@pytest.mark.parametrize('value,age', [(90, 60), (None, 0), (float('nan'), 0)])
def test_invalid_manual_sensor_does_not_switch_to_auto(value, age):
    now = time.monotonic()
    readings = [SensorReading('manual', 'CPU', 'Cpu', 'Tctl', value, sampled_at=now-age),
                SensorReading('auto', 'CPU', 'Cpu', 'CPU Package', 40, sampled_at=now)]
    assert resolve_temps(readings, SensorMap(cpu='manual')).cpu is None


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), -1, 101, 'bad'])
def test_shutdown_rejects_invalid_readback(value):
    assert not Controller._safe_write_confirmed(
        {'fan1': 100}, {'fan1': True}, {'fan1': FanStatus('fan1', duty=value)}, 2)


def test_failed_close_prevents_replacement_and_can_recover():
    owners = []
    class Owner(MockFanBackend):
        stuck = True
        def __init__(self):
            super().__init__()
            owners.append(self)
        def close(self):
            if self.stuck:
                raise FanProcessError('still alive')
        def set_many(self, duties):
            return dict.fromkeys(duties, False)
    backend = AutoFanBackend(Owner)
    backend.open()
    backend.set_many({'fan1':50})
    assert not backend.available
    assert not backend.force_reconnect()
    assert len(owners) == 1
    owners[0].stuck = False
    assert backend.force_reconnect()
    assert len(owners) == 2
    owners[1].stuck = False
    backend.close()


def test_process_close_reports_surviving_owner(monkeypatch):
    backend = ProcessFanBackend()
    monkeypatch.setattr(backend, '_terminate_locked', lambda: False)
    with pytest.raises(FanProcessError):
        backend.close()


def test_failed_open_retains_owner_when_cleanup_fails():
    owners = []
    class Owner:
        def __init__(self):
            owners.append(self)
        def open(self):
            raise OSError('open failed')
        def close(self):
            raise FanProcessError('still alive')
    backend = AutoFanBackend(Owner)
    backend.open()
    assert not backend.force_reconnect()
    assert len(owners) == 1


def test_sensor_retries_backoff_and_reapply_latest_mapping():
    attempts, mappings, delays = [], [], []
    class Stop:
        def is_set(self):
            return len(attempts) >= 4
        def wait(self, delay):
            delays.append(delay)
            return self.is_set()
    class Provider:
        def open(self):
            pass
        def close(self):
            pass
        def set_manual_ids(self, value):
            mappings.append(value.cpu)
        def read_all(self):
            attempts.append(1)
            raise OSError('read failed')
    backend = DeadlineSensorBackend(Provider, manual_ids=SensorMap(cpu='old'))
    backend.set_manual_ids(SensorMap(cpu='new'))
    backend._stop = Stop()
    backend._run()
    assert delays == [1, 2, 4, 8]
    assert mappings == ['new'] * 4


def test_hwinfo_is_removed_and_legacy_config_migrates():
    cfg = Config.from_dict({'sensor_backend':'hwinfo',
                            'sensors':{'cpu':'/hwinfo/1/2', 'gpu':'/gpu/core'}})
    assert cfg.sensor_backend == 'auto'
    assert cfg.sensors.cpu is None
    assert cfg.sensors.gpu == '/gpu/core'
    assert _explicit_spec('hwinfo') is None
    assert all('hwinfo' not in spec.label.lower() for spec in _auto_specs())


@pytest.mark.parametrize('method,args', [
    ('get_state', ()), ('get_config', ()), ('set_curve', ('front', [[0, 20], [100, 100]])),
    ('set_source', ('front', 'cpu')), ('set_mode', ('balanced',)),
    ('apply_settings', ({},)), ('reset_defaults', ()),
])
def test_client_checks_object_response(method, args):
    class Client:
        result = None
        def call(self, name, *params):
            assert (name, params) == (method, args)
            return self.result
    client = Client()
    api = ClientApi(client)
    with pytest.raises(IpcError, match='must be an object'):
        getattr(api, method)(*args)
    client.result = {'ok': True}
    assert getattr(api, method)(*args) == client.result


def test_client_checks_sensor_list():
    class Client:
        result = [None]
        def call(self, method):
            return self.result
    client = Client()
    api = ClientApi(client)
    with pytest.raises(IpcError):
        api.list_sensors()
    client.result = [{'identifier': '/cpu/package'}]
    assert api.list_sensors() == client.result


def test_settings_reject_removed_hwinfo_mapping(tmp_path):
    from needzero.api import Api
    from needzero.config import ConfigError
    ctrl = controller()
    cfg = ctrl.get_config()
    api = Api(ctrl, config_path=tmp_path/'config.json')
    data = {'channels': {name: group.channel for name, group in cfg.groups.items()},
            'min_duty':20, 'poll_interval':2,
            'emergency':{'enabled':True, 'gpu_hotspot_threshold':100, 'duty':100},
            'sensors':{'cpu':'/hwinfo/1/2', 'gpu':None, 'gpu_hotspot':None}}
    with pytest.raises(ConfigError, match='Settings validation failed'):
        api.apply_settings(data)
    assert not (tmp_path/'config.json').exists()
