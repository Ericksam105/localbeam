#!/usr/bin/env python3
"""LocalBeam: a dependency-free, single-use LAN file relay."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import re
import secrets
import shlex
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


APP_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = APP_DIR / "public"
CONFIG_PATH = APP_DIR / "config.json"
CHUNK_SIZE = 1024 * 1024
QUEUE_DEPTH = 8
FINISHED_RETENTION_SECONDS = 30 * 60
MAX_JSON_BYTES = 64 * 1024
STREAM_END = object()
SHORT_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SHORT_CODE_LENGTH = 8


def format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            formatted = f"{amount:.2f}".rstrip("0").rstrip(".")
            return f"{formatted} {unit}"
        amount /= 1024
    return f"{value} B"


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b)?\s*", value, re.I)
    if not match:
        raise ValueError("Use a size such as 750MB, 2GB, or 4GiB.")
    number = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    powers = {
        "B": 0,
        "KB": 1,
        "KIB": 1,
        "MB": 2,
        "MIB": 2,
        "GB": 3,
        "GIB": 3,
        "TB": 4,
        "TIB": 4,
    }
    result = int(number * (1024 ** powers[unit]))
    if result < 1 or result > 16 * 1024**4:
        raise ValueError("The limit must be between 1 byte and 16 TB.")
    return result


def validate_public_host(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("The public host name cannot be empty.")
    host = value.strip()
    if host.lower() == "auto":
        return "auto"
    if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("Use a host name such as OFFICE-PC or localbeam.local.")
    labels = host.rstrip(".").split(".")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
        raise ValueError("Use a valid host name such as OFFICE-PC or localbeam.local.")
    return host


def resolved_public_host(public_host: str) -> str:
    return socket.gethostname() if public_host == "auto" else public_host


def http_origin(public_host: str, port: int) -> str:
    return f"http://{resolved_public_host(public_host)}:{port}"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    defaults = {
        "host": "0.0.0.0",
        "port": 8765,
        "publicHost": "auto",
        "maxFileSizeBytes": 2 * 1024**3,
        "linkTtlSeconds": 600,
    }
    try:
        supplied = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        supplied = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    if not isinstance(supplied, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")
    config = {**defaults, **supplied}
    if not isinstance(config["port"], int) or not 1 <= config["port"] <= 65535:
        raise RuntimeError("config.json port must be between 1 and 65535.")
    if not isinstance(config["maxFileSizeBytes"], int) or config["maxFileSizeBytes"] < 1:
        raise RuntimeError("config.json maxFileSizeBytes must be a positive integer.")
    if not isinstance(config["linkTtlSeconds"], int) or config["linkTtlSeconds"] < 10:
        raise RuntimeError("config.json linkTtlSeconds must be at least 10.")
    try:
        config["publicHost"] = validate_public_host(config["publicHost"])
    except ValueError as exc:
        raise RuntimeError(f"config.json publicHost is invalid: {exc}") from exc
    return config


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class Transfer:
    transfer_id: str
    upload_key: str
    file_name: str
    content_type: str
    size: int
    created_at: float
    expires_at: float
    state: str = "waiting"
    bytes_received: int = 0
    bytes_sent: int = 0
    receiver_connected: bool = False
    upload_started: bool = False
    error: str | None = None
    finished_at: float | None = None
    chunks: queue.Queue[Any] = field(default_factory=lambda: queue.Queue(maxsize=QUEUE_DEPTH))
    stopped: threading.Event = field(default_factory=threading.Event)
    receiver_done: threading.Event = field(default_factory=threading.Event)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.transfer_id,
            "fileName": self.file_name,
            "contentType": self.content_type,
            "size": self.size,
            "createdAt": int(self.created_at * 1000),
            "expiresAt": int(self.expires_at * 1000),
            "state": self.state,
            "bytesReceived": self.bytes_received,
            "bytesSent": self.bytes_sent,
            "error": self.error,
        }


class TransferHub:
    def __init__(self, max_file_size: int, link_ttl_seconds: int, public_host: str = "auto") -> None:
        self.max_file_size = max_file_size
        self.link_ttl_seconds = link_ttl_seconds
        self.public_host = validate_public_host(public_host)
        self.transfers: dict[str, Transfer] = {}
        self.lock = threading.RLock()

    def create(self, file_name: str, size: int, content_type: str) -> Transfer:
        safe_name = Path(file_name.replace("\\", "/")).name.strip()
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("The file name is invalid.")
        if len(safe_name) > 255:
            raise ValueError("The file name is too long.")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("The file size is invalid.")
        safe_content_type = content_type.strip()[:120]
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", safe_content_type):
            safe_content_type = "application/octet-stream"
        with self.lock:
            if size > self.max_file_size:
                raise OverflowError(
                    f"This file is {format_bytes(size)}; the current limit is "
                    f"{format_bytes(self.max_file_size)}."
                )
            now = time.time()
            while True:
                transfer_id = "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(SHORT_CODE_LENGTH))
                if transfer_id not in self.transfers:
                    break
            transfer = Transfer(
                transfer_id=transfer_id,
                upload_key=secrets.token_urlsafe(24),
                file_name=safe_name,
                content_type=safe_content_type,
                size=size,
                created_at=now,
                expires_at=now + self.link_ttl_seconds,
            )
            self.transfers[transfer.transfer_id] = transfer
            return transfer

    def get(self, transfer_id: str) -> Transfer | None:
        self.expire_and_prune()
        with self.lock:
            return self.transfers.get(transfer_id)

    def authorize(self, transfer_id: str, upload_key: str) -> Transfer | None:
        transfer = self.get(transfer_id)
        if transfer is None or not secrets.compare_digest(transfer.upload_key, upload_key):
            return None
        return transfer

    def fail(self, transfer: Transfer, message: str, state: str = "failed") -> None:
        with self.lock:
            if transfer.state in {"completed", "failed", "cancelled", "expired"}:
                return
            transfer.state = state
            transfer.error = message
            transfer.finished_at = time.time()
            transfer.stopped.set()

    def expire_and_prune(self) -> None:
        now = time.time()
        with self.lock:
            for transfer in list(self.transfers.values()):
                if transfer.state == "waiting" and now >= transfer.expires_at:
                    transfer.state = "expired"
                    transfer.error = "This transfer link has expired."
                    transfer.finished_at = now
                    transfer.stopped.set()
                if transfer.finished_at and now - transfer.finished_at >= FINISHED_RETENTION_SECONDS:
                    self.transfers.pop(transfer.transfer_id, None)

    def snapshot(self) -> list[dict[str, Any]]:
        self.expire_and_prune()
        with self.lock:
            return [transfer.public_dict() for transfer in self.transfers.values()]


class LocalBeamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], hub: TransferHub) -> None:
        super().__init__(address, LocalBeamHandler)
        self.hub = hub


class LocalBeamHandler(BaseHTTPRequestHandler):
    server_version = "LocalBeam/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def hub(self) -> TransferHub:
        return self.server.hub  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required.")
        length = int(raw_length)
        if length < 0 or length > MAX_JSON_BYTES:
            raise ValueError("Request body is too large.")
        body = self.rfile.read(length)
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("A JSON object is required.")
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/config":
            with self.hub.lock:
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "maxFileSizeBytes": self.hub.max_file_size,
                        "linkTtlSeconds": self.hub.link_ttl_seconds,
                        "publicHost": resolved_public_host(self.hub.public_host),
                        "shareOrigin": http_origin(self.hub.public_host, self.server.server_address[1]),
                    },
                )
            return

        match = re.fullmatch(r"/api/transfers/([A-Za-z0-9_-]+)", path)
        if match:
            transfer = self.hub.get(match.group(1))
            if transfer is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Transfer not found."})
                return
            with self.hub.lock:
                payload = transfer.public_dict()
            self.send_json(HTTPStatus.OK, payload)
            return

        match = re.fullmatch(r"/api/transfers/([A-Za-z0-9_-]+)/download", path)
        if match:
            self.handle_download(match.group(1))
            return

        if path == "/" or path == "/send" or re.fullmatch(r"/(?:r|receive)/[A-Za-z0-9_-]+", path):
            self.serve_static("index.html")
            return
        if path in {"/app.js", "/styles.css"}:
            self.serve_static(path[1:])
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/transfers":
            try:
                payload = self.read_json()
                transfer = self.hub.create(
                    str(payload.get("fileName", "")),
                    payload.get("size"),
                    str(payload.get("contentType", "application/octet-stream")),
                )
            except OverflowError as exc:
                self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": str(exc)})
                return
            except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self.send_json(
                HTTPStatus.CREATED,
                {
                    **transfer.public_dict(),
                    "uploadKey": transfer.upload_key,
                    "receivePath": f"/r/{transfer.transfer_id}",
                },
            )
            return

        match = re.fullmatch(r"/api/transfers/([A-Za-z0-9_-]+)/cancel", parsed.path)
        if match:
            upload_key = parse_qs(parsed.query).get("key", [""])[0]
            transfer = self.hub.authorize(match.group(1), upload_key)
            if transfer is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Transfer not found."})
                return
            self.hub.fail(transfer, "The sender cancelled this transfer.", "cancelled")
            self._offer_stream_end(transfer)
            self.send_json(HTTPStatus.OK, {"state": "cancelled"})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/transfers/([A-Za-z0-9_-]+)/upload", parsed.path)
        if not match:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        upload_key = parse_qs(parsed.query).get("key", [""])[0]
        transfer = self.hub.authorize(match.group(1), upload_key)
        if transfer is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Transfer not found."})
            return
        self.handle_upload(transfer)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, PUT, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_static(self, file_name: str) -> None:
        path = PUBLIC_DIR / file_name
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Asset not found."})
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".html", ".js", ".css"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if path.suffix == ".html":
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'",
            )
        self.end_headers()
        self.wfile.write(body)

    def handle_download(self, transfer_id: str) -> None:
        transfer = self.hub.get(transfer_id)
        if transfer is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Transfer not found."})
            return
        with self.hub.lock:
            if transfer.state == "expired":
                self.send_json(HTTPStatus.GONE, {"error": transfer.error or "Link expired."})
                return
            if transfer.state != "waiting":
                self.send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "This single-use transfer has already been opened."},
                )
                return
            transfer.state = "receiver-ready"
            transfer.receiver_connected = True

        ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", transfer.file_name).strip() or "download"
        encoded_name = quote(transfer.file_name, safe="")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", transfer.content_type)
            self.send_header("Content-Length", str(transfer.size))
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.flush()

            while True:
                try:
                    chunk = transfer.chunks.get(timeout=1)
                except queue.Empty:
                    if transfer.stopped.is_set():
                        break
                    continue
                if chunk is STREAM_END:
                    break
                self.wfile.write(chunk)
                with self.hub.lock:
                    transfer.bytes_sent += len(chunk)
            self.wfile.flush()
            with self.hub.lock:
                if not transfer.stopped.is_set() and transfer.bytes_sent == transfer.size:
                    transfer.state = "completed"
                    transfer.finished_at = time.time()
                elif transfer.state not in {"failed", "cancelled", "expired"}:
                    self.hub.fail(transfer, "The transfer ended before the complete file arrived.")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            self.hub.fail(transfer, "The recipient disconnected before the transfer completed.")
        finally:
            with self.hub.lock:
                transfer.receiver_connected = False
            transfer.receiver_done.set()

    def handle_upload(self, transfer: Transfer) -> None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required."})
            return
        try:
            content_length = int(raw_length)
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Content-Length is invalid."})
            return
        if content_length != transfer.size:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "The uploaded data does not match the declared file size."},
            )
            return
        with self.hub.lock:
            if transfer.state != "receiver-ready" or not transfer.receiver_connected:
                self.send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "The recipient is not connected or the link was already used."},
                )
                return
            transfer.state = "transferring"
            transfer.upload_started = True

        remaining = content_length
        try:
            while remaining:
                if transfer.stopped.is_set():
                    raise ConnectionAbortedError(transfer.error or "Transfer stopped.")
                chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ConnectionAbortedError("The sender disconnected before upload completed.")
                remaining -= len(chunk)
                with self.hub.lock:
                    transfer.bytes_received += len(chunk)
                while True:
                    if transfer.stopped.is_set():
                        raise ConnectionAbortedError(transfer.error or "Transfer stopped.")
                    try:
                        transfer.chunks.put(chunk, timeout=1)
                        break
                    except queue.Full:
                        continue
            self._put_stream_end(transfer)
            transfer.receiver_done.wait(timeout=300)
            with self.hub.lock:
                completed = transfer.state == "completed"
                error = transfer.error
            if completed:
                self.send_json(HTTPStatus.OK, {"state": "completed"})
            else:
                if not transfer.stopped.is_set():
                    self.hub.fail(transfer, "The recipient did not finish saving the file.")
                self.send_json(HTTPStatus.CONFLICT, {"error": error or transfer.error or "Transfer failed."})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self.hub.fail(transfer, str(exc) or "The transfer was interrupted.")
            self._offer_stream_end(transfer)
            try:
                self.send_json(HTTPStatus.CONFLICT, {"error": transfer.error or "Transfer failed."})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    @staticmethod
    def _put_stream_end(transfer: Transfer) -> None:
        while not transfer.stopped.is_set():
            try:
                transfer.chunks.put(STREAM_END, timeout=1)
                return
            except queue.Full:
                continue

    @staticmethod
    def _offer_stream_end(transfer: Transfer) -> None:
        try:
            transfer.chunks.put_nowait(STREAM_END)
        except queue.Full:
            pass


def discover_lan_addresses(port: int) -> list[str]:
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 80))
        address = probe.getsockname()[0]
        probe.close()
        if not address.startswith("127."):
            addresses.add(address)
    except OSError:
        pass
    return [f"http://{address}:{port}" for address in sorted(addresses)]


def show_urls(port: int, public_host: str = "auto") -> None:
    print(f"  Named address: {http_origin(public_host, port)}")
    print(f"  This computer: http://localhost:{port}")
    lan_urls = discover_lan_addresses(port)
    if lan_urls:
        for url in lan_urls:
            print(f"  Other devices: {url}")
    else:
        print("  Other devices: use this computer's LAN IP address")


def admin_console(
    httpd: LocalBeamServer,
    hub: TransferHub,
    config: dict[str, Any],
    config_path: Path,
) -> None:
    print("\nAdmin terminal ready. Type 'help' for commands.")
    while True:
        try:
            line = input("localbeam-admin> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f"Error: {exc}")
            continue
        command = parts[0].lower()
        if command == "help":
            print("  limit              Show the current maximum file size")
            print("  limit 3GB          Change and save the maximum file size")
            print("  name               Show the host name used in shared links")
            print("  name OFFICE-PC     Change and save the shared-link host name")
            print("  name auto          Use this computer's network name")
            print("  transfers          List current and recent transfers")
            print("  cancel <id>        Cancel an active transfer")
            print("  urls               Show addresses for opening LocalBeam")
            print("  stop               Stop the server")
        elif command == "limit":
            if len(parts) == 1:
                print(f"Current limit: {format_bytes(hub.max_file_size)} ({hub.max_file_size} bytes)")
                continue
            try:
                new_limit = parse_size(parts[1])
                with hub.lock:
                    hub.max_file_size = new_limit
                config["maxFileSizeBytes"] = new_limit
                save_config(config, config_path)
                print(f"New transfers are now limited to {format_bytes(new_limit)}.")
            except (ValueError, OSError) as exc:
                print(f"Error: {exc}")
        elif command == "name":
            if len(parts) == 1:
                print(
                    f"Shared-link host: {resolved_public_host(hub.public_host)} "
                    f"({'automatic' if hub.public_host == 'auto' else 'configured'})"
                )
                continue
            try:
                new_host = validate_public_host(parts[1])
                with hub.lock:
                    hub.public_host = new_host
                config["publicHost"] = new_host
                save_config(config, config_path)
                print(f"New links will use {http_origin(new_host, httpd.server_address[1])}.")
                if new_host != "auto":
                    print("Make sure your router, DNS, or mDNS service resolves this name on recipient devices.")
            except (ValueError, OSError) as exc:
                print(f"Error: {exc}")
        elif command == "transfers":
            transfers = hub.snapshot()
            if not transfers:
                print("No transfers yet.")
                continue
            print(f"{'ID':<12} {'STATE':<16} {'PROGRESS':<14} FILE")
            for transfer in transfers:
                progress = f"{format_bytes(transfer['bytesSent'])}/{format_bytes(transfer['size'])}"
                print(
                    f"{transfer['id'][:10]:<12} {transfer['state']:<16} "
                    f"{progress:<14} {transfer['fileName']}"
                )
        elif command == "cancel" and len(parts) == 2:
            needle = parts[1]
            with hub.lock:
                matches = [item for item in hub.transfers.values() if item.transfer_id.startswith(needle)]
            if len(matches) != 1:
                print("Error: provide one unique transfer ID or prefix.")
                continue
            hub.fail(matches[0], "An administrator cancelled this transfer.", "cancelled")
            LocalBeamHandler._offer_stream_end(matches[0])
            print(f"Cancelled {matches[0].transfer_id}.")
        elif command == "urls":
            show_urls(httpd.server_address[1], hub.public_host)
        elif command in {"stop", "quit", "exit"}:
            print("Stopping LocalBeam...")
            httpd.shutdown()
            return
        else:
            print("Unknown command. Type 'help' to see the available commands.")


def run_server(config_path: Path = CONFIG_PATH, host: str | None = None, port: int | None = None) -> None:
    config = load_config(config_path)
    bind_host = host if host is not None else str(config["host"])
    bind_port = port if port is not None else int(config["port"])
    hub = TransferHub(
        int(config["maxFileSizeBytes"]),
        int(config["linkTtlSeconds"]),
        str(config["publicHost"]),
    )
    httpd = LocalBeamServer((bind_host, bind_port), hub)
    actual_port = httpd.server_address[1]
    print("\nLocalBeam is running")
    print(f"Maximum file size: {format_bytes(hub.max_file_size)}")
    show_urls(actual_port, hub.public_host)
    if bind_host in {"127.0.0.1", "localhost"}:
        print("LAN access is disabled because the server is bound to localhost only.")
    if sys.stdin.isatty():
        threading.Thread(
            target=admin_console,
            args=(httpd, hub, config, config_path),
            daemon=True,
            name="localbeam-admin",
        ).start()
    else:
        print("Admin prompt unavailable because this session is non-interactive.")
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping LocalBeam...")
    finally:
        httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the LocalBeam file-transfer server.")
    parser.add_argument("--host", help="Override the configured bind address.")
    parser.add_argument("--port", type=int, help="Override the configured port.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="Path to config.json.")
    args = parser.parse_args()
    run_server(args.config, args.host, args.port)


if __name__ == "__main__":
    main()
