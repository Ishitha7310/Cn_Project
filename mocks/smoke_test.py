#!/usr/bin/env python3
"""End-to-end smoke test (TESTING ONLY).  Run through ./run.sh test  (needs the controller running).

  default : small leaf-spine, VIP rewrite, runtime add_server, link failure mid-download, server crash + resume
  --big   : 9 switches / 20 hosts, checks the design scales
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config as cfg  # noqa: E402
from netmanager import NetManager  # noqa: E402

CLIENT = "python3 %s/mocks/client.py" % ROOT
results = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   [" + str(detail) + "]") if detail else ""), flush=True)
    results.append(bool(ok))


def parse(out):
    lines = [l for l in out.splitlines() if l.startswith("{")]
    return json.loads(lines[-1]) if lines else {"ok": False, "raw": out[-300:]}


def fetch(nm, client, f="medium", timeout=90):
    return parse(nm.exec(client, "%s --file %s.bin" % (CLIENT, f), timeout=timeout)["out"])


def bg_fetch(nm, client, f, tag):
    log, out = os.path.join(cfg.LOG_DIR, "%s.cli" % tag), os.path.join(cfg.LOG_DIR, "%s.res" % tag)
    for p in (log, out):
        if os.path.exists(p):
            os.unlink(p)
    nm.exec(client, "%s --file %s.bin --log %s" % (CLIENT, f, log), bg=True, log=out)
    return log, out


def wait_server_name(log, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(log):
            for l in open(log):
                if l.startswith("served_by="):
                    return l.strip().split("=")[1]
        time.sleep(0.2)
    return None


def wait_result(out, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(out):
            r = parse(open(out).read())
            if "ok" in r and ("file" in r or "raw" in r and r["raw"]):
                if "file" in r:
                    return r
        time.sleep(0.5)
    return {"ok": False, "raw": "timeout waiting for client"}


def small(nm):
    print("== build network: leaf-spine, 2 spines, 3 leaves, 3 servers, 3 clients")
    t = nm.gen_topo("leaf-spine", spines=2, leaves=3, servers=3, clients=3)
    check("network built and every host reachable", t["ready"]["ok"], t["ready"])
    pa = nm.pingall()
    check("all-pairs ping, 0 lost", pa["lost"] == 0, pa)
    time.sleep(3)                                         # let servers register with the controller

    print("== content over the VIP")
    r = fetch(nm, "c1", "medium")
    check("client fetched 'medium' through the VIP", r["ok"], r)
    edge = nm.meta["c1"]["switch"]
    fl = nm.flows(edge, table=0)
    check("VIP rewrite flow present on client's edge switch", "ip_dst" in fl and "goto_table:1" in fl and "priority=100" in fl)
    used = set()
    for i in range(9):
        r = fetch(nm, ["c1", "c2", "c3"][i % 3], "small")
        if r["ok"]:
            used.add(r["served_by"][-1])
    check("requests spread over several servers (round-robin mock)", len(used) >= 2, sorted(used))

    print("== scale-out: add a server at runtime")
    nm.add_server("srvx", on=nm.edge_sw[-1])
    time.sleep(5)
    used = set()
    for i in range(12):
        r = fetch(nm, ["c1", "c2", "c3"][i % 3], "small")
        if r["ok"]:
            used.add(r["served_by"][-1])
    check("new server 'srvx' started receiving traffic", "srvx" in used, sorted(used))

    print("== link failure in the middle of a download")
    done = False
    for attempt in range(6):
        c = ["c1", "c2", "c3"][attempt % 3]
        log, out = bg_fetch(nm, c, "large", "linkfail")
        srv = wait_server_name(log)
        time.sleep(1.0)
        hop = nm.peer_via_output(nm.meta[c]["switch"], nm.meta[srv]["mac"]) if srv else None
        if hop and hop in nm.net and hop in [s.name for s in nm.net.switches]:
            sw = nm.meta[c]["switch"]
            print("   %s downloads from %s; taking down link %s <-> %s" % (c, srv, sw, hop))
            nm.link_down(sw, hop)
            r = wait_result(out)
            check("download survived link failure (rerouted, same TCP connection)",
                  r["ok"] and r["reconnects"] == 0, r)
            nm.link_up(sw, hop)
            done = True
            break
        wait_result(out)
    if not done:
        check("link failure test could run (needed a cross-leaf path)", False)
    time.sleep(4)

    print("== server crash in the middle of a download (resume on another server)")
    log, out = bg_fetch(nm, "c2", "large", "crash")
    srv = wait_server_name(log)
    time.sleep(1.0)
    print("   c2 downloads from %s; killing it" % srv)
    nm.kill_server(srv)
    r = wait_result(out)
    check("client reconnected and finished on a different server",
          r["ok"] and len(r["served_by"]) >= 2 and r["reconnects"] >= 1, r)
    time.sleep(1)
    r = fetch(nm, "c3", "small")
    check("new requests still work after the crash", r["ok"] and r["served_by"][-1] != srv, r)


def big(nm):
    print("== scale test: leaf-spine, 3 spines, 6 leaves, 6 servers, 14 clients (9 switches, 20 hosts)")
    t = nm.gen_topo("leaf-spine", spines=3, leaves=6, servers=6, clients=14)
    check("big network built and reachable", t["ready"]["ok"], "%s in %ss" % (len(t["hosts"]), t["build_secs"]))
    pa = nm.pingall()
    check("all-pairs ping on 20 hosts, 0 lost", pa["lost"] == 0, pa)
    time.sleep(3)
    ok = 0
    used = set()
    for i in range(8):
        r = fetch(nm, "c%d" % (i + 1), "small")
        ok += r["ok"]
        if r["ok"]:
            used.add(r["served_by"][-1])
    check("8 clients fetched content, spread over servers", ok == 8 and len(used) >= 3, sorted(used))


def main():
    nm = NetManager()
    nm.start(serve=False)
    try:
        (big if "--big" in sys.argv else small)(nm)
    finally:
        nm.stop()
    print("\n%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
