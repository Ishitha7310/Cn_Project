#!/usr/bin/env python3
"""MOCK of Member A's content server (raw sockets). TESTING ONLY.
Protocol:  client sends  "GET <file> <offset>\n"
           server sends  "OK <size> <offset> <server_name>\n" + raw bytes   (or "ERR <code> <msg>\n")
Also registers with the controller and sends a JSON heartbeat every second (control channel)."""
import argparse
import json
import socket
import threading
import time

FILES = {"small": 100 * 1024, "medium": 1024 * 1024, "large": 5 * 1024 * 1024}
CHUNK = 16384
PATTERN = bytes(range(251)) * 80


def chunk(offset, n):
    s = offset % 251
    return PATTERN[s:s + n]


class State:
    active = 0
    lock = threading.Lock()


def recv_line(conn, limit=256):
    buf = b""
    while not buf.endswith(b"\n"):
        c = conn.recv(1)
        if not c or len(buf) > limit:
            raise ConnectionError("bad request")
        buf += c
    return buf.decode().strip()


def handle(conn, args):
    conn.settimeout(15)
    with State.lock:
        State.active += 1
    try:
        parts = recv_line(conn).split()
        if len(parts) != 3 or parts[0] != "GET":
            conn.sendall(b"ERR 400 bad request\n")
            return
        name, off = parts[1], int(parts[2])
        size = FILES.get(name)
        if size is None:
            conn.sendall(b"ERR 404 no such file\n")
            return
        if off < 0 or off > size:
            conn.sendall(b"ERR 416 bad offset\n")
            return
        conn.sendall(("OK %d %d %s\n" % (size, off, args.name)).encode())
        sent = off
        while sent < size:
            n = min(CHUNK, size - sent)
            conn.sendall(chunk(sent, n))
            sent += n
            time.sleep(n * 8 / (args.rate * 1e6))      # crude egress limit
    except (OSError, ValueError):
        pass
    finally:
        with State.lock:
            State.active -= 1
        conn.close()


def control_loop(args):
    host, port = args.ctrl.split(":")
    while True:
        try:
            s = socket.create_connection((host, int(port)), timeout=3)
            s.sendall((json.dumps({"type": "register", "name": args.name, "ip": args.ip,
                                   "capacity_mbps": args.rate, "max_conns": 50}) + "\n").encode())
            while True:
                s.sendall((json.dumps({"type": "heartbeat", "name": args.name,
                                       "active_conns": State.active}) + "\n").encode())
                time.sleep(1)
        except OSError:
            time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--ip", required=True)
    ap.add_argument("--ctrl", default="192.168.100.254:9000")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--rate", type=float, default=10.0, help="per-connection egress limit, Mbps")
    args = ap.parse_args()
    threading.Thread(target=control_loop, args=(args,), daemon=True).start()
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("0.0.0.0", args.port))
    ls.listen(128)
    print("server %s listening on %d" % (args.name, args.port), flush=True)
    while True:
        conn, _ = ls.accept()
        threading.Thread(target=handle, args=(conn, args), daemon=True).start()


if __name__ == "__main__":
    main()
