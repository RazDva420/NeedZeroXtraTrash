from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from needzero.config import ConfigError
from needzero.ipc import (
    Dispatcher, IpcClient, IpcError, IpcServer, MAX_MESSAGE_BYTES,
    ReloadingIpcClient, _client_authenticate, _recv_exact,
)


class FakeApi:
    def get_state(self):
        return {"running": True}

    def set_mode(self, mode):
        if mode not in {"silent", "balanced", "performance"}:
            raise ConfigError("bad mode", {"mode": "unsupported"})
        return {"mode": mode}


@pytest.fixture
def local_server():
    token = b"x" * 32
    stop = threading.Event()
    server = IpcServer(Dispatcher(FakeApi(), token), ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, args=(stop,))
    thread.start()
    try:
        yield token, server.address
    finally:
        stop.set()
        server.close()
        thread.join(2)


def test_authenticated_narrow_call(local_server) -> None:
    token, address = local_server
    assert IpcClient(token, address).call("get_state") == {"running": True}


def test_wrong_token_is_rejected(local_server) -> None:
    _token, address = local_server
    with pytest.raises(IpcError) as caught:
        IpcClient(b"z" * 32, address).call("get_state")
    assert caught.value.code == "unauthorized"


def test_paths_and_unknown_methods_cannot_cross_boundary(local_server) -> None:
    token, address = local_server
    with pytest.raises(IpcError) as caught:
        IpcClient(token, address).call("load_dll", r"C:\\Users\\Public\\evil.dll")
    assert caught.value.code == "method_not_allowed"


def test_structured_validation_error(local_server) -> None:
    token, address = local_server
    with pytest.raises(IpcError) as caught:
        IpcClient(token, address).call("set_mode", "turbo")
    assert caught.value.code == "validation_error"
    assert caught.value.fields == {"mode": "unsupported"}


def test_oversized_message_is_rejected_before_json_decode(local_server) -> None:
    token, address = local_server
    with socket.create_connection(address, timeout=1) as sock:
        _client_authenticate(sock, token)
        sock.sendall(struct.pack("!I", MAX_MESSAGE_BYTES + 1))
        size = struct.unpack("!I", sock.recv(4))[0]
        response = sock.recv(size).decode("utf-8")
    assert "message_size" in response


def test_fake_server_cannot_collect_token_or_application_request() -> None:
    token = b"k" * 32
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    observed = []

    def fake_server():
        connection, _peer = listener.accept()
        with connection:
            connection.settimeout(1)
            connection.sendall(b"n" * 32)
            observed.append(_recv_exact(connection, 32))  # one-way HMAC, not the token
            observed.append(_recv_exact(connection, 32))  # client challenge
            connection.sendall(b"0" * 32)        # invalid server proof
            observed.append(connection.recv(1024))

    thread = threading.Thread(target=fake_server)
    thread.start()
    try:
        with pytest.raises(IpcError) as caught:
            IpcClient(token, listener.getsockname()).call("get_state")
        assert caught.value.code == "unauthorized"
    finally:
        thread.join(2)
        listener.close()

    assert observed[0] != token
    assert len(observed[0]) == len(observed[1]) == 32
    assert observed[2] == b""  # no JSON command was sent before server proof


def test_reloading_client_recovers_when_token_appears_late(tmp_path) -> None:
    token = b"r" * 32
    stop = threading.Event()
    server = IpcServer(Dispatcher(FakeApi(), token), ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, args=(stop,))
    thread.start()
    token_path = tmp_path / "ipc.token"
    client = ReloadingIpcClient(token_path, server.address)
    try:
        with pytest.raises(IpcError) as caught:
            client.call("get_state")
        assert caught.value.code == "token_unavailable"
        token_path.write_text(token.hex(), encoding="ascii")
        assert client.call("get_state") == {"running": True}
    finally:
        stop.set()
        server.close()
        thread.join(2)


def test_slow_unauthenticated_peer_does_not_starve_valid_client(local_server) -> None:
    token, address = local_server
    slow = socket.create_connection(address, timeout=1)
    try:
        assert len(_recv_exact(slow, 32)) == 32
        started = time.monotonic()
        assert IpcClient(token, address).call("get_state") == {"running": True}
        # A stalled unauthenticated peer must not serialize valid clients. Keep
        # enough scheduling slack for loaded CI/VM hosts while remaining close
        # to the server's one-second authentication deadline.
        assert time.monotonic() - started < 1.5
    finally:
        slow.close()


def test_second_server_cannot_bind_controller_address(local_server) -> None:
    token, address = local_server
    with pytest.raises(OSError):
        IpcServer(Dispatcher(FakeApi(), token), address)
