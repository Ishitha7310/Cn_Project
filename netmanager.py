#!/usr/bin/env python3
"""
netmanager.py  --  MEMBER B: the dynamic network.

Owns the live Mininet instance and lets you ADD / REMOVE switches, links, hosts, servers and clients
WHILE THE NETWORK IS RUNNING. Nothing is hard-coded: the controller discovers all of it (LLDP + ARP).

Three ways to drive it
  1) interactive prompt:      sudo python3 netmanager.py serve        ->   nm> add_server --on s3
  2) from another terminal:   python3 netmanager.py ctl add_server --on s3        (Unix socket API, for Member A)
  3) from Python:             from netmanager import NetManager; nm = NetManager(); nm.start(); nm.add_server(...)

Command syntax (prompt and ctl):   <command> [positional args] [--option value] [--flag]
Type  help  at the prompt for the list.

Needs root (Mininet).  Run with the SYSTEM python3 (the apt `mininet` package lives there).
"""
import argparse
import json
import os
import random
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from functools import partial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg  # noqa: E402

NOISE_SCRIPT = r'''
import socket, sys, time
ip, port, secs, mbps = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
pkt = b"x" * 1200
interval = 1200 * 8 / (mbps * 1e6)
end = time.time() + secs
nxt = time.time()
while time.time() < end:
    s.sendto(pkt, (ip, port))
    nxt += interval
    d = nxt - time.time()
    if d > 0:
        time.sleep(d)
'''

COMMANDS = [
    "gen_topo", "add_switch", "add_link", "del_link", "set_link", "link_down", "link_up",
    "add_host", "add_server", "add_clients", "remove_host", "start_server", "kill_server",
    "stop_server", "cont_server", "iperf_noise", "exec", "pingall", "wait_ready", "info",
    "flows", "announce_all", "monitor", "help",
]


