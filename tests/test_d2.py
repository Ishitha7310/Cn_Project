"""D2 tests that need no Mininet, Ryu or root.   Run:  python3 tests/test_d2.py

  1. LinkMonitor: byte counters -> utilization percent -> set_utilization message
  2. smart policy reacts to link utilization, server load and a DOWN server (static does not)
  3. experiment runner logic (run_experiments.py) against a fake network and a fake controller
  4. results pipeline (compare_results.py) on synthetic data
"""
import csv
import io
import contextlib
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "controller"), os.path.join(ROOT, "app"), os.path.join(ROOT, "experiments")]

results = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   [%s]" % (detail,)) if detail else ""))
    results.append(bool(cond))


# ============================================================================= 1. LinkMonitor
print("== 1. link monitor")
from monitor_poller import LinkMonitor  # noqa: E402


class Node:
    def __init__(self, name):
        self.name = name


class Intf:
    def __init__(self, node, name):
        self.node, self.name = node, name


class Link:
    def __init__(self, a, ia, b, ib):
        self.intf1, self.intf2 = Intf(Node(a), ia), Intf(Node(b), ib)


class FakeNet:
    links = []


nm = types.SimpleNamespace(net=FakeNet(), link_params={frozenset(("s3", "s1")): {"bw": 20}})
nm.net.links = [Link("s3", "s3-eth1", "s1", "s1-eth1"),
                Link("h1", "h1-eth0", "s3", "s3-eth9"),        # host link: ignored
                Link("srv1", "srv1-eth1", "sm", "sm-eth1")]    # management bridge: ignored
counters = {"s3-eth1": 0, "s1-eth1": 0}
clock = [100.0]
sent = []
mon = LinkMonitor(nm, interval=2, read_counter=lambda i: counters[i], send=lambda m: sent.append(m) or True,
                  clock=lambda: clock[0])
mon.sample()
check("first sample only records counters (no rate yet)", not sent)
clock[0] += 2
counters["s3-eth1"] += 2_500_000                     # 2.5 MB in 2 s = 10 Mbps on a 20 Mbps link
mon.sample()
check("10 Mbps on a 20 Mbps link -> 50% utilization", sent and sent[-1]["utilization"] == 50.0, sent[-1:] )
check("message format matches the controller's set_utilization",
      sent[-1] == {"type": "cmd", "action": "set_utilization", "switch1": 3, "switch2": 1, "utilization": 50.0})
clock[0] += 2
counters["s1-eth1"] += 20_000_000                    # busier direction is the OTHER end, and it exceeds the link rate
mon.sample()
check("busier direction counts and the value is capped at 100%", sent[-1]["utilization"] == 100.0, sent[-1]["utilization"])
clock[0] += 2
counters["s3-eth1"] = 0                              # counter reset must not produce a bogus value
n = len(sent)
mon.sample()
check("counter reset is ignored", len(sent) == n)
check("only switch-to-switch links are monitored", len(mon.discover()) == 1)
st = mon.status()
check("status reports the latest utilization", st["utilization"].get("s3-s1") == 100.0, st)
failing = LinkMonitor(nm, read_counter=lambda i: counters[i], send=lambda m: False, clock=lambda: clock[0])
failing.sample()
clock[0] += 2
counters["s3-eth1"] += 1000
failing.sample()
check("unreachable controller is counted, not fatal", failing.send_failures >= 1)

# ============================================================================= 2. smart policy
print("== 2. smart policy")
from net_state import NetState  # noqa: E402
from policy import Policy  # noqa: E402
from monitor import Monitor  # noqa: E402


