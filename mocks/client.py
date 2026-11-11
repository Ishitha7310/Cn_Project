#!/usr/bin/env python3
"""MOCK of Member A's client (raw sockets). TESTING ONLY.
Downloads a file from the VIP; on any failure reconnects and RESUMES from the byte offset it already has.
Prints one JSON line at the end."""
import argparse
import json
import os
import socket
import sys
import time

VIP, PORT = "10.0.255.100", 8000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="medium")
    ap.add_argument("--vip", default=VIP)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--log", help="append 'served_by=<name>' lines here as soon as a server answers")
    ap.add_argument("--attempts", type=int, default=30)
    args = ap.parse_args()

    t0 = time.time()
    got, size, reconnects, served, last_err = 0, None, 0, [], ""
    for attempt in range(args.attempts):
        try:
            s = socket.create_connection((args.vip, args.port), timeout=3)
            s.settimeout(4)
            s.sendall(("GET %s %d\n" % (args.file, got)).encode())
            buf = b""
            while b"\n" not in buf:
                c = s.recv(1)
                if not c:
                    raise ConnectionError("closed before header")
                buf += c
            hdr = buf.decode().split()
            if hdr[0] != "OK":
                raise ConnectionError("server said: " + buf.decode().strip())
            size, srv = int(hdr[1]), hdr[3]
            if not served or served[-1] != srv:
                served.append(srv)
                if args.log:
                    with open(args.log, "a") as f:
                        f.write("served_by=%s\n" % srv)
            while got < size:
                d = s.recv(65536)
                if not d:
                    raise ConnectionError("closed mid-transfer")
                got += len(d)
            s.close()
            break
        except (OSError, ConnectionError, IndexError, ValueError) as e:
            last_err = repr(e)
            reconnects += 1
            time.sleep(0.5)
    dt = time.time() - t0
    ok = size is not None and got == size
    print(json.dumps({"ok": ok, "file": args.file, "bytes": got, "size": size, "time": round(dt, 3),
                      "throughput_mbps": round(got * 8 / dt / 1e6, 3) if dt else 0,
                      "served_by": served, "reconnects": reconnects, "err": "" if ok else last_err}), flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
