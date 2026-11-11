#!/usr/bin/env python3
"""
Smart Content Delivery System - Member A (D2)

Turns the raw experiment results (experiments/run_experiments.py -> raw.csv) into
  * summary.csv      one row per (condition, policy)
  * d2_report.md     tables + automatically computed findings (numbers only, no hand-written claims)
  * response_time.png, throughput.png, server_share.png   (+ utilization.png if the link-utilization log exists)

    python3 app/compare_results.py --raw results/d2/raw.csv --out docs/results/d2
    python3 app/compare_results.py --selftest            # synthetic data in /tmp, checks the pipeline only

Needs only the standard library; matplotlib is used for the PNGs (sudo apt install python3-matplotlib).
If matplotlib is missing, the CSV and the report are still written.
"""
import argparse
import csv
import os
import random
import statistics
import sys
from collections import defaultdict

CONDITION_ORDER = ["normal", "congestion", "failure"]
POLICY_ORDER = ["static", "round_robin", "least_conn", "smart"]
POLICY_LABEL = {"static": "static (baseline)", "round_robin": "round_robin", "least_conn": "least_conn", "smart": "smart"}
CONDITION_LABEL = {"normal": "Normal (no extra load)",
                   "congestion": "Congestion (background traffic on a server's links)",
                   "failure": "Server failure during the run"}
COLORS = {"static": "#8d99b3", "round_robin": "#5aa2ff", "least_conn": "#e8b23a", "smart": "#34c27a"}