def build():
    state = NetState()
    for leaf in (3, 4, 5, 6):                         # s3,s4,s5 server leaves; s6 client leaf (no server)
        for spine in (1, 2):
            state.add_link(leaf, spine, 1, 0)
            state.add_link(spine, leaf, 1, 0)
    for i, (name, sw) in enumerate((("srv1", 3), ("srv2", 4), ("srv3", 5)), 1):
        state.add_server(name, "10.0.0.%d" % i)
        state.activate_server(name)
        state.heartbeat(name)
        state.set_server_switch(name, sw)
    state.add_host("10.0.0.10", 6)
    pol = Policy(state)
    return state, pol, Monitor(state)


def pick(pol, ip="10.0.0.10"):
    with contextlib.redirect_stdout(io.StringIO()):
        return pol.select_server(ip)


state, pol, mon = build()
pol.set_policy("static")
check("static always picks the first server", pick(pol) == "srv1")
pol.set_policy("smart")
with contextlib.redirect_stdout(io.StringIO()):
    for _ in range(5):
        for spine in (1, 2):
            mon.update_link_utilization(3, spine, 90)      # srv1's leaf uplinks saturated
check("smart avoids the server behind congested links", pick(pol) != "srv1", pick(pol))
pol.set_policy("static")
check("static ignores the congestion (baseline stays on srv1)", pick(pol) == "srv1")

state, pol, mon = build()
pol.set_policy("smart")
state.servers["srv3"]["load"] = 6
check("smart avoids the heavily loaded server", pick(pol) != "srv3", pick(pol))
state.servers["srv1"]["state"] = "DOWN"
state.servers["srv2"]["state"] = "DOWN"
check("smart only returns ACTIVE servers", pick(pol) == "srv3", pick(pol))
state.servers["srv3"]["state"] = "DOWN"
check("no ACTIVE server -> None", pick(pol) is None)
with contextlib.redirect_stdout(io.StringIO()):
    mon2 = Monitor(state)
    for _ in range(4):
        mon2.update_link_utilization(3, 1, 90)
check("monitor smoothing converges towards a sustained value (EWMA)", state.links[(3, 1)]["utilization"] > 80,
      state.links[(3, 1)]["utilization"])

# ============================================================================= 3. experiment runner
print("== 3. experiment runner (fake network + fake controller)")
tmp = tempfile.mkdtemp(prefix="d2_runner_test_")
CTRL = {"policy": "round_robin", "servers": {"srv1": "ACTIVE", "srv2": "ACTIVE", "srv3": "ACTIVE"}, "log": []}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            m = json.loads(line.decode())
            if m.get("type") == "cmd" and m.get("action") == "set_policy":
                CTRL["policy"] = m["policy"]
                CTRL["log"].append(m["policy"])
                r = {"ok": True, "policy": m["policy"]}
            elif m.get("type") == "state":
                r = {"servers": {n: {"state": s} for n, s in CTRL["servers"].items()}, "policy": CTRL["policy"]}
            elif m.get("type") == "select_server":
                r = {"selected_server": "srv1"}
            else:
                r = {"ok": True}
            self.wfile.write((json.dumps(r) + "\n").encode())


