#!/usr/bin/env python3
"""
D2 experiment runner: compares server-selection policies under different conditions.

    ./run.sh experiments                 # full run (about 15-25 minutes)
    ./run.sh experiments --quick         # one repetition, small files (about 5 minutes) - use it to check your setup
    ./run.sh experiments --dry-run       # print the plan only

What it does
  1. builds a leaf-spine network: S server leaves (one server each), one CLIENT leaf (all clients, no server),
     and one extra leaf with a traffic sink. Because the clients sit on a leaf with no server, EVERY request
     crosses the spine, so link load and the choice of server both matter.
  2. starts the automatic link monitor (monitor_poller.py), which feeds link utilization to the controller
  3. for each repetition, condition and policy it switches the controller policy and runs the concurrent
     download workload (app/workload.py) from every client host, then records every request
  4. writes raw.csv / events.csv / utilization.csv and calls app/compare_results.py for tables and plots

Conditions
  normal      no extra load
  congestion  heavy UDP background traffic out of the server that the `static` baseline uses
  failure     that same server is killed a few seconds into the run (restarted afterwards)
The same target server is used for every policy, so the comparison is like for like. Note that this scenario is
deliberately the WORST case for the static baseline; adaptive policies are expected to route around it.

Needs the controller running (./run.sh experiments starts it) and must run as root (it drives Mininet).
"""
import argparse
import csv
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "app")]
import config as cfg  # noqa: E402

RAW_COLUMNS = ["condition", "policy", "rep", "host", "client", "request", "file", "served_by", "bytes",
               "response_time", "throughput_mbps", "retries", "status", "timestamp"]


