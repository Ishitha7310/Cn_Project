#!/usr/bin/env python3
"""
Presentation CONTROL PANEL: click instead of typing.  Standard library only, no root needed.

Start order (3 terminals):
    1)  ./run.sh controller
    2)  ./run.sh net                      (empty network, leave it running)
    3)  python3 ui/control.py             then open http://localhost:8081
Then click "Build D2 network" once, then "Run auto demo" (or use the buttons one by one).

It talks to the controller (TCP 9000) and to NetManager (Unix socket /tmp/netmanager.sock).
"""
import argparse
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NM_SOCK = "/tmp/netmanager.sock"
CTRL = ("127.0.0.1", 9000)
S = {"log": [], "dl": [], "banner": "Ready. Build the network first.", "demo": False,
     "ctrl": None, "util": {}, "nm_ok": False, "policy": None}
LOCK = threading.Lock()
SEQ = [0]


def log(msg, kind="info"):
    with LOCK:
        S["log"].append({"t": time.strftime("%H:%M:%S"), "msg": msg, "kind": kind})
        del S["log"][:-100]


def banner(msg):
    S["banner"] = msg
    log(msg, "step")


# ----------------------------------------------------------------------------- backends
def nm(cmd, *args, timeout=240, **opts):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(NM_SOCK)
    s.sendall((json.dumps({"cmd": cmd, "args": list(args), "opts": opts}) + "\n").encode())
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


