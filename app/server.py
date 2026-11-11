#!/usr/bin/env python3

"""
Smart Content Delivery System - Member A

TCP content server with:
- One thread per connection
- GET <file> <offset> protocol
- Byte-offset resume support
- Token-bucket egress rate limiting
- Optional newline-delimited JSON control messages to port 9000

NOTE: This file is reconstructed from the documented implementation/features in the
conversation. Compare it with your backup ZIP before treating it as byte-for-byte
identical to the original server.py you tested.
"""

import argparse
import json
import os
import socket
import threading
import time
from pathlib import Path


BUFFER_SIZE = 64 * 1024
HEARTBEAT_INTERVAL = 1.0   # controller (C) declares a server DOWN after 3 s without a heartbeat


class TokenBucket:
    """Simple token bucket limiting bytes per second."""

    def __init__(self, capacity_mbps: float):
        self.rate = max(0.0, capacity_mbps) * 1_000_000 / 8
        self.capacity = max(self.rate, BUFFER_SIZE)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def consume(self, amount: int) -> None:
        if self.rate <= 0:
            return
        remaining = amount
        while remaining > 0:
            with self.lock:
                now = time.monotonic()
                elapsed = now - self.updated
                self.updated = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                granted = min(remaining, int(self.tokens))
                self.tokens -= granted
            if granted:
                remaining -= granted
            else:
                time.sleep(min(remaining / self.rate, 0.02))


class ContentServer:
    def __init__(
        self,
        server_id: str,
        bind_host: str,
        port: int,
        content_dir: Path,
        capacity_mbps: float,
        max_conns: int,
        control_host: str,
        control_port: int,
        advertise_ip: str = "",
    ):
        self.server_id = server_id
        self.bind_host = bind_host
        self.port = port
        self.content_dir = content_dir.resolve()
        self.capacity_mbps = capacity_mbps
        self.max_conns = max_conns
        self.control_host = control_host
        self.control_port = control_port
        self.advertise_ip = advertise_ip or bind_host
        self.draining = threading.Event()
        self.stop_event = threading.Event()
        self.active_conns = 0
        self.total_bytes_sent = 0
        self.active_lock = threading.Lock()
        self.semaphore = threading.BoundedSemaphore(max_conns)
        self.bucket = TokenBucket(capacity_mbps)
        self.listener = None

    def send_control(self, message: dict) -> None:
        """Send one newline-delimited JSON control message."""
        try:
            with socket.create_connection(
                (self.control_host, self.control_port), timeout=2.0
            ) as sock:
                sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        except OSError as exc:
            print(f"[SERVER] control connection unavailable: {exc}")

    def register(self) -> None:
        self.send_control({
            "type": "register",
            "name": self.server_id,
            "ip": self.advertise_ip,
            "capacity_mbps": self.capacity_mbps,
            "max_conns": self.max_conns,
        })

    def heartbeat_loop(self) -> None:
        previous_bytes = self.total_bytes_sent
        previous_time = time.monotonic()
        while not self.stop_event.wait(HEARTBEAT_INTERVAL):
            now = time.monotonic()
            current_bytes = self.total_bytes_sent
            elapsed = max(now - previous_time, 0.001)
            tx_mbps = max(0, current_bytes - previous_bytes) * 8 / elapsed / 1_000_000
            with self.active_lock:
                active = self.active_conns
            self.send_control({
                "type": "heartbeat",
                "name": self.server_id,
                "ip": self.advertise_ip,
                "active_conns": active,
                "avg_service_ms": 0,
                "tx_mbps": round(tx_mbps, 3),
            })
            previous_bytes = current_bytes
            previous_time = now

    def safe_content_path(self, filename: str) -> Path:
        candidate = (self.content_dir / filename).resolve()
        if candidate != self.content_dir and self.content_dir not in candidate.parents:
            raise ValueError("Invalid file path")
        return candidate

    def send_error(self, conn: socket.socket, code: str, message: str) -> None:
        line = f"ERR {code} {message}\n"
        conn.sendall(line.encode("utf-8", errors="replace"))

    def handle_client(self, conn: socket.socket, address) -> None:
        with self.active_lock:
            self.active_conns += 1
        try:
            conn.settimeout(10)
            request = b""
            while b"\n" not in request and len(request) <= 4096:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                request += chunk
            if b"\n" not in request:
                self.send_error(conn, "BAD_REQUEST", "header too large")
                return

            line = request.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
            parts = line.split()
            if len(parts) != 3 or parts[0] != "GET":
                self.send_error(conn, "BAD_REQUEST", "expected GET file offset")
                return

            filename = parts[1]
            try:
                offset = int(parts[2])
                if offset < 0:
                    raise ValueError
            except ValueError:
                self.send_error(conn, "BAD_OFFSET", "offset must be nonnegative")
                return

            try:
                path = self.safe_content_path(filename)
            except ValueError:
                self.send_error(conn, "INVALID_PATH", "invalid file path")
                return

            if not path.is_file():
                self.send_error(conn, "NOT_FOUND", "file not found")
                return

            total_size = path.stat().st_size
            if offset > total_size:
                self.send_error(conn, "BAD_OFFSET", "offset exceeds file size")
                return

            header = f"OK {total_size} {offset} {self.server_id}\n".encode("utf-8")
            conn.sendall(header)

            with path.open("rb") as file:
                file.seek(offset)
                while True:
                    chunk = file.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    self.bucket.consume(len(chunk))
                    conn.sendall(chunk)
                    self.total_bytes_sent += len(chunk)

        except (OSError, ConnectionError, socket.timeout) as exc:
            print(f"[SERVER] client {address} transfer error: {exc}")
        finally:
            with self.active_lock:
                self.active_conns -= 1
            self.semaphore.release()
            try:
                conn.close()
            except OSError:
                pass

    def serve_forever(self) -> None:
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.bind_host, self.port))
        self.listener.listen()
        self.listener.settimeout(1.0)

        print(
            f"[SERVER] {self.server_id} listening on "
            f"{self.bind_host}:{self.port}"
        )
        self.register()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()

        try:
            while not self.stop_event.is_set():
                try:
                    conn, address = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                if self.draining.is_set():
                    try:
                        self.send_error(conn, "DRAINING", "server is draining")
                    finally:
                        conn.close()
                    continue

                if not self.semaphore.acquire(blocking=False):
                    try:
                        self.send_error(conn, "BUSY", "maximum connections reached")
                    finally:
                        conn.close()
                    continue

                threading.Thread(
                    target=self.handle_client,
                    args=(conn, address),
                    daemon=True,
                ).start()
        finally:
            self.stop_event.set()
            if self.listener:
                self.listener.close()
            self.send_control({
                "type": "deregister",
                "name": self.server_id,
                "graceful": True,
            })

    def stop(self) -> None:
        self.stop_event.set()
        if self.listener:
            try:
                self.listener.close()
            except OSError:
                pass


