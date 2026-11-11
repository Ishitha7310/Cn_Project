"""Member C's controller brain as a Ryu app named `Brain` (what B's Forwarding app looks up).

Run:  ryu-manager --observe-links forwarding_app.py controller/brain.py
"""
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)
import config as cfg  # noqa: E402
from core import BrainCore  # noqa: E402

try:
    importlib.import_module("ryu")
    _PKG = "ryu"
except ImportError:  # C originally used os_ken
    _PKG = "os_ken"


def _imp(n):
    return importlib.import_module(_PKG + "." + n)


app_manager = _imp("base.app_manager")
set_ev_cls = _imp("controller.handler").set_ev_cls
ofproto_v1_3 = _imp("ofproto.ofproto_v1_3")
hub = _imp("lib.hub")
topo_event = _imp("topology.event")
_BaseApp = getattr(app_manager, "RyuApp", None) or getattr(app_manager, "OSKenApp")


class Brain(_BaseApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(Brain, self).__init__(*args, **kwargs)
        self.core = BrainCore(
            located_hosts=lambda: (self._fwd().hosts if self._fwd() else {}),
            notify_routes=self._routes_changed,
            notify_server_down=self._server_down,
            default_policy=os.environ.get("BRAIN_POLICY", "round_robin"),
            log=self.logger.info)
        hub.spawn(self._listen)
        hub.spawn(self._reaper)
        self.logger.info("Brain up (control port %s, policy %s)", cfg.CTRL_PORT, self.core.policy.current_policy)

    def _fwd(self):
        return app_manager.lookup_service_brick(cfg.FWD_APP)

    def _routes_changed(self):
        f = self._fwd()
        if f is not None:
            f.on_routes_changed()

    def _server_down(self, name):
        f = self._fwd()
        if f is not None:
            f.on_server_down(name)

    # ---- topology discovery (Ryu LLDP events)
    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        l = ev.link
        self.logger.info("Brain: link s%s:%s -> s%s:%s", l.src.dpid, l.src.port_no, l.dst.dpid, l.dst.port_no)
        self.core.link_add(l.src.dpid, l.dst.dpid, l.src.port_no, l.dst.port_no)

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_del(self, ev):
        self.core.link_delete(ev.link.src.dpid, ev.link.dst.dpid)

    @set_ev_cls(topo_event.EventSwitchLeave)
    def _sw_leave(self, ev):
        dpid = ev.switch.dp.id
        for (a, b) in [k for k in self.core.ports if k[0] == dpid]:
            self.core.state.remove_link(a, b)
            self.core.ports.pop((a, b), None)
            self.core.ports.pop((b, a), None)
        self._routes_changed()

    # ---- contract: B -> C
    def on_host_learned(self, mac, ip, dpid, port):
        self.core.on_host_learned(mac, ip, dpid, port)

    def on_host_removed(self, mac, ip):
        self.core.on_host_removed(mac, ip)

    def on_connection_assigned(self, client_ip, client_port, server_name):
        self.core.on_connection_assigned(client_ip, client_port, server_name)

    def select_server(self, client_ip):
        return self.core.select_server(client_ip)

    def next_hop_table(self):
        t = self.core.next_hop_table()
        if len(t) != getattr(self, "_last_n", -1):      # log only when the size changes
            self._last_n = len(t)
            self.logger.info("Brain: next_hop_table has %d entries (%d hosts, %d links)",
                             len(t), len(self.core.hosts), len(self.core.state.links) // 2)
        return t

    # ---- control server: JSON lines over TCP 0.0.0.0:9000 (servers register/heartbeat; ctl.py sends commands)
    def _listen(self):
        hub.StreamServer(("0.0.0.0", cfg.CTRL_PORT), self._handle).serve_forever()

    def _handle(self, sock, addr):
        buf = b""
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
                lines = []
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    lines.append(line)
                if buf.strip():                 # tolerate a JSON message with no trailing newline (old ctl.py)
                    try:
                        json.loads(buf.decode())
                        lines.append(buf)
                        buf = b""
                    except ValueError:
                        pass
                for line in lines:
                    if not line.strip():
                        continue
                    reply = self.core.handle(json.loads(line.decode()))
                    if reply is not None:
                        sock.sendall((json.dumps(reply) + "\n").encode())
        except Exception as e:
            self.logger.debug("control connection %s ended: %s", addr, e)
        finally:
            sock.close()

    def _reaper(self):
        while True:
            hub.sleep(1)
            self.core.reap()