def ctrl(msg, timeout=3):
    with socket.create_connection(CTRL, timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(msg) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            c = s.recv(65536)
            if not c:
                break
            data += c
    return json.loads(data.decode())


def set_policy(p):
    r = ctrl({"type": "cmd", "action": "set_policy", "policy": p})
    log("policy -> %s" % r.get("policy", p), "good")


def poller():
    while True:
        try:
            st = ctrl({"type": "state"}, 2)
            S["ctrl"], S["policy"] = st, st.get("policy")
        except Exception:
            S["ctrl"] = None
        try:
            S["util"] = nm("monitor", "status", timeout=2).get("utilization", {})
            S["nm_ok"] = True
        except Exception:
            S["nm_ok"] = False
        time.sleep(1)


# ----------------------------------------------------------------------------- actions
def download(host, fname, tag=""):
    """Run client.py inside a Mininet host (background + log file), wait, parse the result."""
    SEQ[0] += 1
    rec = {"id": SEQ[0], "host": host, "file": fname, "tag": tag, "policy": S["policy"], "state": "running"}
    S["dl"].append(rec)
    del S["dl"][:-30]
    path = "/tmp/ui_dl_%d.log" % rec["id"]
    try:
        nm("exec", host, "cd %s && python3 app/client.py --file %s" % (ROOT, fname), bg=True, log=path)
        for _ in range(240):
            time.sleep(0.5)
            txt = open(path).read() if os.path.exists(path) else ""
            if "Status:" in txt:
                g = lambda k: (re.search(k + r":\s+(\S+)", txt) or [None, None])[1]
                rec.update(server=g("Server"), secs=float(g("Time") or 0), mbps=float(g("Throughput") or 0),
                           retries=g("Retries"), status=g("Status"), state="done")
                log("%s %s via %s: %.2fs, %.1f Mbps, retries %s, %s" % (host, fname, rec["server"], rec["secs"],
                    rec["mbps"], rec["retries"], rec["status"]), "good" if rec["status"] == "OK" else "bad")
                return rec
            if "Traceback" in txt:
                break
        rec.update(state="done", status="FAILED", server="-", secs=0, mbps=0, retries="-")
        log("%s %s FAILED" % (host, fname), "bad")
    except Exception as e:
        rec.update(state="done", status="ERROR", server="-", secs=0, mbps=0, retries="-")
        log("download error: %s" % e, "bad")
    return rec


def build():
    banner("Building the D2 network (about 40 s)...")
    nm("gen_topo", "leaf-spine", spines=2, leaves=5, servers=3, clients=0)
    nm("add_clients", 3, on="s6")
    nm("add_host", "noisesink", on="s7")
    nm("wait_ready")
    nm("announce_all")
    nm("monitor", "on", interval=1)
    banner("Network ready: 3 servers, 3 clients, monitor ON")


def stop_noise():
    nm("exec", "c1", "pkill -f '[n]oise.py'")
    log("background noise stopped")


def reset():
    stop_noise()
    for n in (S["ctrl"] or {}).get("servers", {}):
        try:
            nm("start_server", n)
        except Exception:
            pass
    set_policy("static")
    banner("Reset done: noise off, servers restarted, policy static")


def busiest():
    sv = (S["ctrl"] or {}).get("servers", {})
    return max(sv, key=lambda n: sv[n].get("load", 0)) if sv else "srv1"


def demo():
    S["demo"] = True
    try:
        stop_noise()
        banner("1/6  Baseline: STATIC policy, clean network")
        set_policy("static")
        time.sleep(1)
        base = download("c1", "medium.bin", "1 static, clean").get("server") or "srv1"
        banner("2/6  Congest the links behind %s (18 Mbps UDP noise)" % base)
        nm("iperf_noise", base, "noisesink", secs=120, mbps=18)
        time.sleep(7)
        banner("3/6  STATIC under congestion: still sent to the busy server")
        r3 = download("c1", "medium.bin", "3 static, congested")
        banner("4/6  Switch to SMART policy: controller avoids the congested path")
        set_policy("smart")
        time.sleep(1)
        r4 = download("c1", "medium.bin", "4 smart, congested")
        banner("5/6  Load spread: 3 clients download at once")
        ts = [threading.Thread(target=download, args=(c, "medium.bin", "5 smart, 3 clients")) for c in ("c1", "c2", "c3")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        banner("6/6  Failure: crash the serving server in the middle of a download")
        t = threading.Thread(target=download, args=("c1", "large.bin", "6 crash + resume"))
        t.start()
        time.sleep(1.5)
        victim = busiest()
        nm("kill_server", victim)
        log("killed %s mid-download" % victim, "bad")
        t.join()
        time.sleep(1)
        nm("start_server", victim)
        stop_noise()
        banner("Demo finished. Congested download: static %.2fs via %s, smart %.2fs via %s."
               % (r3["secs"], r3["server"], r4["secs"], r4["server"]))
    except Exception as e:
        banner("Demo stopped: %s" % e)
        log("demo error: %s" % e, "bad")
    S["demo"] = False


def run_bg(fn, *a):
    def w():
        try:
            fn(*a)
        except Exception as e:
            log("error: %s" % e, "bad")
    threading.Thread(target=w, daemon=True).start()


def do(d):
    a = d.get("a")
    if a == "build":
        run_bg(build)
    elif a == "demo":
        if not S["demo"]:
            run_bg(demo)
    elif a == "reset":
        run_bg(reset)
    elif a == "policy":
        run_bg(set_policy, d["policy"])
    elif a == "download":
        run_bg(download, d["host"], d["file"], "manual")
    elif a == "download_all":
        for c in ("c1", "c2", "c3"):
            run_bg(download, c, d["file"], "manual x3")
    elif a == "noise":
        run_bg(lambda: (nm("iperf_noise", d["server"], "noisesink", secs=int(d.get("secs", 60)), mbps=float(d.get("mbps", 18))),
                        log("noise %s -> noisesink" % d["server"], "warn")))
    elif a == "stop_noise":
        run_bg(stop_noise)
    elif a in ("kill_server", "start_server"):
        run_bg(lambda: (nm(a, d["server"]), log("%s %s" % (a, d["server"]), "warn")))
    elif a in ("link_down", "link_up"):
        run_bg(lambda: (nm(a, d["s1"], d["s2"]), log("%s %s-%s" % (a, d["s1"], d["s2"]), "warn")))
    elif a == "clear":
        S["dl"].clear()
        S["log"].clear()


# ----------------------------------------------------------------------------- page
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>SDN CDN control</title>
<style>
:root{--bg:#0f1420;--card:#182033;--ink:#e8ecf5;--mut:#8b96ad;--ac:#4f8cff;--ok:#34c38f;--bad:#f46a6a;--warn:#f1b44c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px system-ui,sans-serif}
header{padding:14px 20px;background:#0b0f19;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0}.pill{padding:3px 10px;border-radius:99px;background:#222c45;font-size:12px}
.ok{color:var(--ok)}.bad{color:var(--bad)}
#banner{margin:14px 20px 0;padding:14px 18px;border-radius:10px;background:#1d2a4d;border-left:5px solid var(--ac);font-size:18px}
main{display:grid;grid-template-columns:340px 1fr;gap:14px;padding:14px 20px}
.card{background:var(--card);border-radius:10px;padding:14px;margin-bottom:14px}
.card h2{margin:0 0 10px;font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em}
button{background:#26324f;color:var(--ink);border:0;border-radius:7px;padding:8px 12px;margin:3px 3px 3px 0;cursor:pointer;font-size:13px}
button:hover{background:#33436b}button.p{background:var(--ac)}button.big{width:100%;padding:14px;font-size:16px;margin:0 0 8px}
button.d{background:#6b2b34}button.act{outline:2px solid var(--ok)}
select,input{background:#0f1420;color:var(--ink);border:1px solid #2c3858;border-radius:6px;padding:6px;margin:3px 3px 3px 0}
input{width:60px}table{width:100%;border-collapse:collapse}th{color:var(--mut);text-align:left;font-weight:500;font-size:12px}
td,th{padding:5px 6px;border-bottom:1px solid #243051}.bar{height:10px;border-radius:5px;background:#243051;overflow:hidden}
.bar i{display:block;height:100%;background:var(--ok)}.row{display:flex;align-items:center;gap:8px;margin:4px 0}
.row span:first-child{width:70px;color:var(--mut)}.row .bar{flex:1}
#log{max-height:200px;overflow:auto;font:12px ui-monospace,monospace}.step{color:var(--ac)}.good{color:var(--ok)}.warn{color:var(--warn)}
</style></head><body>
<header><h1>SDN Content Delivery - Control Panel</h1><span class="pill" id="pc">controller ?</span>
<span class="pill" id="pn">network ?</span><span class="pill" id="pp">policy ?</span></header>
<div id="banner">...</div>
<main><div>
 <div class="card"><h2>Automatic</h2>
  <button class="big" onclick="go({a:'build'})">1. Build D2 network</button>
  <button class="big p" onclick="go({a:'demo'})">2. Run auto demo (about 1 min)</button>
  <button onclick="go({a:'reset'})">Reset</button><button onclick="go({a:'clear'})">Clear results</button></div>
 <div class="card"><h2>Policy</h2><div id="pol"></div></div>
 <div class="card"><h2>Download through the virtual IP</h2>
  <select id="hc"><option>c1</option><option>c2</option><option>c3</option></select>
  <select id="fl"><option>small.bin</option><option selected>medium.bin</option><option>large.bin</option></select>
  <button class="p" onclick="go({a:'download',host:hc.value,file:fl.value})">Download</button>
  <button onclick="go({a:'download_all',file:fl.value})">All 3 clients</button></div>
 <div class="card"><h2>Congestion</h2>
  <select id="ns"></select> Mbps <input id="nmb" value="18">
  <button class="d" onclick="go({a:'noise',server:ns.value,mbps:nmb.value,secs:90})">Start noise</button>
  <button onclick="go({a:'stop_noise'})">Stop</button></div>
 <div class="card"><h2>Failures</h2>
  <select id="ks"></select><button class="d" onclick="go({a:'kill_server',server:ks.value})">Crash server</button>
  <button onclick="go({a:'start_server',server:ks.value})">Restart</button><br>
  Link <input id="l1" value="s3"> <input id="l2" value="s1">
  <button class="d" onclick="go({a:'link_down',s1:l1.value,s2:l2.value})">Down</button>
  <button onclick="go({a:'link_up',s1:l1.value,s2:l2.value})">Up</button></div>
</div><div>
 <div class="card"><h2>Content servers (from the controller)</h2><table id="sv"></table></div>
 <div class="card"><h2>Link utilization (measured)</h2><div id="ut"></div></div>
 <div class="card"><h2>Download results</h2><table id="dl"></table></div>
 <div class="card"><h2>Event log</h2><div id="log"></div></div>
</div></main>
<script>
const $=id=>document.getElementById(id);
const go=b=>fetch('/api/do',{method:'POST',body:JSON.stringify(b)});
$('pol').innerHTML=['static','round_robin','least_conn','smart'].map(p=>`<button data-p="${p}" onclick="go({a:'policy',policy:'${p}'})">${p}</button>`).join('');
let opts='';
function fillSel(names){const k=names.join();if(k===opts)return;opts=k;
 for(const id of['ns','ks']){const v=$(id).value;$(id).innerHTML=names.map(n=>`<option>${n}</option>`).join('');if(names.includes(v))$(id).value=v;}}
async function tick(){try{const s=await (await fetch('/api/status')).json();
 $('banner').textContent=s.banner;
 $('pc').innerHTML='controller '+(s.ctrl?'<b class=ok>online</b>':'<b class=bad>offline</b>');
 $('pn').innerHTML='network '+(s.nm_ok?'<b class=ok>online</b>':'<b class=bad>offline</b>');
 $('pp').textContent='policy: '+(s.policy||'?');
 document.querySelectorAll('#pol button').forEach(b=>b.classList.toggle('act',b.dataset.p===s.policy));
 const sv=(s.ctrl&&s.ctrl.servers)||{};const names=Object.keys(sv).sort();fillSel(names.length?names:['srv1','srv2','srv3']);
 $('sv').innerHTML='<tr><th>Server<th>State<th>Active conns<th>Switch</tr>'+names.map(n=>{const v=sv[n];
  return `<tr><td>${n}<td class="${v.state=='ACTIVE'?'ok':'bad'}">${v.state}<td>${v.load??0}<td>${v.switch??''}</tr>`}).join('');
 const u=Object.entries(s.util||{});$('ut').innerHTML=u.length?u.map(([k,v])=>`<div class=row><span>${k}</span><div class=bar><i style="width:${Math.min(v,100)}%;background:${v>70?'var(--bad)':v>35?'var(--warn)':'var(--ok)'}"></i></div><span>${(+v).toFixed(0)}%</span></div>`).join(''):'<i>monitor off or no data yet</i>';
 $('dl').innerHTML='<tr><th>#<th>Step<th>Client<th>File<th>Policy<th>Server<th>Time (s)<th>Mbps<th>Retries<th>Status</tr>'+s.dl.slice().reverse().map(r=>
  `<tr><td>${r.id}<td>${r.tag}<td>${r.host}<td>${r.file}<td>${r.policy||''}<td>${r.server||'...'}<td>${r.secs!=null?r.secs.toFixed(2):'...'}<td>${r.mbps!=null?r.mbps.toFixed(1):''}<td>${r.retries??''}<td class="${r.status=='OK'?'ok':r.state=='running'?'':'bad'}">${r.status||'running'}</tr>`).join('');
 $('log').innerHTML=s.log.slice().reverse().map(l=>`<div class="${l.kind}">${l.t}  ${l.msg}</div>`).join('');
}catch(e){}}
setInterval(tick,1000);tick();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/status":
            self.send(json.dumps(S, default=str).encode(), "application/json")
        else:
            self.send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            do(json.loads(self.rfile.read(n) or b"{}"))
        except Exception as e:
            log("bad request: %s" % e, "bad")
        self.send(b"{}", "application/json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    a = ap.parse_args()
    threading.Thread(target=poller, daemon=True).start()
    print("Control panel: http://localhost:%d" % a.port)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