# ----------------------------------------------------------------------------- loading
def load_raw(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for k in ("response_time", "throughput_mbps", "bytes", "retries"):
                try:
                    r[k] = float(r[k])
                except (KeyError, ValueError):
                    r[k] = 0.0
            r["rep"] = int(float(r.get("rep") or 0))
            r["ok"] = (r.get("status") == "OK")
            rows.append(r)
    if not rows:
        raise ValueError("no rows in %s" % path)
    return rows


def ordered(values, preferred):
    seen = sorted(set(values), key=lambda v: (preferred.index(v) if v in preferred else len(preferred), v))
    return seen


# ----------------------------------------------------------------------------- statistics
def mean(xs):
    return statistics.mean(xs) if xs else 0.0


def std(xs):
    return statistics.stdev(xs) if len(xs) >= 2 else 0.0


def p95(xs):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def summarize(rows):
    """{(condition, policy): metrics}. Mean and std are taken over repetitions (each repetition = one mean)."""
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[(r["condition"], r["policy"])][r["rep"]].append(r)
    out = {}
    for key, by_rep in groups.items():
        rep_rt, rep_tp, all_rt, retries, total, ok = [], [], [], [], 0, 0
        for rep, rs in by_rep.items():
            good = [x for x in rs if x["ok"]]
            total += len(rs)
            ok += len(good)
            retries += [x["retries"] for x in rs]
            all_rt += [x["response_time"] for x in good]
            if good:
                rep_rt.append(mean([x["response_time"] for x in good]))
                rep_tp.append(mean([x["throughput_mbps"] for x in good]))
        out[key] = {"reps": len(by_rep), "requests": total, "ok_rate": 100.0 * ok / total if total else 0.0,
                    "rt_mean": mean(rep_rt), "rt_std": std(rep_rt), "rt_p95": p95(all_rt),
                    "tp_mean": mean(rep_tp), "tp_std": std(rep_tp), "retries": mean(retries)}
    return out


def server_share(rows):
    """{(condition, policy): {server: percent of successful requests}}"""
    counts = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["ok"]:
            counts[(r["condition"], r["policy"])][r.get("served_by") or "unknown"] += 1
    out = {}
    for key, c in counts.items():
        tot = sum(c.values())
        out[key] = {s: 100.0 * n / tot for s, n in sorted(c.items())}
    return out


# ----------------------------------------------------------------------------- outputs
def write_summary_csv(summary, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["condition", "policy", "repetitions", "requests", "success_percent", "response_time_mean_s",
                    "response_time_std_s", "response_time_p95_s", "throughput_mean_mbps", "throughput_std_mbps",
                    "retries_mean"])
        for (c, p), m in sorted(summary.items(), key=lambda kv: (CONDITION_ORDER.index(kv[0][0]) if kv[0][0] in CONDITION_ORDER else 9,
                                                                 POLICY_ORDER.index(kv[0][1]) if kv[0][1] in POLICY_ORDER else 9)):
            w.writerow([c, p, m["reps"], m["requests"], round(m["ok_rate"], 1), round(m["rt_mean"], 3),
                        round(m["rt_std"], 3), round(m["rt_p95"], 3), round(m["tp_mean"], 3), round(m["tp_std"], 3),
                        round(m["retries"], 2)])


def findings(summary, conditions, policies):
    """Plain statements computed from the numbers (no opinions)."""
    lines = []
    for c in conditions:
        have = {p: summary[(c, p)] for p in policies if (c, p) in summary and summary[(c, p)]["rt_mean"] > 0}
        if len(have) < 2:
            continue
        best = min(have, key=lambda p: have[p]["rt_mean"])
        lines.append("**%s:** lowest mean response time was `%s` (%.2f s)." % (CONDITION_LABEL.get(c, c), best, have[best]["rt_mean"]))
        if "static" in have and "smart" in have:
            s, m = have["static"], have["smart"]
            change = 100.0 * (m["rt_mean"] - s["rt_mean"]) / s["rt_mean"] if s["rt_mean"] else 0.0
            tp = 100.0 * (m["tp_mean"] - s["tp_mean"]) / s["tp_mean"] if s["tp_mean"] else 0.0
            lines.append("  `smart` vs `static`: response time %+.1f%% (%.2f s vs %.2f s), throughput %+.1f%% (%.2f vs %.2f Mbps)."
                         % (change, m["rt_mean"], s["rt_mean"], tp, m["tp_mean"], s["tp_mean"]))
        for p, m in have.items():
            if m["ok_rate"] < 100.0:
                lines.append("  `%s` completed only %.0f%% of its requests." % (p, m["ok_rate"]))
    return lines


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def write_report(rows, summary, share, out_dir, meta, synthetic=False, plots=()):
    conditions = ordered([c for c, _ in summary], CONDITION_ORDER)
    policies = ordered([p for _, p in summary], POLICY_ORDER)
    lines = ["# D2 experiment results", ""]
    if synthetic:
        lines += ["> **SYNTHETIC TEST DATA - generated by `--selftest` to check the pipeline. Not real results.**", ""]
    if meta:
        lines += ["## Setup", ""] + ["- %s" % m for m in meta] + [""]
    lines += ["## Summary", "",
              "Mean and standard deviation are taken over repetitions (one mean per repetition). "
              "Response time and throughput use successful requests only.", ""]
    table = []
    for c in conditions:
        for p in policies:
            m = summary.get((c, p))
            if not m:
                continue
            table.append([c, POLICY_LABEL.get(p, p), m["reps"], m["requests"], "%.0f%%" % m["ok_rate"],
                          "%.2f +/- %.2f" % (m["rt_mean"], m["rt_std"]), "%.2f" % m["rt_p95"],
                          "%.2f +/- %.2f" % (m["tp_mean"], m["tp_std"]), "%.2f" % m["retries"]])
    lines += [md_table(["Condition", "Policy", "Reps", "Requests", "Success", "Response time (s)", "p95 (s)",
                        "Throughput (Mbps)", "Retries/request"], table), ""]
    lines += ["## What the numbers show", ""] + findings(summary, conditions, policies) + [""]
    if any(m["reps"] < 3 for m in summary.values()):
        lines += ["_Some groups have fewer than 3 repetitions, so the standard deviations are not reliable. "
                  "Run with `--reps 3` or more for the final report._", ""]
    servers = sorted({s for d in share.values() for s in d})
    if servers:
        lines += ["## Which server answered (share of successful requests)", ""]
        srows = []
        for c in conditions:
            for p in policies:
                if (c, p) in share:
                    srows.append([c, POLICY_LABEL.get(p, p)] + ["%.0f%%" % share[(c, p)].get(s, 0.0) for s in servers])
        lines += [md_table(["Condition", "Policy"] + servers, srows), ""]
    if plots:
        lines += ["## Plots", ""] + ["![%s](%s)" % (os.path.splitext(p)[0], p) for p in plots] + [""]
    lines += ["## Policies compared", "",
              "- `static`: always the first ACTIVE server (the no-intelligence baseline).",
              "- `round_robin`: rotates over ACTIVE servers.",
              "- `least_conn`: ACTIVE server with the fewest active connections.",
              "- `smart`: lowest score = normalised server load + bottleneck link utilization on the path + path delay.", ""]
    with open(os.path.join(out_dir, "d2_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ----------------------------------------------------------------------------- plots
def get_plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except ImportError:
        print("matplotlib not installed - skipping PNG plots (sudo apt install python3-matplotlib)")
        return None


def watermark(plt, fig, synthetic):
    if synthetic:
        fig.text(0.5, 0.5, "SYNTHETIC TEST DATA", fontsize=34, color="red", alpha=0.18, ha="center", va="center", rotation=25)


def plot_metric(plt, summary, key, std_key, ylabel, title, path, synthetic):
    conditions = ordered([c for c, _ in summary], CONDITION_ORDER)
    policies = ordered([p for _, p in summary], POLICY_ORDER)
    fig, ax = plt.subplots(figsize=(9, 5))
    width = 0.8 / max(1, len(policies))
    for i, p in enumerate(policies):
        xs = [j + (i - (len(policies) - 1) / 2) * width for j in range(len(conditions))]
        vals = [summary.get((c, p), {}).get(key, 0) for c in conditions]
        errs = [summary.get((c, p), {}).get(std_key, 0) for c in conditions]
        ax.bar(xs, vals, width, yerr=errs, capsize=3, label=POLICY_LABEL.get(p, p), color=COLORS.get(p, "#999999"))
    ax.set_xticks(range(len(conditions)))
    ax.set_xticklabels([c for c in conditions])
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    watermark(plt, fig, synthetic)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_share(plt, share, path, synthetic):
    conditions = ordered([c for c, _ in share], CONDITION_ORDER)
    policies = ordered([p for _, p in share], POLICY_ORDER)
    servers = sorted({s for d in share.values() for s in d})
    palette = ["#5aa2ff", "#34c27a", "#e8b23a", "#ef5b5b", "#b07cff", "#8d99b3", "#4cc9c0", "#ff8fb1"]
    fig, axes = plt.subplots(1, len(conditions), figsize=(4.2 * len(conditions), 4.6), sharey=True, squeeze=False)
    for ax, c in zip(axes[0], conditions):
        bottoms = [0.0] * len(policies)
        for si, s in enumerate(servers):
            vals = [share.get((c, p), {}).get(s, 0.0) for p in policies]
            ax.bar(range(len(policies)), vals, bottom=bottoms, label=s, color=palette[si % len(palette)])
            bottoms = [b + v for b, v in zip(bottoms, vals)]
        ax.set_xticks(range(len(policies)))
        ax.set_xticklabels(policies, rotation=30, ha="right")
        ax.set_title(c)
    axes[0][0].set_ylabel("share of requests (%)")
    axes[0][-1].legend(title="server", loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.suptitle("Which server answered")
    watermark(plt, fig, synthetic)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_utilization(plt, util_csv, events_csv, path):
    series = defaultdict(list)
    with open(util_csv, newline="") as f:
        for r in csv.DictReader(f):
            series[r["link"]].append((float(r["time"]), float(r["utilization"])))
    if not series:
        return False
    t0 = min(t for pts in series.values() for t, _ in pts)
    fig, ax = plt.subplots(figsize=(10, 4.8))
    for link, pts in sorted(series.items()):
        if max(u for _, u in pts) < 1:          # skip links that never carried anything
            continue
        ax.plot([t - t0 for t, _ in pts], [u for _, u in pts], linewidth=1.2, label=link)
    shade = {"normal": "#8d99b3", "congestion": "#e8b23a", "failure": "#ef5b5b"}
    seen = set()
    if events_csv and os.path.exists(events_csv):
        with open(events_csv, newline="") as f:
            for r in csv.DictReader(f):
                c = r["condition"]
                ax.axvspan(float(r["start"]) - t0, float(r["end"]) - t0, color=shade.get(c, "#999999"), alpha=0.12,
                           label=None if c in seen else "run: " + c)
                seen.add(c)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("link utilization (%)")
    ax.set_ylim(0, 105)
    ax.set_title("Switch-link utilization measured automatically by the monitor")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=7, ncol=2, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------- driver
def build_outputs(raw_csv, out_dir, meta=None, util_csv=None, events_csv=None, synthetic=False):
    os.makedirs(out_dir, exist_ok=True)
    rows = load_raw(raw_csv)
    summary = summarize(rows)
    share = server_share(rows)
    write_summary_csv(summary, os.path.join(out_dir, "summary.csv"))
    plots = []
    plt = get_plt()
    if plt:
        plot_metric(plt, summary, "rt_mean", "rt_std", "mean response time (s) - lower is better",
                    "Response time by policy and condition", os.path.join(out_dir, "response_time.png"), synthetic)
        plot_metric(plt, summary, "tp_mean", "tp_std", "mean throughput (Mbps) - higher is better",
                    "Throughput by policy and condition", os.path.join(out_dir, "throughput.png"), synthetic)
        plot_share(plt, share, os.path.join(out_dir, "server_share.png"), synthetic)
        plots = ["response_time.png", "throughput.png", "server_share.png"]
        if util_csv and os.path.exists(util_csv) and plot_utilization(plt, util_csv, events_csv, os.path.join(out_dir, "utilization.png")):
            plots.append("utilization.png")
    write_report(rows, summary, share, out_dir, meta or [], synthetic, plots)
    return summary


def selftest():
    import tempfile
    random.seed(7)
    tmp = tempfile.mkdtemp(prefix="d2_selftest_")
    raw = os.path.join(tmp, "raw.csv")
    base = {"static": 6.0, "round_robin": 4.5, "least_conn": 4.4, "smart": 3.8}
    cond_factor = {"normal": 1.0, "congestion": 1.8, "failure": 1.4}
    cols = ["condition", "policy", "rep", "host", "client", "request", "file", "served_by", "bytes", "response_time",
            "throughput_mbps", "retries", "status", "timestamp"]
    with open(raw, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for c, cf in cond_factor.items():
            for p, b in base.items():
                for rep in range(1, 4):
                    for i in range(12):
                        rt = max(0.2, random.gauss(b * cf, 0.6))
                        srv = {"static": "srv1"}.get(p) or random.choice(["srv1", "srv2", "srv3"])
                        w.writerow({"condition": c, "policy": p, "rep": rep, "host": "c%d" % (i % 3 + 1), "client": i % 2 + 1,
                                    "request": i, "file": "large.bin", "served_by": srv, "bytes": 5242880,
                                    "response_time": rt, "throughput_mbps": 5242880 * 8 / rt / 1e6,
                                    "retries": 1 if (c == "failure" and random.random() < 0.1) else 0,
                                    "status": "OK" if random.random() > 0.02 else "ERROR", "timestamp": i})
    util = os.path.join(tmp, "utilization.csv")
    with open(util, "w") as f:
        f.write("time,link,utilization\n")
        for t in range(60):
            f.write("%d,s3-s1,%.1f\n%d,s3-s2,%.1f\n" % (t, 10 + (50 if 20 < t < 40 else 0), t, 5))
    events = os.path.join(tmp, "events.csv")
    with open(events, "w") as f:
        f.write("start,end,condition,policy,rep\n0,20,normal,static,1\n20,40,congestion,static,1\n40,59,failure,static,1\n")
    summary = build_outputs(raw, os.path.join(tmp, "out"), ["synthetic data, pipeline check only"], util, events, synthetic=True)
    expected = ["summary.csv", "d2_report.md"]
    if get_plt():
        expected += ["response_time.png", "throughput.png", "server_share.png", "utilization.png"]
    missing = [e for e in expected if not os.path.exists(os.path.join(tmp, "out", e))]
    print("selftest output folder:", os.path.join(tmp, "out"))
    print("groups summarised:", len(summary), "(expected 12)")
    if missing or len(summary) != 12:
        print("SELFTEST FAILED, missing:", missing)
        return 1
    print("SELFTEST OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Summarise and plot D2 experiment results")
    ap.add_argument("--raw", help="raw.csv written by experiments/run_experiments.py")
    ap.add_argument("--out", default="docs/results/d2", help="output folder")
    ap.add_argument("--util", help="utilization.csv (optional)")
    ap.add_argument("--events", help="events.csv (optional)")
    ap.add_argument("--selftest", action="store_true", help="run on synthetic data to check this script")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.raw:
        ap.error("--raw is required (or use --selftest)")
    summary = build_outputs(a.raw, a.out, util_csv=a.util, events_csv=a.events)
    print("wrote %d summary groups to %s" % (len(summary), a.out))


if __name__ == "__main__":
    main()