class Srv(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


import config as cfg  # noqa: E402
try:
    server = Srv(("127.0.0.1", cfg.CTRL_PORT), Handler)
except OSError as e:
    server = None
    print("  SKIP  runner test: port %d is busy (%s). Stop the real controller and rerun." % (cfg.CTRL_PORT, e))

if server:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    WORKER = ("import sys,csv,time,random\n"
              "p=sys.argv[1]; random.seed(1)\n"
              "w=csv.writer(open(p,'w',newline=''))\n"
              "w.writerow(['timestamp','client','request','file','served_by','bytes','response_time','throughput_mbps','retries','status'])\n"
              "for i in range(4): w.writerow([time.time(),1+i%2,i,'small.bin','srv%d'%(1+i%3),100,1.0+random.random(),5.0,0,'OK'])\n")
    worker_path = os.path.join(tmp, "worker.py")
    open(worker_path, "w").write(WORKER)

    class FakeLM:
        latest = {("s3", "s1"): 80.0}

        def save_history(self, path):
            open(path, "w").write("time,link,utilization\n1.0,s3-s1,80.0\n2.0,s3-s1,10.0\n")

    class FakeNM:
        def __init__(self):
            self.bg, self.meta, self.edge_sw, self.link_monitor = [], {}, [], FakeLM()
            self.calls = []

        def start(self):
            return self

        def stop(self):
            self.calls.append("stop")

        def gen_topo(self, *a, **k):
            self.edge_sw = ["s%d" % i for i in range(3, 3 + k["leaves"])]
            return {}

        def add_clients(self, n, on=None):
            out = []
            for i in range(n):
                self.meta["c%d" % (i + 1)] = {"ip": "10.0.0.%d" % (10 + i)}
                out.append({"name": "c%d" % (i + 1)})
            return out

        def add_host(self, name, on=None):
            self.meta[name] = {"ip": "10.0.0.99"}

        def wait_ready(self, t):
            return {"ok": True}

        def announce_all(self):
            pass

        def monitor(self, *a, **k):
            return {}

        def exec(self, host, command, timeout=60, bg=False, log=None):
            csv_path = command.split("--csv ")[1].split()[0]
            p = subprocess.Popen([sys.executable, worker_path, csv_path])
            self.bg.append(p)
            return {"pid": p.pid}

        def iperf_noise(self, a, b, secs=20, mbps=8):
            self.bg.append(subprocess.Popen(["sleep", "30"]))
            self.calls.append(("noise", a, b))

        def kill_server(self, name):
            CTRL["servers"][name] = "DOWN"
            self.calls.append(("kill", name))

        def start_server(self, name):
            CTRL["servers"][name] = "ACTIVE"
            self.calls.append(("start", name))

    fake = FakeNM()
    stub = types.ModuleType("netmanager")
    stub.NetManager = lambda: fake
    sys.modules["netmanager"] = stub
    import run_experiments  # noqa: E402
    out = os.path.join(tmp, "out")
    argv = sys.argv
    sys.argv = ["run_experiments.py", "--quick", "--reps", "1", "--cooldown", "0", "--settle", "0",
                "--kill-after", "0", "--out", out]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = run_experiments.main()
    finally:
        sys.argv = argv
    with open(os.path.join(out, "raw.csv")) as f:
        rows = list(csv.DictReader(f))
    conds = sorted({r["condition"] for r in rows})
    pols = sorted({r["policy"] for r in rows})
    check("runner finished", rc == 0, buf.getvalue().strip().splitlines()[-1] if buf.getvalue() else "")
    check("every condition and policy was run", conds == ["congestion", "failure", "normal"]
          and pols == ["round_robin", "smart", "static"], (conds, pols))
    check("every run produced rows from every client host", len(rows) == 9 * 3 * 4, len(rows))
    check("policy was switched on the controller before each run", len(CTRL["log"]) >= 9, CTRL["log"][:4])
    check("congestion started noise from the baseline's server", ("noise", "srv1", "noisesink") in fake.calls)
    check("failure killed the baseline's server and restarted it", ("kill", "srv1") in fake.calls and ("start", "srv1") in fake.calls)
    check("report, summary and plots were written", all(os.path.exists(os.path.join(out, x))
          for x in ("d2_report.md", "summary.csv", "events.csv", "utilization.csv")))
    check("no leftover background processes after the runs", all(p.poll() is not None for p in fake.bg if "sleep" not in " ".join(getattr(p, "args", []))))
    server.shutdown()

# ============================================================================= 4. results pipeline
print("== 4. results pipeline")
r = subprocess.run([sys.executable, os.path.join(ROOT, "app", "compare_results.py"), "--selftest"],
                   capture_output=True, text=True)
check("compare_results.py selftest", r.returncode == 0 and "SELFTEST OK" in r.stdout, r.stdout.strip().splitlines()[-1:] or r.stderr[-200:])

print("\n%d/%d checks passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