# ----------------------------------------------------------------------------- controller helpers
def ctl(message, timeout=3.0):
    with socket.create_connection(("127.0.0.1", cfg.CTRL_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(message) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode()) if data else {}


def set_policy(name):
    r = ctl({"type": "cmd", "action": "set_policy", "policy": name})
    if not r.get("ok"):
        raise RuntimeError("controller refused policy %r: %s" % (name, r))


def active_servers():
    st = ctl({"type": "state"})
    return sorted(n for n, s in st.get("servers", {}).items() if s.get("state") == "ACTIVE")


def wait_active(names, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if set(names) <= set(active_servers()):
            return True
        time.sleep(1)
    return False


# ----------------------------------------------------------------------------- one run
def run_once(nm, cond, policy, rep, args, clients, target, sink, raw_writer, events_writer, log_dir):
    set_policy(policy)
    run_id = "%s_%s_r%d" % (cond, policy, rep)
    t_start = time.time()
    noise = []
    note = ""
    if cond == "congestion":
        before = len(nm.bg)
        nm.iperf_noise(target, sink, secs=int(args.run_timeout + 15), mbps=args.noise_mbps)
        noise = nm.bg[before:]
        time.sleep(args.settle)                      # let the monitor see the load and the controller react
        lm = nm.link_monitor
        peak = max(lm.latest.values()) if lm and lm.latest else 0.0
        note = " (peak link utilization %.0f%%)" % peak
        if peak < 30:
            print("   WARNING: background traffic is not showing up in the link monitor (peak %.0f%%) - "
                  "the congestion condition may not be effective" % peak)

    procs, csvs, dl_dirs = [], [], []
    files = " ".join(args.files)
    for c in clients:
        csv_path = os.path.join(log_dir, "%s_%s.csv" % (run_id, c))
        dl = "/tmp/d2dl/%s_%s" % (run_id, c)
        dl_dirs.append(dl)
        if os.path.exists(csv_path):
            os.remove(csv_path)
        cmd = ("cd %s && python3 app/workload.py --clients %d --requests %d --ramp 0.2 --files %s "
               "--output %s --csv %s" % (ROOT, args.workers, args.requests, files, dl, csv_path))
        nm.exec(c, cmd, bg=True, log=os.path.join(log_dir, "%s_%s.log" % (run_id, c)))
        procs.append(nm.bg[-1])
        csvs.append((c, csv_path))

    if cond == "failure":
        time.sleep(args.kill_after)
        nm.kill_server(target)
        note = " (killed %s after %.0f s)" % (target, args.kill_after)

    deadline = time.time() + args.run_timeout
    timed_out = 0
    for p in procs:
        try:
            p.wait(timeout=max(1.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.kill()
            timed_out += 1
    for p in noise:
        try:
            p.kill()
        except Exception:
            pass
    t_end = time.time()

    rows = []
    for c, path in csvs:
        if not os.path.exists(path):
            continue
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                rows.append({"condition": cond, "policy": policy, "rep": rep, "host": c, **{k: r[k] for k in r}})
    for r in rows:
        raw_writer.writerow({k: r.get(k, "") for k in RAW_COLUMNS})
    events_writer.writerow([t_start, t_end, cond, policy, rep])
    nm.bg[:] = [p for p in nm.bg if p.poll() is None]
    for d in dl_dirs:
        shutil.rmtree(d, ignore_errors=True)

    ok = [r for r in rows if r.get("status") == "OK"]
    mean_rt = sum(float(r["response_time"]) for r in ok) / len(ok) if ok else float("nan")
    print("   %d/%d requests ok, mean response time %.2f s, took %.0f s%s%s"
          % (len(ok), len(rows), mean_rt, t_end - t_start, note,
             ("  [%d workload(s) timed out]" % timed_out) if timed_out else ""))

    if cond == "failure":                            # bring the victim back for the next run
        nm.start_server(target)
        if not wait_active([target], 40):
            print("   WARNING: %s did not come back ACTIVE; skipping any further failure runs" % target)
            return False
    return True


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="D2 experiments: policy comparison under normal / congestion / failure")
    ap.add_argument("--policies", nargs="+", default=["static", "round_robin", "smart"],
                    help="policies to compare (static round_robin least_conn smart)")
    ap.add_argument("--conditions", nargs="+", default=["normal", "congestion", "failure"])
    ap.add_argument("--reps", type=int, default=3, help="repetitions of every (condition, policy)")
    ap.add_argument("--servers", type=int, default=3)
    ap.add_argument("--spines", type=int, default=2)
    ap.add_argument("--client-hosts", type=int, default=3)
    ap.add_argument("--workers", type=int, default=2, help="concurrent download workers per client host")
    ap.add_argument("--requests", type=int, default=2, help="downloads per worker")
    ap.add_argument("--files", nargs="+", default=["medium.bin", "large.bin"])
    ap.add_argument("--bw", type=float, default=20, help="switch-link bandwidth in Mbps")
    ap.add_argument("--noise-mbps", type=float, default=18)
    ap.add_argument("--kill-after", type=float, default=4, help="seconds into a failure run before the server is killed")
    ap.add_argument("--settle", type=float, default=5, help="seconds to let the monitor react after starting noise")
    ap.add_argument("--cooldown", type=float, default=6, help="seconds between runs")
    ap.add_argument("--run-timeout", type=float, default=120)
    ap.add_argument("--monitor-interval", type=float, default=1.0)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "results", "d2"))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--quick", action="store_true", help="1 repetition, small files: a fast check of the setup")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = ap.parse_args()
    if args.quick:
        args.reps, args.files, args.requests = 1, ["small.bin", "medium.bin"], 2

    plan = [(r, c, p) for r in range(1, args.reps + 1) for c in args.conditions for p in args.policies]
    print("plan: %d runs (%d repetitions x %d conditions x %d policies); roughly %d-%d minutes"
          % (len(plan), args.reps, len(args.conditions), len(args.policies),
             len(plan) * 0.4, len(plan) * 0.8))
    if args.dry_run:
        return 0

    from netmanager import NetManager
    os.makedirs(args.out, exist_ok=True)
    log_dir = os.path.join(ROOT, "logs", "d2")
    os.makedirs(log_dir, exist_ok=True)
    raw_path = os.path.join(args.out, "raw.csv")
    events_path = os.path.join(args.out, "events.csv")
    util_path = os.path.join(args.out, "utilization.csv")

    nm = NetManager().start()
    raw_f = open(raw_path, "w", newline="")
    ev_f = open(events_path, "w", newline="")
    raw_w = csv.DictWriter(raw_f, fieldnames=RAW_COLUMNS)
    raw_w.writeheader()
    ev_w = csv.writer(ev_f)
    ev_w.writerow(["start", "end", "condition", "policy", "rep"])
    rng = random.Random(args.seed)
    skip_failure = False
    try:
        print("== building leaf-spine: %d spines, %d server leaves + 1 client leaf + 1 sink leaf"
              % (args.spines, args.servers))
        topo = nm.gen_topo("leaf-spine", spines=args.spines, leaves=args.servers + 2, servers=args.servers,
                           clients=0, bw=args.bw)
        client_leaf, sink_leaf = nm.edge_sw[args.servers], nm.edge_sw[args.servers + 1]
        clients = [c["name"] for c in nm.add_clients(args.client_hosts, on=client_leaf)]
        nm.add_host("noisesink", on=sink_leaf)
        print("   ready:", nm.wait_ready(90))
        nm.announce_all()
        nm.monitor("on", interval=args.monitor_interval)
        servers = ["srv%d" % (i + 1) for i in range(args.servers)]
        if not wait_active(servers, 40):
            print("   WARNING: not every server registered ACTIVE: %s" % active_servers())
        time.sleep(3)

        set_policy("static")
        ip = nm.meta[clients[0]]["ip"]
        target = ctl({"type": "select_server", "client_ip": ip}).get("selected_server") or servers[0]
        print("   baseline (static) sends everything to %s; congestion and failure target that server" % target)

        # interleave: for each repetition, for each condition, policies in a random order (reduces drift bias)
        n = 0
        total = len(plan)
        for rep in range(1, args.reps + 1):
            for cond in args.conditions:
                order = list(args.policies)
                rng.shuffle(order)
                for policy in order:
                    n += 1
                    if cond == "failure" and skip_failure:
                        print("[%d/%d] %s / %s / rep %d  SKIPPED (server did not recover earlier)" % (n, total, cond, policy, rep))
                        continue
                    print("[%d/%d] %s / %s / rep %d" % (n, total, cond, policy, rep))
                    ok = run_once(nm, cond, policy, rep, args, clients, target, "noisesink", raw_w, ev_w, log_dir)
                    raw_f.flush()
                    ev_f.flush()
                    if cond == "failure" and not ok:
                        skip_failure = True
                    time.sleep(args.cooldown)
        meta = [
            "Topology: leaf-spine, %d spines, %d server leaves (1 server each), 1 client leaf (%d client hosts), "
            "1 sink leaf; every switch link %.0f Mbps" % (args.spines, args.servers, args.client_hosts, args.bw),
            "Workload per run: %d client hosts x %d concurrent workers x %d downloads of %s"
            % (args.client_hosts, args.workers, args.requests, ", ".join(args.files)),
            "Repetitions: %d (policy order shuffled within each condition)" % args.reps,
            "Congestion: %.0f Mbps UDP from %s to a sink host; Failure: %s killed %.0f s into the run (restarted afterwards)"
            % (args.noise_mbps, target, target, args.kill_after),
            "Link monitor: every %.1f s, utilization = busier direction of each switch link / link bandwidth"
            % args.monitor_interval,
            "Run date: %s" % time.strftime("%Y-%m-%d %H:%M"),
        ]
        if nm.link_monitor:
            nm.link_monitor.save_history(util_path)
    except KeyboardInterrupt:
        print("\ninterrupted - saving what was collected so far")
        meta = ["Interrupted run - partial results"]
        if nm.link_monitor:
            nm.link_monitor.save_history(util_path)
    finally:
        raw_f.close()
        ev_f.close()
        nm.stop()

    try:
        import compare_results
        compare_results.build_outputs(raw_path, args.out, meta, util_path, events_path)
        print("\nDone. Open %s" % os.path.join(args.out, "d2_report.md"))
    except Exception as e:
        print("\nresults saved in %s but the report could not be built (%s: %s)" % (args.out, type(e).__name__, e))
        print("fix the problem, then run:  python3 app/compare_results.py --raw %s --out %s --util %s --events %s"
              % (raw_path, args.out, util_path, events_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
