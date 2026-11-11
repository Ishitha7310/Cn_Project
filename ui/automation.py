#!/usr/bin/env python3
"""
Automation UI for the SDN content delivery system: one web page that replaces the 3-4 terminals.

  setup / content / unit tests -> controller start/stop -> network start + topology -> scenarios (D1 checks,
  link failure, server crash, D2 congestion demo) -> manual controls (policy, servers, links, noise, downloads,
  flows, any nm> command) -> run.sh test / experiments -> results (report + plots) -> live state and logs.

It drives the same things you type by hand: `run.sh controller`, `netmanager.py serve` (Unix socket API) and the
controller's JSON port 9000. Standard library only.

Run from the repo root as root (Mininet needs it):
    sudo python3 ui/automation.py            # then open http://localhost:8090
    sudo python3 ui/automation.py --port 9091 --host 0.0.0.0     (only on a trusted network: it runs root commands)
"""
import argparse
import collections
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config as cfg  # noqa: E402
from netmanager import COMMANDS, parse_tokens  # noqa: E402

IS_ROOT = os.geteuid() == 0
SUDO = [] if IS_ROOT else ["sudo", "-n"]
RESULT_DIRS = ("docs/results", "logs")


# ------------------------------------------------------------------ talking to the controller and NetManager
def ctl(msg, timeout=3):
    with socket.create_connection(("127.0.0.1", cfg.CTRL_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(msg) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            c = s.recv(65536)
            if not c:
                break
            data += c
    return json.loads(data.decode()) if data else {}


def nm(cmd, *args, _t=180, **opts):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(_t)
    s.connect(cfg.NM_SOCK)
    s.sendall(json.dumps({"cmd": cmd, "args": list(args), "opts": opts}).encode() + b"\n")
    data = b""
    while not data.endswith(b"\n"):
        c = s.recv(65536)
        if not c:
            break
        data += c
    s.close()
    r = json.loads(data.decode())
    if not r["ok"]:
        raise RuntimeError(r["error"])
    return r["result"]


def nm_up():
    return os.path.exists(cfg.NM_SOCK)


# ------------------------------------------------------------------ managed long-running processes
class Proc:
    def __init__(self, name):
        self.name, self.p, self.lines = name, None, collections.deque(maxlen=500)

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def start(self, cmd, env=None, stdin=False):
        if self.alive():
            return
        self.lines.clear()
        self.p = subprocess.Popen(cmd, cwd=ROOT, env=dict(os.environ, **(env or {})), text=True, bufsize=1,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=self._pump, args=(self.p,), daemon=True).start()

    def _pump(self, p):
        for line in p.stdout:
            self.lines.append(line.rstrip())
        self.lines.append("[process exited with code %s]" % p.wait())

    def stop(self, polite=None):
        if not self.alive():
            return
        try:
            if polite:
                self.p.stdin.write(polite + "\n")
                self.p.stdin.flush()
                self.p.wait(timeout=15)
        except Exception:
            pass
        for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, 3)):
            if not self.alive():
                break
            try:
                os.killpg(self.p.pid, sig)
                self.p.wait(timeout=wait)
            except Exception:
                pass


CONTROLLER, NETWORK, DASH = Proc("controller"), Proc("network"), Proc("dashboard")
PROCS = {"controller": CONTROLLER, "network": NETWORK, "dashboard": DASH}


# ------------------------------------------------------------------ jobs (every button press is a job with a log)
JOBS, JOB_LOCK, JOB_SEQ = [], threading.Lock(), [0]


