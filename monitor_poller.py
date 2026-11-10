#!/usr/bin/env python3
"""Automatic link monitoring (D2).

Every `interval` seconds this reads the transmit byte counters of both ends of every switch-to-switch link,
turns the busier direction into a utilization percentage of the link's configured bandwidth, and sends it to the
controller as a `set_utilization` message (TCP 127.0.0.1:9000, the same port ctl.py uses).

What the controller does with it (already in controller/):
  * monitor.py   smooths the value (EWMA, alpha 0.5)
  * path_engine  recomputes routes when a link's utilization changed by >= 20 points
  * policy.py    the `smart` policy adds the bottleneck utilization of the path to its server score

It runs inside NetManager (which already knows every link and runs as root next to Open vSwitch), so it needs no
extra processes:   nm> monitor on [--interval 2]     monitor off     monitor status
Standard library only.
"""
import json
import re
import socket
import threading
import time

import config as cfg

SWITCH_NAME = re.compile(r"^s(\d+)$")      # data switches only; skips the management bridge "sm" and hosts
HISTORY_MAX = 200000


def sysfs_tx_bytes(ifname):
    """Bytes transmitted on a Linux interface (Open vSwitch ports live in the root namespace)."""
    with open("/sys/class/net/%s/statistics/tx_bytes" % ifname) as f:
        return int(f.read())


def send_to_controller(host, port, message, timeout=1.0):
    """One JSON line to the controller; returns False if it could not be delivered."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(message) + "\n").encode())
            s.recv(4096)                    # the controller answers {"ok": true}
        return True
    except OSError:
        return False


class LinkMonitor:
    def __init__(self, nm, interval=2.0, ctrl_host="127.0.0.1", ctrl_port=cfg.CTRL_PORT,
                 read_counter=sysfs_tx_bytes, send=None, clock=time.time):
        self.nm = nm
        self.interval = float(interval)
        self.read = read_counter
        self.clock = clock
        self.send = send or (lambda msg: send_to_controller(ctrl_host, ctrl_port, msg))
        self.prev = {}               # (a, b) -> (time, tx_a, tx_b)
        self.latest = {}             # (a, b) -> utilization percent
        self.history = []            # (time, "s3", "s1", utilization)
        self.send_failures = 0
        self._stop = threading.Event()
        self._thread = None

    # ----------------------------------------------------------------- what to measure
    def discover(self):
        """Switch-to-switch links currently in the network, with both interface names and the configured bandwidth."""
        out = []
        for link in list(self.nm.net.links):
            n1, n2 = link.intf1.node, link.intf2.node
            m1, m2 = SWITCH_NAME.match(n1.name), SWITCH_NAME.match(n2.name)
            if not (m1 and m2):
                continue
            params = self.nm.link_params.get(frozenset((n1.name, n2.name)), {})
            out.append({"a": n1.name, "b": n2.name, "ia": link.intf1.name, "ib": link.intf2.name,
                        "ida": int(m1.group(1)), "idb": int(m2.group(1)), "bw": float(params.get("bw") or 20)})
        return out

    # ----------------------------------------------------------------- one measurement round
    def sample(self):
        now = self.clock()
        done = []
        for L in self.discover():
            key = (L["a"], L["b"])
            try:
                tx_a, tx_b = self.read(L["ia"]), self.read(L["ib"])
            except (OSError, ValueError):           # interface disappeared (link deleted)
                self.prev.pop(key, None)
                continue
            before = self.prev.get(key)
            self.prev[key] = (now, tx_a, tx_b)
            if before is None:
                continue                            # need two readings to get a rate
            dt = now - before[0]
            d_a, d_b = tx_a - before[1], tx_b - before[2]
            if dt <= 0 or d_a < 0 or d_b < 0:       # clock oddity or counter reset
                continue
            util = min(100.0, max(d_a, d_b) * 8 / (dt * L["bw"] * 1e6) * 100)
            util = round(util, 1)
            self.latest[key] = util
            self.history.append((now, L["a"], L["b"], util))
            del self.history[:-HISTORY_MAX]
            msg = {"type": "cmd", "action": "set_utilization",
                   "switch1": L["ida"], "switch2": L["idb"], "utilization": util}
            if not self.send(msg):
                self.send_failures += 1
            done.append((L["a"], L["b"], util))
        return done

    # ----------------------------------------------------------------- background thread
    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="link-monitor")
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception:                       # never let monitoring kill the network manager
                pass

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1)
        self._thread = None

    def status(self):
        return {"running": bool(self._thread and self._thread.is_alive()), "interval": self.interval,
                "links": len(self.latest), "send_failures": self.send_failures,
                "utilization": {"%s-%s" % k: v for k, v in sorted(self.latest.items())}}

    def save_history(self, path):
        """CSV of every measurement (time, link, utilization) for the report."""
        with open(path, "w") as f:
            f.write("time,link,utilization\n")
            for t, a, b, u in self.history:
                f.write("%.3f,%s-%s,%s\n" % (t, a, b, u))
