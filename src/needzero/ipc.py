"""Authenticated, length-bounded local IPC for the controller service."""

from __future__ import annotations

import hmac
import hashlib
import json
import os
import secrets
import socket
import struct
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .config import ConfigError


LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 47632
MAX_MESSAGE_BYTES = 64 * 1024
SOCKET_TIMEOUT_SECONDS = 3.0
AUTH_TIMEOUT_SECONDS = 1.0
AUTH_NONCE_BYTES = 32
MAX_CONCURRENT_CLIENTS = 8

# Deliberately narrow: controller IPC never accepts filesystem or executable
# paths.  Window/autostart lifecycle operations stay in the unelevated client.
_METHOD_ARITY = {
    "get_state": (0, 0),
    "get_config": (0, 0),
    "list_sensors": (0, 0),
    "set_curve": (2, 2),
    "set_source": (2, 2),
    "set_mode": (1, 1),
    "apply_settings": (1, 1),
    "reset_defaults": (0, 0),
    "apply_now": (0, 0),
    "test_full": (0, 1),
    "get_autostart": (0, 0),
    # Used only by the elevated uninstaller. Authentication still requires the
    # protected per-installation token, and no path/process arguments cross
    # the privilege boundary.
    "shutdown_controller": (0, 0),
}


class IpcError(RuntimeError):
    def __init__(self, code: str, message: str, fields: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields or {}


def read_token(path: Path) -> bytes:
    try:
        text = Path(path).read_text(encoding="ascii").strip()
        token = bytes.fromhex(text)
    except (OSError, ValueError) as exc:
        raise IpcError("token_unavailable", "Controller authentication is unavailable") from exc
    if len(token) != 32:
        raise IpcError("token_invalid", "Controller authentication token is invalid")
    return token


def create_token(path: Path, secure: Optional[Callable[[Path], None]] = None) -> bytes:
    """Create a 256-bit token once; the caller supplies the Windows ACL step."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_bytes(32)
    try:
        with path.open("x", encoding="ascii") as fh:
            fh.write(token.hex())
            fh.flush()
    except FileExistsError:
        return read_token(path)
    if secure is not None:
        try:
            secure(path)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
    return token


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise IpcError("connection_closed", "Controller connection closed early")
        chunks.extend(part)
    return bytes(chunks)


def _recv_message(sock: socket.socket) -> dict:
    size = struct.unpack("!I", _recv_exact(sock, 4))[0]
    if size <= 0 or size > MAX_MESSAGE_BYTES:
        raise IpcError("message_size", "IPC message size is invalid")
    try:
        value = json.loads(_recv_exact(sock, size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IpcError("invalid_json", "IPC message is not valid JSON") from exc
    if not isinstance(value, dict):
        raise IpcError("invalid_request", "IPC request must be an object")
    return value


def _send_message(sock: socket.socket, value: dict) -> None:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise IpcError("message_size", "IPC response is too large")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _proof(token: bytes, role: bytes, nonce: bytes) -> bytes:
    return hmac.new(token, role + nonce, hashlib.sha256).digest()


def _server_authenticate(sock: socket.socket, token: bytes) -> None:
    server_nonce = secrets.token_bytes(AUTH_NONCE_BYTES)
    sock.sendall(server_nonce)
    client_proof = _recv_exact(sock, hashlib.sha256().digest_size)
    if not hmac.compare_digest(client_proof, _proof(token, b"client:", server_nonce)):
        raise IpcError("unauthorized", "Controller client authentication failed")
    client_nonce = _recv_exact(sock, AUTH_NONCE_BYTES)
    sock.sendall(_proof(token, b"server:", client_nonce))


def _client_authenticate(sock: socket.socket, token: bytes) -> None:
    try:
        server_nonce = _recv_exact(sock, AUTH_NONCE_BYTES)
        sock.sendall(_proof(token, b"client:", server_nonce))
        client_nonce = secrets.token_bytes(AUTH_NONCE_BYTES)
        sock.sendall(client_nonce)
        server_proof = _recv_exact(sock, hashlib.sha256().digest_size)
    except (OSError, IpcError) as exc:
        raise IpcError("unauthorized", "Controller server authentication failed") from exc
    if not hmac.compare_digest(server_proof, _proof(token, b"server:", client_nonce)):
        raise IpcError("unauthorized", "Controller server authentication failed")


class Dispatcher:
    def __init__(self, api: Any, token: bytes) -> None:
        if len(token) != 32:
            raise ValueError("IPC token must contain 32 bytes")
        self._api = api
        self.token = token

    def dispatch(self, request: dict) -> dict:
        method = request.get("method")
        args = request.get("args", [])
        if not isinstance(method, str) or method not in _METHOD_ARITY:
            raise IpcError("method_not_allowed", "Controller command is not allowed")
        if not isinstance(args, list):
            raise IpcError("invalid_args", "Controller command arguments must be a list")
        minimum, maximum = _METHOD_ARITY[method]
        if not minimum <= len(args) <= maximum:
            raise IpcError("invalid_args", "Controller command argument count is invalid")
        target = getattr(self._api, method, None)
        if target is None or not callable(target):
            raise IpcError("method_unavailable", "Controller command is unavailable")
        return {"ok": True, "result": target(*args)}


def _error_response(exc: Exception) -> dict:
    if isinstance(exc, IpcError):
        code, fields = exc.code, exc.fields
    elif isinstance(exc, ConfigError):
        code, fields = "validation_error", exc.fields
    elif isinstance(exc, OSError):
        code, fields = "persistence_error", {}
    else:
        code, fields = "internal_error", {}
    return {
        "ok": False,
        "error": {"code": code, "message": str(exc) or code, "fields": fields},
    }


class IpcServer:
    def __init__(
        self,
        dispatcher: Dispatcher,
        address: Tuple[str, int] = (LOOPBACK, DEFAULT_PORT),
    ) -> None:
        if address[0] != LOOPBACK:
            raise ValueError("controller IPC must bind to IPv4 loopback")
        self._dispatcher = dispatcher
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            self._socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(address)
        self._socket.listen(8)
        self._socket.settimeout(0.5)
        self.address = self._socket.getsockname()
        self._closed = False
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_CLIENTS)
        self._workers: set[threading.Thread] = set()
        self._connections: set[socket.socket] = set()
        self._worker_lock = threading.Lock()

    def serve_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                connection, peer = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._closed:
                    return
                raise
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            worker = threading.Thread(
                target=self._serve_connection,
                args=(connection, peer),
                name="controller-ipc-client",
                daemon=True,
            )
            with self._worker_lock:
                self._workers.add(worker)
                self._connections.add(connection)
            worker.start()

    def _serve_connection(self, connection: socket.socket, peer) -> None:
        try:
            with connection:
                connection.settimeout(AUTH_TIMEOUT_SECONDS)
                try:
                    if peer[0] != LOOPBACK:
                        raise IpcError("unauthorized", "Non-local controller client rejected")
                    _server_authenticate(connection, self._dispatcher.token)
                except Exception:
                    return
                connection.settimeout(SOCKET_TIMEOUT_SECONDS)
                try:
                    response = self._dispatcher.dispatch(_recv_message(connection))
                except Exception as exc:  # converted to a structured client error
                    response = _error_response(exc)
                try:
                    _send_message(connection, response)
                except (OSError, IpcError):
                    pass
        finally:
            with self._worker_lock:
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
            self._slots.release()

    def close(self) -> None:
        self._closed = True
        self._socket.close()
        with self._worker_lock:
            connections = list(self._connections)
            workers = list(self._workers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(1.5)


class IpcClient:
    def __init__(
        self,
        token: bytes,
        address: Tuple[str, int] = (LOOPBACK, DEFAULT_PORT),
        timeout: float = SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        self._token = token
        self._address = address
        self._timeout = timeout

    def call(self, method: str, *args: object) -> Any:
        request = {"method": method, "args": list(args)}
        try:
            with socket.create_connection(self._address, timeout=self._timeout) as sock:
                sock.settimeout(self._timeout)
                _client_authenticate(sock, self._token)
                _send_message(sock, request)
                response = _recv_message(sock)
        except IpcError:
            raise
        except OSError as exc:
            raise IpcError("controller_unavailable", "Controller is not reachable") from exc
        if response.get("ok") is True:
            return response.get("result")
        raw_error = response.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        raise IpcError(
            str(error.get("code") or "remote_error"),
            str(error.get("message") or "Controller request failed"),
            error.get("fields") if isinstance(error.get("fields"), dict) else {},
        )


class ReloadingIpcClient:
    """Reload the protected token so UI boot can recover after late install/start."""

    def __init__(
        self,
        token_path: Path,
        address: Tuple[str, int] = (LOOPBACK, DEFAULT_PORT),
        timeout: float = SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        self._token_path = Path(token_path)
        self._address = address
        self._timeout = timeout
        self._token: Optional[bytes] = None
        self._client: Optional[IpcClient] = None
        self._lock = threading.Lock()

    def call(self, method: str, *args: object) -> Any:
        token = read_token(self._token_path)
        with self._lock:
            if self._client is None or token != self._token:
                self._token = token
                self._client = IpcClient(token, self._address, self._timeout)
            client = self._client
        return client.call(method, *args)