class Job:
    def __init__(self, title):
        with JOB_LOCK:
            JOB_SEQ[0] += 1
            self.id = JOB_SEQ[0]
            JOBS.append(self)
            del JOBS[:-25]
        self.title, self.status, self.lines, self.proc = title, "running", [], None
        self.t0, self.t1, self.failed_checks = time.time(), None, 0

    def log(self, msg):
        for l in str(msg).splitlines() or [""]:
            self.lines.append(l)
        del self.lines[:-600]

    def check(self, ok, msg):
        self.log(("PASS  " if ok else "FAIL  ") + msg)
        if not ok:
            self.failed_checks += 1
        return ok

    def sh(self, cmd, timeout=3600):
        """Run a shell command, stream its output into the log; raises if it fails."""
        self.log("$ " + " ".join(cmd))
        self.proc = subprocess.Popen(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        t = threading.Timer(timeout, lambda: self.cancel())
        t.start()
        for line in self.proc.stdout:
            self.log(line.rstrip())
        rc = self.proc.wait()
        t.cancel()
        if self.status == "cancelled":
            raise RuntimeError("cancelled")
        if rc != 0:
            raise RuntimeError("command exited with code %s" % rc)
        return rc

    def cancel(self):
        self.status = "cancelled"
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except Exception:
                pass


def run_job(title, fn, params):
    job = Job(title)

    def work():
        try:
            fn(job, params)
            if job.status != "cancelled":
                job.status = "failed" if job.failed_checks else "done"
        except Exception as e:
            if job.status != "cancelled":
                job.status = "failed"
            job.log("ERROR: %s" % e)
        job.t1 = time.time()

    threading.Thread(target=work, daemon=True).start()
    return job.id


# ------------------------------------------------------------------ building blocks
def need_net():
    if not nm_up():
        raise RuntimeError("the network is not running - press 'Start network' first")


def start_controller(job, p):
    if CONTROLLER.alive():
        return job.log("controller already running")
    mgr = next((".venv/bin/" + m for m in ("ryu-manager", "osken-manager") if os.path.exists(os.path.join(ROOT, ".venv/bin/" + m))), None)
    if not mgr:
        raise RuntimeError("no .venv found - run 'Setup' first")
    env = {"BRAIN_POLICY": p["policy"]} if p.get("policy") else {}
    CONTROLLER.start(["bash", "run.sh", "controller"], env=env)
    for _ in range(40):
        try:
            socket.create_connection(("127.0.0.1", cfg.OF_PORT), 1).close()
            return job.log("controller is listening on OpenFlow port %d" % cfg.OF_PORT)
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("controller did not open port %d (see Logs > controller)" % cfg.OF_PORT)


def stop_network(job=None):
    NETWORK.stop(polite="exit")
    subprocess.run(SUDO + ["mn", "-c"], capture_output=True)


def start_network(job, p):
    if not CONTROLLER.alive():
        start_controller(job, {})
    if NETWORK.alive():
        return job.log("network already running")
    NETWORK.start(SUDO + ["env", "NM_DATAPATH=" + cfg.DATAPATH, "/usr/bin/python3", "netmanager.py", "serve"], stdin=True)
    for _ in range(120):
        if nm_up():
            return job.log("NetManager is up")
        if not NETWORK.alive():
            break
        time.sleep(0.5)
    raise RuntimeError("NetManager did not start (see Logs > network)")


def fresh_network(job, p=None):
    stop_network(job)
    start_network(job, {})


def build_topo(job, p):
    need_net()
    opts = {k: p[k] for k in ("spines", "leaves", "switches", "servers", "clients", "bw", "delay") if p.get(k) not in (None, "")}
    opts = {k: (float(v) if k in ("bw", "delay") else int(v)) for k, v in opts.items()}
    job.log("building %s topology %s (takes 30-60 s)..." % (p.get("kind", "leaf-spine"), opts))
    r = nm("gen_topo", p.get("kind", "leaf-spine"), _t=400, **opts)
    job.log(json.dumps(r.get("ready", r), default=str))
    if p.get("monitor"):
        nm("monitor", "on", interval=1)
        job.log("link monitor on")


def download(job, host, f="large.bin"):
    out = nm("exec", host, "python3 %s/app/client.py --file %s" % (ROOT, f), timeout=120)["out"]
    st = re.search(r"Status:\s*(\w+)", out)
    srv = re.search(r"\b(srv\d+)\b", out)
    mb = re.search(r"([\d.]+)\s*Mbps", out)
    return {"ok": bool(st and st.group(1).upper() == "OK"), "server": srv.group(1) if srv else None,
            "mbps": float(mb.group(1)) if mb else None, "out": out}


def bg_download(host, f, logf):
    try:
        os.unlink(logf)
    except OSError:
        pass
    nm("exec", host, "python3 %s/app/client.py --file %s > %s 2>&1" % (ROOT, f, logf), bg=True)


def wait_log(logf, secs=60):
    end = time.time() + secs
    while time.time() < end:
        try:
            t = open(logf).read()
            if re.search(r"Status:\s*\w+", t):
                return t
        except OSError:
            pass
        time.sleep(0.5)
    return ""


def ensure_topo(job):
    need_net()
    if not [h for h, m in nm("info")["hosts"].items() if m["kind"] == "client"]:
        build_topo(job, {"kind": "leaf-spine", "spines": 2, "leaves": 3, "servers": 3, "clients": 3, "monitor": True})
    info = nm("info")
    return ([h for h, m in info["hosts"].items() if m["kind"] == "client"],
            [h for h, m in info["hosts"].items() if m["kind"] == "server"], info)


# ------------------------------------------------------------------ scenarios (the automated demo / test scripts)
def sc_d1(job, p):
    clients, servers, info = ensure_topo(job)
    r = nm("pingall", _t=300)
    job.check(r["lost"] == 0, "T2 pingall: %s sent, %s lost" % (r["sent"], r["lost"]))
    st = ctl({"type": "state"})
    bad = [n for n, s in st["servers"].items() if s["state"] != "ACTIVE"]
    job.check(not bad and len(st["servers"]) >= 1, "T4 servers ACTIVE: %s" % ", ".join(sorted(st["servers"])))
    used = set()
    for c in clients[:3]:
        d = download(job, c)
        used.add(d["server"])
        job.check(d["ok"], "T3 %s -> large.bin served by %s (%s Mbps)" % (c, d["server"], d["mbps"]))
    job.check(len(used) > 1 or len(servers) == 1, "T5 load spread over servers: %s (policy %s)" % (sorted(map(str, used)), st.get("policy")))


def pick_core_link(info, client):
    leaf = info["hosts"][client]["switch"]
    sw = set(info["switches"])
    for l in info["links"]:
        if leaf in (l["a"], l["b"]) and l["a"] in sw and l["b"] in sw:
            return l["a"], l["b"]
    raise RuntimeError("no switch-switch link at %s" % leaf)


def sc_link(job, p):
    clients, servers, info = ensure_topo(job)
    c = clients[0]
    a, b = pick_core_link(info, c)
    bg_download(c, "large.bin", "/tmp/ui_t8.log")
    time.sleep(0.6)
    job.log("link_down %s %s during the download" % (a, b))
    nm("link_down", a, b)
    t = wait_log("/tmp/ui_t8.log", 90)
    job.check("Status: OK" in t or re.search(r"Status:\s*OK", t) is not None, "T8 download finished after link failure")
    job.log(t.strip()[-400:])
    nm("link_up", a, b)
    job.log("link_up %s %s (restored)" % (a, b))


def sc_crash(job, p):
    clients, servers, info = ensure_topo(job)
    c = clients[0]
    bg_download(c, "large.bin", "/tmp/ui_t9.log")
    victim = None
    for _ in range(20):
        time.sleep(0.3)
        sv = ctl({"type": "state"})["servers"]
        busy = [n for n, s in sv.items() if s.get("load", 0) > 0]
        if busy:
            victim = busy[0]
            break
    victim = victim or servers[0]
    job.log("kill_server %s (crash)" % victim)
    nm("kill_server", victim)
    t = wait_log("/tmp/ui_t9.log", 90)
    job.check(re.search(r"Status:\s*OK", t) is not None, "T9 download resumed on another server after crash")
    job.log(t.strip()[-400:])
    nm("start_server", victim)
    job.log("restarted %s" % victim)


def sc_d2(job, p):
    job.log("D2 demo needs a topology whose clients sit on a server-less leaf: rebuilding the network")
    fresh_network(job)
    for cmd in ("gen_topo leaf-spine --spines 2 --leaves 5 --servers 3 --clients 0", "add_clients 3 --on s6",
                "add_host noisesink --on s7", "wait_ready", "announce_all", "monitor on --interval 1"):
        c, a, o = parse_tokens(shlex.split(cmd))
        job.log("nm> " + cmd)
        nm(c, *a, _t=400, **o)
    res = {}
    base = download(job, "c1")
    job.log("baseline: %s Mbps via %s" % (base["mbps"], base["server"]))
    target = base["server"] or "srv1"
    ctl({"type": "cmd", "action": "set_policy", "policy": "static"})
    static_target = sorted(ctl({"type": "state"})["servers"])[0]
    nm("iperf_noise", static_target, "noisesink", secs=60, mbps=int(p.get("mbps") or 18))
    job.log("noise 18 Mbps from %s for 60 s; letting the monitor react" % static_target)
    time.sleep(8)
    for pol in ("static", "smart"):
        ctl({"type": "cmd", "action": "set_policy", "policy": pol})
        d = download(job, "c1")
        res[pol] = d
        job.log("%s: %s Mbps, served by %s, ok=%s" % (pol, d["mbps"], d["server"], d["ok"]))
        time.sleep(2)
    s, m = res["static"], res["smart"]
    job.check(s["ok"] and m["ok"], "both downloads completed")
    job.check(bool(m["server"]) and m["server"] != static_target, "smart avoided the congested server %s (used %s)" % (static_target, m["server"]))
    if s["mbps"] and m["mbps"]:
        job.check(m["mbps"] > s["mbps"], "smart faster than static: %.1f vs %.1f Mbps" % (m["mbps"], s["mbps"]))


SCENARIOS = {"d1": ("D1 checks T2-T5 (ping, downloads, health, load spread)", sc_d1),
             "link": ("T8 link failure during a download", sc_link),
             "crash": ("T9 server crash with resume", sc_crash),
             "d2": ("D2 congestion demo: static vs smart", sc_d2)}


# ------------------------------------------------------------------ actions (what the buttons call)
def a_setup(job, p):
    job.sh(["bash", "setup.sh"])
    job.sh(["python3", "app/make_content.py"])


def a_unit(job, p):
    job.sh(["python3", "tests/test_core.py"])
    job.sh(["python3", "tests/test_d2.py"])


def a_full_stop(job, p):
    stop_network(job)
    CONTROLLER.stop()
    subprocess.run(SUDO + ["bash", "run.sh", "clean"], cwd=ROOT, capture_output=True)
    job.log("everything stopped and cleaned")


def a_start_all(job, p):
    start_controller(job, p)
    start_network(job, p)
    build_topo(job, dict(p, monitor=True))


def a_dash(job, p):
    DASH.start([sys.executable, "ui/dashboard.py", "--port", "8080"])
    job.log("topology dashboard: http://localhost:8080")


def release_for_runsh(job):
    """run.sh test / experiments start their own controller and clean mininet: free ours first."""
    if CONTROLLER.alive() or NETWORK.alive():
        job.log("stopping the UI-managed controller/network (run.sh starts its own)")
        stop_network(job)
        CONTROLLER.stop()


def a_runtest(job, p):
    release_for_runsh(job)
    job.sh(["bash", "run.sh", "test"])


def a_experiments(job, p):
    release_for_runsh(job)
    cmd = ["bash", "run.sh", "experiments"]
    if p.get("quick"):
        cmd.append("--quick")
    if p.get("reps"):
        cmd += ["--reps", str(int(p["reps"]))]
    pol = [x for x in p.get("policies", []) if re.fullmatch(r"\w+", x)]
    if pol:
        cmd += ["--policies"] + pol
    job.sh(cmd)
    job.log("results are in docs/results/d2 (see the Results panel)")


def a_policy(job, p):
    r = ctl({"type": "cmd", "action": "set_policy", "policy": p["policy"]})
    job.log(json.dumps(r))


def a_server(job, p):
    act, s = p["action"], p["server"]
    if act in ("drain", "activate", "remove"):
        job.log(json.dumps(ctl({"type": "cmd", "action": act, "server_id": s})))
    else:
        need_net()
        job.log(json.dumps(nm(act, s)))


def a_addserver(job, p):
    need_net()
    job.log(json.dumps(nm("add_server", **({"on": p["on"]} if p.get("on") else {}))))


def a_link(job, p):
    need_net()
    act, a, b = p["action"], p["a"], p["b"]
    if act == "set_link":
        o = {k: float(p[k]) for k in ("bw", "delay", "loss") if p.get(k) not in (None, "")}
        job.log(json.dumps(nm("set_link", a, b, **o)))
    else:
        job.log(json.dumps(nm(act, a, b)))


def a_noise(job, p):
    need_net()
    job.log(json.dumps(nm("iperf_noise", p["a"], p["b"], secs=int(p.get("secs") or 20), mbps=float(p.get("mbps") or 8))))


def a_download(job, p):
    need_net()
    d = download(job, p["host"], p.get("file", "large.bin"))
    job.log(d["out"])
    job.check(d["ok"], "download from %s: server=%s, %s Mbps" % (p["host"], d["server"], d["mbps"]))


def a_workload(job, p):
    need_net()
    cmd = "python3 %s/app/workload.py --clients %d --requests %d --csv /tmp/ui_workload.csv --output /tmp/ui_dl" % (
        ROOT, int(p.get("clients") or 5), int(p.get("requests") or 1))
    job.log(nm("exec", p["host"], cmd, timeout=600, _t=620)["out"])


def a_flows(job, p):
    need_net()
    o = {"table": int(p["table"])} if p.get("table") not in (None, "") else {}
    job.log(nm("flows", p["switch"], **o))


def a_nm(job, p):
    need_net()
    line = p["line"].strip()
    toks = shlex.split(line)
    if toks[0] in COMMANDS:
        c, a, o = parse_tokens(toks)
        r = nm(c, *a, _t=400, **o)
    else:
        r = nm("exec", toks[0], line.split(None, 1)[1], timeout=120)["out"]
    job.log(r if isinstance(r, str) else json.dumps(r, indent=1, default=str))


def a_ctl(job, p):
    job.log(json.dumps(ctl(json.loads(p["json"])), indent=1))


def a_scenario(job, p):
    SCENARIOS[p["name"]][1](job, p)


ACTIONS = {
    "setup": ("Setup (apt + venv + content)", a_setup), "unit": ("Unit tests (no root)", a_unit),
    "start_all": ("Start controller + network + topology", a_start_all), "full_stop": ("Stop everything", a_full_stop),
    "controller_start": ("Start controller", lambda j, p: start_controller(j, p)),
    "controller_stop": ("Stop controller", lambda j, p: CONTROLLER.stop()),
    "net_start": ("Start network", start_network), "net_stop": ("Stop network", lambda j, p: stop_network(j)),
    "topo": ("Build topology", build_topo), "dashboard": ("Start topology dashboard", a_dash),
    "runtest": ("run.sh test (end-to-end)", a_runtest), "experiments": ("D2 experiments", a_experiments),
    "policy": ("Set policy", a_policy), "server": ("Server command", a_server), "addserver": ("Add server", a_addserver),
    "link": ("Link command", a_link), "noise": ("Background noise", a_noise), "download": ("Download", a_download),
    "workload": ("Workload", a_workload), "flows": ("Flow table", a_flows), "nm": ("nm> command", a_nm),
    "ctl": ("Controller command", a_ctl), "pingall": ("pingall", lambda j, p: j.check(
        (lambda r: r["lost"] == 0)(r := nm("pingall", _t=300)), "pingall %s" % r)),
    "scenario": ("Scenario", a_scenario),
}


def do_action(body):
    name, params = body.get("name"), body.get("params") or {}
    if name not in ACTIONS:
        return {"error": "unknown action"}
    title = ACTIONS[name][0]
    if name == "scenario":
        title = "Scenario: " + SCENARIOS[params["name"]][0]
    return {"job": run_job(title, ACTIONS[name][1], params)}


# ------------------------------------------------------------------ live state
LIVE = {"ctl": None, "ctl_err": "controller not running", "info": None}


def poller():
    n = 0
    while True:
        try:
            LIVE["ctl"], LIVE["ctl_err"] = ctl({"type": "state"}), ""
        except Exception as e:
            LIVE["ctl"], LIVE["ctl_err"] = None, str(e)
        if n % 3 == 0 and nm_up():
            threading.Thread(target=lambda: LIVE.update(info=_info()), daemon=True).start()
        elif not nm_up():
            LIVE["info"] = None
        n += 1
        time.sleep(1)


def _info():
    try:
        return nm("info", _t=10)
    except Exception:
        return LIVE["info"]


def list_results():
    out = []
    for d in RESULT_DIRS[:1]:
        base = os.path.join(ROOT, d)
        for dp, _, fs in os.walk(base):
            for f in sorted(fs):
                if f.lower().endswith((".md", ".csv", ".png")) and not f.startswith("Screenshot"):
                    out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return sorted(out)


def view():
    st = LIVE["ctl"]
    now = time.time()
    servers = {}
    for n, s in ((st or {}).get("servers") or {}).items():
        hb = s.get("last_heartbeat") or 0
        servers[n] = {"ip": s.get("ip"), "state": s.get("state"), "load": s.get("load", 0), "switch": s.get("switch"),
                      "mbps": s.get("tx_mbps"), "age": round(now - hb, 1) if hb else None}
    seen, links = set(), []
    for l in (st or {}).get("links", []):
        k = tuple(sorted((str(l["switch1"]), str(l["switch2"]))))
        if k not in seen:
            seen.add(k)
            links.append({"a": k[0], "b": k[1], "util": round(l.get("utilization", 0), 1)})
    info = LIVE["info"] or {}
    return {
        "root": IS_ROOT, "ctl_up": CONTROLLER.alive(), "net_up": nm_up(), "ctl_ok": st is not None, "ctl_err": LIVE["ctl_err"],
        "policy": (st or {}).get("policy"), "servers": servers, "links": links, "hosts": info.get("hosts", {}),
        "switches": info.get("switches", []), "nlinks": info.get("links", []),
        "scenarios": {k: v[0] for k, v in SCENARIOS.items()},
        "jobs": [{"id": j.id, "title": j.title, "status": j.status, "secs": round((j.t1 or now) - j.t0, 1),
                  "log": j.lines[-250:]} for j in reversed(JOBS)],
        "logs": {k: list(p.lines)[-120:] for k, p in PROCS.items()},
        "alive": {k: p.alive() for k, p in PROCS.items()}, "results": list_results(),
    }


# ------------------------------------------------------------------ page
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SDN-CDN Automation</title><style>
:root{--bg:#10141c;--panel:#1a2130;--line:#2c3650;--text:#e6ebf5;--mute:#8d99b3;--good:#34c27a;--bad:#ef5b5b;--warn:#e8b23a;--info:#5aa2ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
h1{font-size:18px;margin:0}h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);margin:0 0 9px}
.pill{padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:12.5px}.pill i{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--bad);margin-right:6px}.pill i.on{background:var(--good)}
main{display:grid;grid-template-columns:minmax(330px,420px) 1fr;gap:16px;padding:16px 20px;max-width:1500px;margin:auto}
@media(max-width:1000px){main{grid-template-columns:1fr}}
.col{display:grid;gap:14px;align-content:start;min-width:0}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px}
button{background:#243049;color:var(--text);border:1px solid #3a4768;border-radius:7px;padding:6px 11px;cursor:pointer;font:inherit;margin:2px 3px 2px 0}
button:hover{border-color:var(--info)}button.go{background:#1f5c3d;border-color:var(--good)}button.warn{background:#5c2a2a;border-color:var(--bad)}
input,select{background:#0f1522;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 7px;font:inherit;margin:2px 3px 2px 0}
input[type=number]{width:66px}input.wide{width:100%}label{color:var(--mute);font-size:12.5px;margin-right:6px}
.row{margin:5px 0}.hint{color:var(--mute);font-size:12.5px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:4px 6px;border-bottom:1px solid #222b3f;font-size:13px}th{color:var(--mute);font-weight:600}
.st{font-weight:650}.ACTIVE{color:var(--good)}.DOWN,.REMOVED{color:var(--bad)}.JOINING,.DRAINING{color:var(--warn)}
.bar{height:8px;background:#243049;border-radius:5px;overflow:hidden;min-width:80px}.bar b{display:block;height:100%;background:var(--info)}.bar b.m{background:var(--warn)}.bar b.h{background:var(--bad)}
pre{margin:0;background:#0b0f17;border:1px solid var(--line);border-radius:8px;padding:9px;max-height:320px;overflow:auto;font:12.5px/1.4 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-all}
.PASS{color:var(--good)}.FAIL,.ERROR{color:var(--bad)}.job{border:1px solid var(--line);border-radius:8px;margin-bottom:6px}.job summary{padding:6px 10px;cursor:pointer}
.tag{font-size:11.5px;padding:1px 8px;border-radius:99px;color:#0b0f17;background:var(--mute);margin-left:6px;font-weight:650}.tag.done{background:var(--good)}.tag.failed{background:var(--bad)}.tag.running{background:var(--info)}
.tabs button.on{border-color:var(--info);background:#2a3a5c}details.sec>summary{cursor:pointer;color:var(--mute);margin:6px 0}
img{max-width:100%;border:1px solid var(--line);border-radius:8px;margin:6px 0}
</style></head><body>
<header><h1>SDN-CDN Automation</h1>
<span class="pill"><i id="d_root"></i>root</span><span class="pill"><i id="d_ctl"></i>controller</span>
<span class="pill"><i id="d_net"></i>network</span><span class="pill">policy: <b id="pol">-</b></span>
<span class="pill">servers: <b id="nsrv">0</b></span><span class="pill">hosts: <b id="nhost">0</b></span><span id="rootwarn" class="hint"></span></header>
<main>
<div class="col">
 <section class="panel"><h2>1 · One-click</h2>
  <button class="go" onclick="act('start_all',{})">▶ Start controller + network + topology</button>
  <button class="warn" onclick="act('full_stop',{})">■ Stop everything</button>
  <div class="hint">Uses the topology form below. First time on a machine: Setup.</div>
  <div class="row"><button onclick="act('setup',{})">Setup (once)</button><button onclick="act('unit',{})">Unit tests</button>
  <button onclick="act('dashboard',{})">Start topology dashboard</button> <a style="color:var(--info)" href="#" onclick="window.open('http://'+location.hostname+':8080');return false">open :8080</a></div></section>
 <section class="panel"><h2>2 · Services &amp; topology</h2>
  <div class="row"><button onclick="act('controller_start',{policy:v('c_pol')})">Start controller</button><button onclick="act('controller_stop',{})">Stop</button>
  <label>initial policy</label><select id="c_pol"><option value="">default (round_robin)</option><option>static</option><option>round_robin</option><option>least_conn</option><option>smart</option></select></div>
  <div class="row"><button onclick="act('net_start',{})">Start network</button><button onclick="act('net_stop',{})">Stop network</button></div>
  <div class="row"><label>kind</label><select id="t_kind"><option>leaf-spine</option><option>ring</option><option>random</option></select>
  <label>spines</label><input type="number" id="t_spines" value="2"><label>leaves</label><input type="number" id="t_leaves" value="3">
  <label>switches</label><input type="number" id="t_switches" value="6"><br><label>servers</label><input type="number" id="t_servers" value="3">
  <label>clients</label><input type="number" id="t_clients" value="3"><label>bw</label><input type="number" id="t_bw" value="20"><label>delay</label><input type="number" id="t_delay" value="2">
  <br><label><input type="checkbox" id="t_mon" checked> link monitor on</label> <button class="go" onclick="topo()">Build topology</button></div></section>
 <section class="panel"><h2>3 · Automated scenarios</h2><div id="scen"></div>
  <div class="hint">Each runs the steps for you and prints PASS / FAIL. The D2 demo rebuilds the network.</div>
  <div class="row"><button onclick="act('runtest',{})">run.sh test (D1 end-to-end)</button></div>
  <details class="sec"><summary>D2 experiments (15-25 min; --quick about 5)</summary>
   <label><input type="checkbox" id="x_quick" checked> quick</label><label>reps</label><input type="number" id="x_reps" placeholder="3">
   <div class="row"><label><input type="checkbox" class="x_pol" value="static" checked> static</label><label><input type="checkbox" class="x_pol" value="round_robin" checked> round_robin</label>
   <label><input type="checkbox" class="x_pol" value="least_conn"> least_conn</label><label><input type="checkbox" class="x_pol" value="smart" checked> smart</label></div>
   <button class="go" onclick="experiments()">Run experiments</button></details></section>
 <section class="panel"><h2>4 · Manual controls</h2>
  <div class="tabs" id="tabs"></div><div id="tabbody"></div></section>
</div>
<div class="col">
 <section class="panel"><h2>Live: servers</h2><table id="srv"></table></section>
 <section class="panel"><h2>Live: link utilization</h2><div id="links"></div></section>
 <section class="panel"><h2>Activity (jobs)</h2><div id="jobs"></div><div class="row"><button onclick="cancelAll()">Cancel running</button></div></section>
 <section class="panel"><h2>Console</h2><div class="row"><input class="wide" id="cmd" placeholder='nm> command, e.g.  gen_topo ring --switches 5   |   c1 python3 app/client.py --file small.bin   |   flows s3 --table 0'></div>
  <div class="hint">Anything you would type at nm&gt;. <button onclick="runcmd()">Run</button></div></section>
 <section class="panel"><h2>Results</h2><div id="res" class="hint">no result files yet</div></section>
 <section class="panel"><h2>Logs</h2><div class="tabs" id="logtabs"></div><pre id="logbox"></pre></section>
</div></main>
<script>
let S={}, TAB='Policy', LOG='controller', OPEN=new Set();
const v=id=>document.getElementById(id).value, esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function act(name,params){await fetch('/api/action',{method:'POST',body:JSON.stringify({name,params})});tick()}
function topo(){act('topo',{kind:v('t_kind'),spines:v('t_spines'),leaves:v('t_leaves'),switches:v('t_switches'),servers:v('t_servers'),clients:v('t_clients'),bw:v('t_bw'),delay:v('t_delay'),monitor:document.getElementById('t_mon').checked})}
function experiments(){act('experiments',{quick:document.getElementById('x_quick').checked,reps:v('x_reps'),policies:[...document.querySelectorAll('.x_pol:checked')].map(x=>x.value)})}
function runcmd(){const l=v('cmd').trim();if(l)act('nm',{line:l})}
document.getElementById('cmd').addEventListener('keydown',e=>{if(e.key==='Enter')runcmd()});
async function cancelAll(){await fetch('/api/cancel',{method:'POST'});tick()}
const opt=(arr,sel)=>arr.map(x=>`<option${x===sel?' selected':''}>${esc(x)}</option>`).join('');
function tabBody(){
 const srv=Object.keys(S.servers||{}).sort(), hosts=Object.keys(S.hosts||{}).sort(), sw=(S.switches||[]).slice().sort(), cl=hosts.filter(h=>S.hosts[h].kind==='client');
 const sel=(id,a)=>`<select id="${id}">${opt(a)}</select>`;
 const linkSel=`<label>A</label>${sel('l_a',[...sw,...hosts])}<label>B</label>${sel('l_b',[...sw,...hosts])}`;
 const T={
 Policy:`<div class="row">${['static','round_robin','least_conn','smart'].map(p=>`<button ${S.policy===p?'class="go"':''} onclick="act('policy',{policy:'${p}'})">${p}</button>`).join('')}</div><div class="hint">static = baseline, smart = load + path delay + utilization.</div>`,
 Servers:`<div class="row">${sel('s_n',srv)}</div><div class="row">${['drain','activate','remove','kill_server','stop_server','cont_server','start_server'].map(a=>`<button onclick="act('server',{action:'${a}',server:v('s_n')})">${a.replace('_server','')}${a.endsWith('_server')?' ('+({kill_server:'crash',stop_server:'hang',cont_server:'resume',start_server:'start'})[a]+')':''}</button>`).join('')}</div>
  <div class="row"><label>add server on</label>${sel('s_on',['',...sw])}<button onclick="act('addserver',{on:v('s_on')})">Add</button></div>`,
 Links:`<div class="row">${linkSel}</div><div class="row"><button onclick="act('link',{action:'link_down',a:v('l_a'),b:v('l_b')})">link down</button><button onclick="act('link',{action:'link_up',a:v('l_a'),b:v('l_b')})">link up</button></div>
  <div class="row"><label>bw</label><input type="number" id="l_bw" placeholder="Mbps"><label>delay</label><input type="number" id="l_d" placeholder="ms"><label>loss</label><input type="number" id="l_l" placeholder="%">
  <button onclick="act('link',{action:'set_link',a:v('l_a'),b:v('l_b'),bw:v('l_bw'),delay:v('l_d'),loss:v('l_l')})">set</button></div>`,
 Traffic:`<div class="row"><label>noise from</label>${sel('n_a',hosts)}<label>to</label>${sel('n_b',hosts)}</div><div class="row"><label>Mbps</label><input type="number" id="n_m" value="18"><label>secs</label><input type="number" id="n_s" value="30"><button onclick="act('noise',{a:v('n_a'),b:v('n_b'),mbps:v('n_m'),secs:v('n_s')})">Start noise</button></div>
  <div class="row"><label>download on</label>${sel('d_h',cl)}<select id="d_f"><option>large.bin</option><option>medium.bin</option><option>small.bin</option></select><button class="go" onclick="act('download',{host:v('d_h'),file:v('d_f')})">Download</button></div>
  <div class="row"><label>workload on</label>${sel('w_h',cl)}<label>clients</label><input type="number" id="w_c" value="5"><label>req</label><input type="number" id="w_r" value="1"><button onclick="act('workload',{host:v('w_h'),clients:v('w_c'),requests:v('w_r')})">Run</button></div>`,
 Inspect:`<div class="row"><button onclick="act('pingall',{})">pingall</button><button onclick="act('ctl',{json:'{&quot;type&quot;:&quot;state&quot;}'})">controller state</button></div>
  <div class="row"><label>flows of</label>${sel('f_s',sw)}<select id="f_t"><option value="">all tables</option><option>0</option><option>1</option></select><button onclick="act('flows',{switch:v('f_s'),table:v('f_t')})">Show</button></div>
  <div class="row"><button onclick="act('nm',{line:'info'})">nm info</button><button onclick="act('nm',{line:'monitor on --interval 1'})">monitor on</button><button onclick="act('nm',{line:'monitor off'})">off</button><button onclick="act('nm',{line:'monitor status'})">status</button></div>`};
 return T[TAB]||'';}
function renderTabs(){const keep=document.activeElement&&document.activeElement.id;document.getElementById('tabs').innerHTML=['Policy','Servers','Links','Traffic','Inspect'].map(t=>`<button class="${t===TAB?'on':''}" onclick="TAB='${t}';renderTabs()">${t}</button>`).join('');
 const b=document.getElementById('tabbody'),vals={};b.querySelectorAll('input,select').forEach(e=>vals[e.id]=e.value);b.innerHTML=tabBody();b.querySelectorAll('input,select').forEach(e=>{if(vals[e.id]!==undefined&&[...(e.options||[])].length!==1)e.value=vals[e.id]});}
let lastKey='';
function render(){
 const on=(id,x)=>document.getElementById(id).className=x?'on':'';on('d_root',S.root);on('d_ctl',S.ctl_ok);on('d_net',S.net_up);
 document.getElementById('rootwarn').textContent=S.root?'':'not root: commands use sudo -n; start with  sudo python3 ui/automation.py';
 document.getElementById('pol').textContent=S.policy||'-';document.getElementById('nsrv').textContent=Object.keys(S.servers).length;document.getElementById('nhost').textContent=Object.keys(S.hosts).length;
 document.getElementById('scen').innerHTML=Object.entries(S.scenarios).map(([k,t])=>`<button onclick="act('scenario',{name:'${k}'})">▶ ${esc(t)}</button>`).join('<br>');
 const sv=Object.keys(S.servers).sort();document.getElementById('srv').innerHTML=sv.length?'<tr><th>server</th><th>state</th><th>ip</th><th>switch</th><th>conns</th><th>heartbeat</th></tr>'+sv.map(n=>{const s=S.servers[n];return `<tr><td><b>${n}</b></td><td class="st ${s.state}">${s.state}</td><td>${s.ip}</td><td>${s.switch==null?'-':'s'+s.switch}</td><td>${s.load}</td><td>${s.age==null?'-':s.age+' s'}</td></tr>`}).join(''):'<tr><td class="hint">no servers registered (start the controller and a topology)</td></tr>';
 const ls=S.links.slice().sort((a,b)=>b.util-a.util);document.getElementById('links').innerHTML=ls.length?ls.map(l=>`<div style="display:grid;grid-template-columns:80px 1fr 44px;gap:8px;align-items:center;margin:3px 0"><span>s${l.a} - s${l.b}</span><div class="bar"><b class="${l.util>=70?'h':l.util>=40?'m':''}" style="width:${Math.min(100,l.util)}%"></b></div><span class="hint">${Math.round(l.util)}%</span></div>`).join(''):'<span class="hint">no links known yet</span>';
 document.getElementById('jobs').innerHTML=S.jobs.length?S.jobs.slice(0,8).map((j,i)=>`<details class="job" data-id="${j.id}"${(OPEN.has(j.id)||(i===0&&!OPEN.size))?' open':''}><summary>#${j.id} ${esc(j.title)}<span class="tag ${j.status}">${j.status}</span> <span class="hint">${j.secs}s</span></summary><pre>${j.log.map(l=>{const c=/^(PASS|FAIL|ERROR)/.exec(l);return c?`<span class="${c[1]}">${esc(l)}</span>`:esc(l)}).join('\n')}</pre></details>`).join(''):'<span class="hint">nothing run yet</span>';
 document.querySelectorAll('#jobs details').forEach(d=>d.ontoggle=()=>{const id=+d.dataset.id;d.open?OPEN.add(id):OPEN.delete(id)});
 document.querySelectorAll('#jobs pre').forEach(p=>p.scrollTop=p.scrollHeight);
 const rs=S.results,key=rs.join('|');if(key!==lastKey){lastKey=key;document.getElementById('res').innerHTML=rs.length?rs.map(f=>/\.png$/.test(f)?`<img src="/files/${f}">`:`<div><a style="color:var(--info)" href="/files/${f}" target="_blank">${f}</a></div>`).join(''):'no result files yet (run the D2 experiments)'}
 document.getElementById('logtabs').innerHTML=Object.keys(S.logs).map(k=>`<button class="${k===LOG?'on':''}" onclick="LOG='${k}';render()">${k}${S.alive[k]?' ●':''}</button>`).join('');
 const lb=document.getElementById('logbox'),atEnd=lb.scrollTop+lb.clientHeight>=lb.scrollHeight-20;lb.textContent=(S.logs[LOG]||[]).join('\n')||'(empty)';if(atEnd)lb.scrollTop=lb.scrollHeight;
 const sig=JSON.stringify([Object.keys(S.hosts),S.switches,Object.keys(S.servers)]);if(sig!==render.sig){render.sig=sig;renderTabs()}}
async function tick(){try{S=await (await fetch('/api/state',{cache:'no-store'})).json();render()}catch(e){document.getElementById('rootwarn').textContent='UI server not reachable'}}
renderTabs();tick();setInterval(tick,1500);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(view(), default=str))
        if path.startswith("/files/"):
            rel = os.path.normpath(path[len("/files/"):])
            full = os.path.realpath(os.path.join(ROOT, rel))
            ok = any(full.startswith(os.path.realpath(os.path.join(ROOT, d)) + os.sep) for d in RESULT_DIRS)
            if ok and os.path.isfile(full):
                ctype = {".png": "image/png", ".csv": "text/plain", ".md": "text/plain"}.get(os.path.splitext(full)[1], "text/plain")
                return self._send(200, open(full, "rb").read(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if self.path == "/api/cancel":
            for j in JOBS:
                if j.status == "running":
                    j.cancel()
            return self._send(200, "{}")
        if self.path == "/api/action":
            try:
                return self._send(200, json.dumps(do_action(json.loads(self.rfile.read(n) or b"{}"))))
            except Exception as e:
                return self._send(400, json.dumps({"error": str(e)}))
        self._send(404, "{}")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="Automation UI for the SDN content delivery system")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default localhost; the UI runs root commands)")
    a = ap.parse_args()
    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("Automation UI: http://localhost:%d%s" % (a.port, "" if IS_ROOT else "   (not root: using sudo -n)"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nstopping managed processes...")
        stop_network()
        CONTROLLER.stop()
        DASH.stop()


if __name__ == "__main__":
    main()