def parse_args():
    parser = argparse.ArgumentParser(description="Smart Content Delivery TCP server")
    parser.add_argument("--id", default="srv1", help="Server ID")
    parser.add_argument("--name", dest="id", help="Alias of --id (used by NetManager)")
    parser.add_argument("--ip", default="", help="Data-network IP to register with the controller")
    parser.add_argument("--ctrl", default="", help="Control server as host:port (overrides --control-host/--control-port)")
    parser.add_argument("--bind", default="0.0.0.0", help="Address to bind")
    parser.add_argument("--port", type=int, default=8000, help="TCP port")
    parser.add_argument("--content-dir", default=str(Path(__file__).resolve().parent.parent / "data" / "content"), help="Content directory")
    parser.add_argument("--capacity-mbps", type=float, default=20, help="Egress capacity in Mbps")
    parser.add_argument("--max-conns", type=int, default=50, help="Maximum simultaneous connections")
    parser.add_argument("--control-host", default="127.0.0.1", help="Control server host")
    parser.add_argument("--control-port", type=int, default=9000, help="Control server port")
    args = parser.parse_args()
    if args.ctrl:
        args.control_host, _, port = args.ctrl.rpartition(":")
        args.control_port = int(port)
    return args


def main():
    args = parse_args()
    server = ContentServer(
        server_id=args.id,
        bind_host=args.bind,
        port=args.port,
        content_dir=Path(args.content_dir),
        capacity_mbps=args.capacity_mbps,
        max_conns=args.max_conns,
        control_host=args.control_host,
        control_port=args.control_port,
        advertise_ip=args.ip,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SERVER] shutting down")
        server.stop()


if __name__ == "__main__":
    main()
