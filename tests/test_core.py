"""Integration test without Mininet/Ryu: A's real server + C's logic (BrainCore) + B's contract.
Run:  python3 tests/test_core.py
"""
import json, os, socket, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "controller")]
from core import BrainCore  # noqa: E402

ok_all = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   [%s]" % detail) if detail else ""))
    ok_all.append(bool(cond))

# fake B state
located, downs, route_calls = {}, [], []
core = BrainCore(located_hosts=lambda: located, notify_routes=lambda: route_calls.append(1),
                 notify_server_down=downs.append, log=lambda *a: None)

# ---- 1. leaf-spine: s1,s2 spines; s3,s4 leaves.  ports chosen like Ryu would report them
for a, b, pa, pb in [(3, 1, 1, 1), (3, 2, 2, 1), (4, 1, 1, 2), (4, 2, 2, 2)]:
    core.link_add(a, b, pa, pb); core.link_add(b, a, pb, pa)
core.on_host_learned("02:00:0a:00:00:01", "10.0.0.1", 3, 10)     # srv1 on s3
core.on_host_learned("02:00:0a:00:00:02", "10.0.0.2", 4, 10)     # client on s4
t = core.next_hop_table()
check("host's own switch -> host port", t[(3, "02:00:0a:00:00:01")] == 10)
check("every switch has a route to every host", all((s, m) in t for s in (1, 2, 3, 4) for m in ("02:00:0a:00:00:01", "02:00:0a:00:00:02")), len(t))
check("leaf s4 reaches srv1 via a spine port", t[(4, "02:00:0a:00:00:01")] in (1, 2))
before = dict(t)
core.link_delete(4, 1); core.link_delete(1, 4)
t2 = core.next_hop_table()
check("link s4-s1 down: s4 re-routes via s2 (port 2)", t2[(4, "02:00:0a:00:00:01")] == 2, t2[(4, "02:00:0a:00:00:01")])
check("on_routes_changed was triggered", len(route_calls) > 0)

# ---- 2. real server process from Member A talking to a listener that feeds BrainCore
srv_sock = socket.socket(); srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv_sock.bind(("127.0.0.1", 0)); srv_sock.listen(16); port = srv_sock.getsockname()[1]
seen = []
def accept_loop():
    while True:
        try: c, _ = srv_sock.accept()
        except OSError: return
        data = c.makefile().read()
        for line in data.splitlines():
            m = json.loads(line); seen.append(m); core.handle(m)
        c.close()
threading.Thread(target=accept_loop, daemon=True).start()
os.makedirs(os.path.join(ROOT, "data", "content"), exist_ok=True)
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "app", "server.py"), "--name", "srv1", "--ip", "10.0.0.1",
                         "--ctrl", "127.0.0.1:%d" % port, "--port", "18000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2.6)
check("A's server registered with name/ip", any(m["type"] == "register" and m["name"] == "srv1" and m["ip"] == "10.0.0.1" for m in seen))
hb = [m for m in seen if m["type"] == "heartbeat"]
check("A's server heartbeats at >=1 Hz", len(hb) >= 2, len(hb))
check("server visible to C as ACTIVE", core.state.servers["srv1"]["state"] == "ACTIVE")
check("not selectable until B has located its IP", core.select_server("10.0.0.2") is None)
located["10.0.0.1"] = object()
sel = core.select_server("10.0.0.2")
check("select_server returns {name, ip}", sel == {"name": "srv1", "ip": "10.0.0.1"}, sel)
check("server's switch learned from host location", core.state.servers["srv1"]["switch"] == 3)

# ---- 3. policies
for n, ip in (("srv2", "10.0.0.3"), ("srv3", "10.0.0.4")):
    core.handle({"type": "register", "name": n, "ip": ip, "capacity_mbps": 20, "max_conns": 50}); located[ip] = object()
core.policy.set_policy("round_robin")
picks = [core.select_server("10.0.0.2")["name"] for _ in range(6)]
check("round_robin cycles all servers", set(picks) == {"srv1", "srv2", "srv3"}, picks)
core.handle({"type": "cmd", "action": "set_policy", "policy": "least_conn"})
core.handle({"type": "heartbeat", "name": "srv1", "active_conns": 5}); core.handle({"type": "heartbeat", "name": "srv2", "active_conns": 0}); core.handle({"type": "heartbeat", "name": "srv3", "active_conns": 3})
check("least_conn picks the idle server", core.select_server("10.0.0.2")["name"] == "srv2")
core.handle({"type": "cmd", "action": "set_policy", "policy": "smart"})
check("smart policy returns a server (falls back safely)", core.select_server("10.0.0.2") is not None)

# ---- 4. failure handling
proc.kill(); proc.wait(); time.sleep(0.2)
core.state.servers["srv1"]["last_heartbeat"] = time.time() - 4      # simulate 4 s of silence
core.reap()
check("heartbeat timeout -> DOWN and Forwarding.on_server_down('srv1')", core.state.servers["srv1"]["state"] == "DOWN" and "srv1" in downs, downs)
check("DOWN server is never selected", all(core.select_server("10.0.0.2")["name"] != "srv1" for _ in range(6)))
core.handle({"type": "cmd", "action": "drain", "name": "srv3"}); core.handle({"type": "heartbeat", "name": "srv3", "active_conns": 0})
check("drained + idle server removed and flows cleaned", core.state.servers["srv3"]["state"] == "REMOVED" and "srv3" in downs)
core.handle({"type": "cmd", "action": "set_utilization", "switch1": 3, "switch2": 2, "utilization": 90})
check("big utilization change triggers route recompute", core.state.links[(3, 2)]["utilization"] == 90)
srv_sock.close()
print("\nRESULT:", "ALL PASS" if all(ok_all) else "FAILURES")
sys.exit(0 if all(ok_all) else 1)