class NetManager:
    def __init__(self, of_ip=cfg.OF_IP, of_port=cfg.OF_PORT):
        self.of_ip, self.of_port = of_ip, of_port
        self.lock = threading.RLock()
        self.net = None
        self.sm = None
        self.procs = {}          # server name -> Popen
        self.bg = []             # background Popens (noise, bg exec)
        self.meta = {}           # host name -> dict(kind, ip, mac, mgmt_ip, switch)
        self.edge_sw = []        # switches that hosts should attach to (set by gen_topo)
        self.link_params = {}    # frozenset({a,b}) -> dict(bw, delay, loss)
        self.sw_idx = self.host_idx = self.srv_idx = self.cl_idx = 0
        self.link_monitor = None  # D2: LinkMonitor, started with `monitor on`
        self._sock_server = None
        os.makedirs(cfg.LOG_DIR, exist_ok=True)

    # ------------------------------------------------------------------ lifecycle
    def start(self, serve=True):
        from mininet.net import Mininet
        from mininet.node import OVSSwitch, OVSBridge, RemoteController, Host
        from mininet.link import TCLink
        from mininet.log import setLogLevel
        from mininet.clean import cleanup
        setLogLevel("warning")
        if os.geteuid() != 0:
            raise SystemExit("netmanager must run as root (sudo)")
        self._ensure_ovs()
        self._safe_cleanup(cleanup)
        self._OVSSwitch = OVSSwitch
        switch = partial(OVSSwitch, protocols="OpenFlow13", datapath=cfg.DATAPATH)
        self.net = Mininet(controller=None, switch=switch, host=Host, link=TCLink, build=False)
        self.net.addController("c0", controller=RemoteController, ip=self.of_ip, port=self.of_port)
        # management network: a plain learning bridge, NO OpenFlow controller
        self.sm = self.net.addSwitch("sm", cls=partial(OVSBridge, datapath=cfg.DATAPATH), dpid="%016x" % 0xFFFE)
        self.net.start()
        subprocess.run(["ip", "addr", "flush", "dev", "sm"], check=False)
        subprocess.run(["ip", "addr", "add", "%s/%d" % (cfg.MGMT_CTRL_IP, cfg.MGMT_PREFIX), "dev", "sm"], check=False)
        subprocess.run(["ip", "link", "set", "sm", "up"], check=False)
        subprocess.run("ethtool -K sm tso off gso off gro off tx off rx off 2>/dev/null", shell=True, check=False)
        with open("/tmp/noise.py", "w") as f:
            f.write(NOISE_SCRIPT)
        if serve:
            self._start_socket_server()
        return self

    def stop(self):
        if self.link_monitor:
            self.link_monitor.stop()
            self.link_monitor = None
        with self.lock:
            for p in list(self.procs.values()) + self.bg:
                try:
                    p.kill()
                except Exception:
                    pass
            self.procs.clear()
            self.bg = []
            if self._sock_server:
                self._sock_server.shutdown()
                self._sock_server = None
            try:
                os.unlink(cfg.NM_SOCK)
            except OSError:
                pass
            if self.net:
                self.net.stop()
                self.net = None

    @staticmethod
    def _safe_cleanup(cleanup):
        """Mininet's cleanup() does `killall ... ryu-manager` and `rm -f /tmp/*.log`, which kills OUR controller
        (it is already running when the network starts) and deletes logs. Run it with those two steps removed."""
        import mininet.clean as mclean
        real_sh = mclean.sh

        def sh(cmd):
            if cmd.startswith("killall"):
                cmd = cmd.replace(" ryu-manager", "")
            if cmd.startswith("rm -f /tmp/vconn"):
                return ""
            return real_sh(cmd)

        mclean.sh = sh
        try:
            cleanup()
        finally:
            mclean.sh = real_sh

    @staticmethod
    def _ensure_ovs():
        r = subprocess.run(["ovs-vsctl", "--timeout=5", "show"], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("Open vSwitch is not running. Try: sudo systemctl start openvswitch-switch\n" + r.stderr)

    # ------------------------------------------------------------------ helpers
    def _node(self, name):
        try:
            return self.net.get(name)
        except KeyError:
            raise ValueError("no such node: %s" % name)

    def _is_switch(self, node):
        return isinstance(node, self._OVSSwitch)

    def _connect(self, a, b, **kw):
        """addLink on a RUNNING network: also plug the new port into any Open vSwitch involved."""
        link = self.net.addLink(a, b, **kw)
        for intf in (link.intf1, link.intf2):
            if self._is_switch(intf.node):
                intf.node.attach(intf)
        return link

    def _switch_names(self):
        return [s.name for s in self.net.switches if s is not self.sm]

    def _host_count(self, sw):
        return sum(1 for m in self.meta.values() if m["switch"] == sw)

    def _default_switch(self):
        cands = [s for s in (self.edge_sw or self._switch_names()) if s in self.net]
        if not cands:
            raise ValueError("no switch to attach to - run gen_topo or add_switch first")
        return min(cands, key=self._host_count)

    # ------------------------------------------------------------------ switches & links
    def add_switch(self, name=None):
        with self.lock:
            self.sw_idx += 1
            name = name or "s%d" % self.sw_idx
            if name in self.net:
                raise ValueError("name taken: %s" % name)
            sw = self.net.addSwitch(name, dpid="%016x" % self.sw_idx)
            sw.start(self.net.controllers)
            return {"switch": name, "dpid": self.sw_idx}

    def add_link(self, a, b, bw=20, delay=2, loss=0):
        with self.lock:
            na, nb = self._node(a), self._node(b)
            self._connect(na, nb, bw=bw, delay="%sms" % delay, loss=loss)
            self.link_params[frozenset((a, b))] = {"bw": bw, "delay": delay, "loss": loss}
            return {"link": [a, b], "bw": bw, "delay": delay, "loss": loss}

    def _links_between(self, a, b):
        na, nb = self._node(a), self._node(b)
        links = self.net.linksBetween(na, nb)
        if not links:
            raise ValueError("no link between %s and %s" % (a, b))
        return links

    def del_link(self, a, b):
        with self.lock:
            for l in self._links_between(a, b):
                for intf in (l.intf1, l.intf2):
                    if self._is_switch(intf.node):
                        intf.node.detach(intf)
                self.net.delLink(l)
            self.link_params.pop(frozenset((a, b)), None)
            return {"deleted": [a, b]}

    def _set_status(self, a, b, status):
        with self.lock:
            for l in self._links_between(a, b):
                for intf in (l.intf1, l.intf2):
                    intf.node.cmd("ip link set dev %s %s" % (intf.name, status))
            return {"link": [a, b], "status": status}

    def link_down(self, a, b):
        return self._set_status(a, b, "down")

    def link_up(self, a, b):
        return self._set_status(a, b, "up")

    def set_link(self, a, b, bw=None, delay=None, loss=None):
        """Change a live link's bandwidth (Mbps) / delay (ms) / loss (%) -- handy to create congestion."""
        with self.lock:
            kw = {}
            if bw is not None:
                kw["bw"] = bw
            if delay is not None:
                kw["delay"] = "%sms" % delay
            if loss is not None:
                kw["loss"] = loss
            for l in self._links_between(a, b):
                for intf in (l.intf1, l.intf2):
                    intf.config(**kw)
            self.link_params.setdefault(frozenset((a, b)), {}).update(
                {k: v for k, v in (("bw", bw), ("delay", delay), ("loss", loss)) if v is not None})
            return {"link": [a, b], "set": kw}

    # ------------------------------------------------------------------ hosts, servers, clients
    def _make_host(self, name, on, kind):
        if name in self.net:
            raise ValueError("name taken: %s" % name)
        if len(name) > 9:
            raise ValueError("host names must be <= 9 chars")
        on = on or self._default_switch()
        sw = self._node(on)
        self.host_idx += 1
        i = self.host_idx
        ip, mac = cfg.host_ip(i), cfg.host_mac(i)
        h = self.net.addHost(name, ip=ip)
        link = self._connect(h, sw, addr1=mac, bw=100, delay="1ms")
        link.intf1.setIP(ip, cfg.PREFIX)
        h.cmd("ip link set lo up")
        h.cmd("sysctl -qw net.ipv6.conf.all.disable_ipv6=1")
        h.cmd("ethtool -K %s tso off gso off gro off tx off rx off 2>/dev/null" % link.intf1.name)
        m = {"kind": kind, "ip": ip, "mac": mac, "switch": on, "mgmt_ip": None}
        if kind == "server":
            self.srv_idx += 1
            from mininet.link import Link
            m["mgmt_ip"] = cfg.mgmt_ip(self.srv_idx)
            ml = self._connect(h, self.sm, cls=Link, intfName1="%s-mgmt" % name)
            ml.intf1.setIP(m["mgmt_ip"], cfg.MGMT_PREFIX)
            h.cmd("ethtool -K %s tso off gso off gro off tx off rx off 2>/dev/null" % ml.intf1.name)
        self.meta[name] = m
        self._announce(h)
        return m

    def _announce(self, h):
        """One ARP for the VIP makes the controller learn this host (ARP is how hosts get discovered)."""
        try:
            self.bg.append(h.popen(["ping", "-c", "1", "-W", "1", cfg.VIP],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except Exception:
            pass

    def announce_all(self):
        with self.lock:
            for n in self.meta:
                self._announce(self._node(n))
            return {"announced": len(self.meta)}

    def add_host(self, name, on=None):
        with self.lock:
            return self._make_host(name, on, "host")

    def add_server(self, name=None, on=None, start=True):
        with self.lock:
            name = name or self._free_name("srv")
            m = self._make_host(name, on, "server")
            if start:
                self.start_server(name)
            return dict(m, name=name)

    def _free_name(self, prefix):
        k = 1
        while "%s%d" % (prefix, k) in self.net:
            k += 1
        return "%s%d" % (prefix, k)

    def add_clients(self, n=1, on=None):
        with self.lock:
            out = []
            for _ in range(int(n)):
                name = self._free_name("c")
                out.append(dict(self._make_host(name, on, "client"), name=name))
            return out

    def remove_host(self, name):
        with self.lock:
            h = self._node(name)
            self._kill_proc(name)
            for l in [l for l in list(self.net.links) if h in (l.intf1.node, l.intf2.node)]:
                for intf in (l.intf1, l.intf2):
                    if self._is_switch(intf.node):
                        intf.node.detach(intf)
                self.net.delLink(l)
            self.net.delHost(h)
            self.meta.pop(name, None)
            return {"removed": name}

    # ---- server processes
    def start_server(self, name):
        with self.lock:
            m = self.meta.get(name)
            if not m or m["kind"] != "server":
                raise ValueError("%s is not a server" % name)
            self._kill_proc(name)
            cmd = cfg.SERVER_CMD.format(repo=cfg.REPO, name=name, ip=m["ip"], ctrl_ip=cfg.MGMT_CTRL_IP,
                                        ctrl_port=cfg.CTRL_PORT, port=cfg.SERVICE_PORT, mgmt_ip=m["mgmt_ip"])
            logf = open(os.path.join(cfg.LOG_DIR, "%s.log" % name), "ab")
            self.procs[name] = self._node(name).popen(shlex.split(cmd), stdout=logf, stderr=subprocess.STDOUT)
            return {"server": name, "pid": self.procs[name].pid}

    def _kill_proc(self, name):
        p = self.procs.pop(name, None)
        if p is not None:
            try:
                p.send_signal(signal.SIGCONT)
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass

    def _signal(self, name, sig):
        p = self.procs.get(name)
        if p is None:
            raise ValueError("%s has no running server process" % name)
        p.send_signal(sig)
        return {"server": name, "signal": signal.Signals(sig).name}

    def kill_server(self, name):          # SIGKILL = crash
        with self.lock:
            r = self._signal(name, signal.SIGKILL)
            self.procs.pop(name, None)
            return r

    def stop_server(self, name):          # SIGSTOP = hang (socket stays open, never answers)
        return self._signal(name, signal.SIGSTOP)

    def cont_server(self, name):
        return self._signal(name, signal.SIGCONT)

    # ------------------------------------------------------------------ traffic & commands on hosts
    def iperf_noise(self, a, b, secs=20, mbps=8):
        """Background UDP load from host a to host b: saturates links on the path (congestion scenario)."""
        with self.lock:
            hb = self.meta[b]["ip"]
            self.bg.append(self._node(a).popen(["python3", "/tmp/noise.py", hb, "5999", str(secs), str(mbps)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            return {"noise": "%s -> %s" % (a, b), "mbps": mbps, "secs": secs}

    def exec(self, host, command, timeout=60, bg=False, log=None):
        """Run a shell command inside a host's network namespace."""
        h = self._node(host)
        if bg:
            out = open(log, "wb") if log else subprocess.DEVNULL
            p = h.popen(["bash", "-c", command], stdout=out, stderr=subprocess.STDOUT)
            self.bg.append(p)
            return {"pid": p.pid}
        p = h.popen(["bash", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True)
        try:
            out, _ = p.communicate(timeout=float(timeout))
            rc = p.returncode
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
            rc = "timeout"
        return {"rc": rc, "out": out}

    def pingall(self, timeout=1):
        """Every host pings every other host once (in parallel across hosts)."""
        names = list(self.meta)
        procs = {}
        for n in names:
            targets = " ".join(self.meta[o]["ip"] for o in names if o != n)
            script = ("for ip in %s; do ping -c1 -W%d $ip >/dev/null 2>&1 && echo ok || echo FAIL $ip; done"
                      % (targets, int(timeout)))
            procs[n] = self._node(n).popen(["bash", "-c", script], stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, universal_newlines=True)
        sent = lost = 0
        failed = []
        for n, p in procs.items():
            out, _ = p.communicate(timeout=300)
            for line in out.splitlines():
                sent += 1
                if line.startswith("FAIL"):
                    lost += 1
                    failed.append("%s->%s" % (n, line.split()[1]))
        return {"sent": sent, "lost": lost, "failed": failed[:20]}

    def wait_ready(self, timeout=60):
        """Block until every switch is connected to the controller and all hosts can reach the first host."""
        t0 = time.time()
        deadline = t0 + timeout
        while time.time() < deadline:
            if all(s.connected() for s in self.net.switches):
                break
            time.sleep(0.5)
        else:
            return {"ok": False, "why": "switches not connected to controller", "secs": round(time.time() - t0, 1)}
        time.sleep(1)
        self.announce_all()
        names = list(self.meta)
        if len(names) < 2:
            return {"ok": True, "secs": round(time.time() - t0, 1)}
        ref = names[0]
        ref_ip = self.meta[ref]["ip"]
        pending = [n for n in names if n != ref]
        while time.time() < deadline and pending:
            procs = {n: self._node(n).popen(["ping", "-c", "1", "-W", "1", ref_ip],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for n in pending}
            pending = [n for n, p in procs.items() if p.wait() != 0]
            if pending:
                time.sleep(0.5)
        return {"ok": not pending, "unreachable": pending, "secs": round(time.time() - t0, 1)}

    # ------------------------------------------------------------------ introspection
    def info(self):
        with self.lock:
            return {
                "switches": self._switch_names(),
                "hosts": self.meta,
                "links": [{"a": l.intf1.node.name, "b": l.intf2.node.name,
                           **self.link_params.get(frozenset((l.intf1.node.name, l.intf2.node.name)), {})}
                          for l in self.net.links],
                "servers_running": sorted(n for n, p in self.procs.items() if p.poll() is None),
            }

    def flows(self, switch, table=None):
        """The OpenFlow table of a switch (the same thing as `ovs-ofctl dump-flows`)."""
        cmd = ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", switch]
        if table is not None:
            cmd.append("table=%s" % table)
        return subprocess.run(cmd, capture_output=True, text=True).stdout

    def peer_via_output(self, switch, dst_mac):
        """Which neighbour does `switch` currently send traffic for dst_mac to? (reads table 1)"""
        import re
        out = self.flows(switch, table=cfg.TABLE_ROUTE)
        m = re.search(r"dl_dst=%s\S*\s+actions=output:(\d+)" % re.escape(dst_mac.lower()), out)
        if not m:
            return None
        port = m.group(1)
        sw = self._node(switch)
        for intf in sw.intfList():
            if intf.link and sw.cmd("ovs-vsctl get Interface %s ofport" % intf.name).strip() == port:
                other = intf.link.intf1 if intf.link.intf2 is intf else intf.link.intf2
                return other.node.name
        return None

    # ------------------------------------------------------------------ topology generators
    def gen_topo(self, kind="leaf-spine", spines=2, leaves=3, switches=6, servers=3, clients=3,
                 bw=20, delay=2, extra=3, seed=1, wait=True, start_servers=True):
        """kind = leaf-spine | ring | random.  Hosts are spread round-robin over the edge switches."""
        t0 = time.time()
        with self.lock:
            created = []
            if kind == "leaf-spine":
                sp = [self.add_switch()["switch"] for _ in range(int(spines))]
                lv = [self.add_switch()["switch"] for _ in range(int(leaves))]
                for l in lv:
                    for s in sp:
                        self.add_link(l, s, bw=bw, delay=delay)
                edges = lv
            elif kind in ("ring", "random"):
                n = int(switches)
                sw = [self.add_switch()["switch"] for _ in range(n)]
                if kind == "ring":
                    for i in range(n):
                        if n > 2 or i == 0:
                            self.add_link(sw[i], sw[(i + 1) % n], bw=bw, delay=delay)
                else:
                    rnd = random.Random(seed)
                    seen = set()
                    for i in range(1, n):                     # random spanning tree => connected
                        j = rnd.randrange(i)
                        self.add_link(sw[i], sw[j], bw=bw, delay=delay)
                        seen.add(frozenset((i, j)))
                    for _ in range(int(extra)):               # plus a few extra links => redundancy
                        i, j = rnd.sample(range(n), 2)
                        if frozenset((i, j)) not in seen:
                            self.add_link(sw[i], sw[j], bw=bw, delay=delay)
                            seen.add(frozenset((i, j)))
                edges = sw
            else:
                raise ValueError("unknown topology kind: %s" % kind)
            self.edge_sw = edges
            # switches must be connected to the controller before hosts announce themselves
            self._wait_switches(30)
            time.sleep(1)
            for k in range(int(servers)):
                created.append(self.add_server(on=edges[k % len(edges)], start=start_servers)["name"])
            for k in range(int(clients)):
                created.append(self.add_clients(1, on=edges[k % len(edges)])[0]["name"])
        res = {"kind": kind, "switches": self._switch_names(), "hosts": created, "build_secs": round(time.time() - t0, 1)}
        if wait:
            res["ready"] = self.wait_ready(90)
        return res

    def _wait_switches(self, timeout):
        end = time.time() + timeout
        while time.time() < end and not all(s.connected() for s in self.net.switches):
            time.sleep(0.3)

    # ------------------------------------------------------------------ D2: automatic link monitoring
    def monitor(self, action="status", interval=2.0):
        """monitor on [--interval 2] | off | status  - poll switch-link byte counters and feed utilization to the controller."""
        from monitor_poller import LinkMonitor
        if action == "on":
            if self.link_monitor:
                self.link_monitor.stop()
            self.link_monitor = LinkMonitor(self, interval=float(interval)).start()
            return {"monitor": "on", "interval": float(interval)}
        if action == "off":
            if self.link_monitor:
                self.link_monitor.stop()
                self.link_monitor = None
            return {"monitor": "off"}
        return self.link_monitor.status() if self.link_monitor else {"running": False}

    # ------------------------------------------------------------------ command dispatch
    def help(self):
        return HELP

    def run(self, cmd, args=(), opts=None):
        if cmd not in COMMANDS:
            raise ValueError("unknown command: %s (try: help)" % cmd)
        with self.lock:
            return getattr(self, cmd)(*args, **(opts or {}))

    def run_line(self, line):
        parts = line.strip().split(None, 1)
        if not parts:
            return None
        if self.net is not None and parts[0] in self.meta:            # "c1 python3 mocks/client.py --file large"
            if len(parts) < 2:
                raise ValueError("usage: <host> <shell command>")
            return self.exec(parts[0], parts[1])["out"]
        cmd, args, opts = parse_tokens(shlex.split(line))
        return self.run(cmd, args, opts)

    # ------------------------------------------------------------------ Unix socket API
    def _start_socket_server(self):
        nm = self
        try:
            os.unlink(cfg.NM_SOCK)
        except OSError:
            pass

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    try:
                        req = json.loads(line.decode())
                        res = nm.run(req["cmd"], req.get("args", []), req.get("opts", {}))
                        resp = {"ok": True, "result": res}
                    except Exception as e:
                        resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
                    self.wfile.write((json.dumps(resp, default=str) + "\n").encode())

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self._sock_server = Server(cfg.NM_SOCK, Handler)
        os.chmod(cfg.NM_SOCK, 0o666)
        threading.Thread(target=self._sock_server.serve_forever, daemon=True).start()


HELP = """commands (options are --name value):
  gen_topo [leaf-spine|ring|random] --spines 2 --leaves 3 --switches 6 --servers 3 --clients 3 --bw 20 --delay 2
  add_switch [name]                       add_link A B --bw 20 --delay 2 --loss 0      del_link A B
  link_down A B     link_up A B           set_link A B --bw 5 --delay 50 --loss 2   (live congestion/degrade)
  add_server [name] --on sX               add_clients N --on sX     add_host name --on sX     remove_host name
  kill_server N (crash)  stop_server N (hang)  cont_server N  start_server N
  iperf_noise A B --secs 20 --mbps 8      pingall     wait_ready     announce_all
  monitor on [--interval 2] | off | status     (automatic link-utilization monitoring, D2)
  exec HOST "shell command" [--bg]        or just:   c1 python3 mocks/client.py --file large
  flows sX [--table 0|1]                  info                       help      exit"""


def parse_tokens(toks):
    def conv(v):
        for t in (int, float):
            try:
                return t(v)
            except ValueError:
                pass
        return v
    cmd, args, opts = toks[0], [], {}
    i = 1
    while i < len(toks):
        t = toks[i]
        if t.startswith("--") and len(t) > 2:
            k = t[2:].replace("-", "_")
            if i + 1 < len(toks) and not toks[i + 1].startswith("--"):
                opts[k] = conv(toks[i + 1])
                i += 1
            else:
                opts[k] = True
        else:
            args.append(conv(t) if cmd in COMMANDS else t)
        i += 1
    return cmd, args, opts


def fmt(res):
    if res is None:
        return ""
    if isinstance(res, str):
        return res
    return json.dumps(res, indent=2, default=str)


def ctl(argv):
    """Client for the Unix-socket API:  python3 netmanager.py ctl add_server --on s2"""
    cmd, args, opts = parse_tokens(argv)
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(cfg.NM_SOCK)
    except OSError:
        sys.exit("cannot reach %s - is `netmanager.py serve` running?" % cfg.NM_SOCK)
    s.sendall((json.dumps({"cmd": cmd, "args": args, "opts": opts}) + "\n").encode())
    data = b""
    while not data.endswith(b"\n"):
        c = s.recv(65536)
        if not c:
            break
        data += c
    resp = json.loads(data.decode())
    if resp["ok"]:
        print(fmt(resp["result"]))
    else:
        sys.exit("error: " + resp["error"])


def serve(startup):
    nm = NetManager()
    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    try:
        nm.start()
        print("network up. controller expected at %s:%s. type `help`." % (cfg.OF_IP, cfg.OF_PORT))
        for line in startup:
            print("nm> " + line)
            print(fmt(nm.run_line(line)))
        while True:
            try:
                line = input("nm> ").strip()
            except EOFError:
                break
            if line in ("exit", "quit"):
                break
            try:
                out = nm.run_line(line)
                if out:
                    print(fmt(out))
            except Exception as e:
                print("error: %s: %s" % (type(e).__name__, e))
    except KeyboardInterrupt:
        pass
    finally:
        print("stopping network...")
        nm.stop()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "ctl":
        ctl(sys.argv[2:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "serve":
        ap = argparse.ArgumentParser(prog="netmanager.py serve")
        ap.add_argument("--run", action="append", default=[],
                        help='command to run right after start, e.g. --run "gen_topo leaf-spine --servers 4"')
        serve(ap.parse_args(sys.argv[2:]).run)
    else:
        print(__doc__)
        sys.exit(1)
