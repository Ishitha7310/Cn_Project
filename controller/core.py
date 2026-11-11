"""BrainCore: Member C's logic (NetState, PathEngine, Policy, Monitor) behind Member B's contract.

Pure Python, no Ryu import, so it can be unit-tested without a network (see tests/test_core.py).
brain.py wraps this in the Ryu app `Brain`.
"""
import time

from net_state import NetState
from path_engine import PathEngine
from policy import Policy
from monitor import Monitor


class BrainCore:
    def __init__(self, located_hosts=lambda: {}, notify_routes=lambda: None,
                 notify_server_down=lambda name: None, default_policy="round_robin", log=print):
        self.state = NetState()
        self.path_engine = PathEngine(self.state)
        self.policy = Policy(self.state, self.path_engine)
        self.monitor = Monitor(self.state)
        self.policy.set_policy(default_policy)
        self.policy.eligible = self._eligible
        self.located_hosts = located_hosts          # () -> {ip: Host} from Forwarding.hosts
        self.notify_routes = notify_routes          # Forwarding.on_routes_changed
        self.notify_server_down = notify_server_down  # Forwarding.on_server_down
        self.log = log
        self.ports = {}                             # (dpid_a, dpid_b) -> port on a facing b
        self.hosts = {}                             # ip -> (mac, dpid, port)

    # ------------------------------------------------------------ topology (from LLDP events)
    def link_add(self, a, b, port_a, port_b):
        old = self.state.links.get((a, b), {})
        self.state.add_link(a, b, old.get("delay", 1), old.get("utilization", 0))
        self.ports[(a, b)], self.ports[(b, a)] = port_a, port_b
        self.notify_routes()

    def link_delete(self, a, b):
        self.state.remove_link(a, b)
        self.ports.pop((a, b), None)
        self.ports.pop((b, a), None)
        self.notify_routes()

    # ------------------------------------------------------------ B -> C
    def on_host_learned(self, mac, ip, dpid, port):
        self.hosts[ip] = (mac.lower(), dpid, port)
        self.state.add_host(ip, dpid)
        for sid, s in self.state.servers.items():       # a server's switch = where its IP was located
            if s["ip"] == ip:
                self.state.set_server_switch(sid, dpid)

    def on_host_removed(self, mac, ip):
        self.hosts.pop(ip, None)
        self.state.hosts.pop(ip, None)

    def on_connection_assigned(self, client_ip, client_port, server_name):
        s = self.state.servers.get(server_name)
        if s is not None:
            s["load"] += 1          # optimistic; the next heartbeat overwrites it with the real value

    def _eligible(self, server_id):
        return self.state.servers[server_id]["ip"] in self.located_hosts()

    # ------------------------------------------------------------ C -> B
    def select_server(self, client_ip):
        name = None
        try:
            name = self.policy.select_server(client_ip)
        except Exception as e:      # e.g. 'smart' with a switch that has no links yet
            self.log("policy %s failed (%s); falling back to least_conn" % (self.policy.current_policy, e))
        if name is None and self.policy.current_policy == "smart":
            name = self.policy.least_conn(client_ip)
        if name is None:
            return None
        return {"name": name, "ip": self.state.servers[name]["ip"]}

    def next_hop_table(self):
        """{(dpid, mac): out_port}: one entry per host per switch that can reach its edge switch."""
        table, by_edge = {}, {}
        for ip, (mac, dpid, port) in self.hosts.items():
            table[(dpid, mac)] = port
            by_edge.setdefault(dpid, []).append(mac)
        in_graph = {s for link in self.state.links for s in link}
        for edge, macs in by_edge.items():
            if edge not in in_graph:
                continue
            for sw, nxt in self.state.next_hop_table(edge).items():   # C's Dijkstra (delay + k*utilization)
                port = self.ports.get((sw, nxt))
                if port is None:
                    continue
                for mac in macs:
                    table[(sw, mac)] = port
        return table

    # ------------------------------------------------------------ servers (control channel)
    def _register(self, name, ip):
        self.state.add_server(name, ip)
        self.state.activate_server(name)
        self.state.heartbeat(name)
        loc = self.hosts.get(ip)
        if loc:
            self.state.set_server_switch(name, loc[1])
        self.log("server %s registered (%s)" % (name, ip))
        self.notify_routes()

    def reap(self):
        """Run every second: heartbeat timeout -> DOWN -> tell Forwarding."""
        before = {n: s["state"] for n, s in self.state.servers.items()}
        self.state.check_server_health()
        for n, s in self.state.servers.items():
            if s["state"] == "DOWN" and before.get(n) != "DOWN":
                self.log("server %s heartbeat timeout -> DOWN" % n)
                self.notify_server_down(n)

    def handle(self, m):
        """One control message (dict) -> reply dict or None. Accepts A's format (name/action) and C's (server_id/command)."""
        t = m.get("type")
        name = m.get("name") or m.get("server_id")
        if t == "register":
            self._register(name, m["ip"])
            if m.get("switch") is not None:
                self.state.set_server_switch(name, m["switch"])
            return {"ok": True}
        if t == "heartbeat":
            if name not in self.state.servers:
                if m.get("ip"):                     # controller restarted after the server: self-register
                    self._register(name, m["ip"])
                else:
                    return None
            self.state.heartbeat(name)
            s = self.state.servers[name]
            if "active_conns" in m:
                s["load"] = m["active_conns"]
            if "tx_mbps" in m:
                s["tx_mbps"] = m["tx_mbps"]
            if s["state"] == "DRAINING" and s["load"] == 0:
                self.state.remove_server(name)
                self.notify_server_down(name)
            return None
        if t == "deregister":
            self.state.remove_server(name)
            self.notify_server_down(name)
            return {"ok": True}
        if t == "set_load":
            if name in self.state.servers:
                self.state.servers[name]["load"] = m["load"]
            return {"ok": True}
        if t == "add_link":
            self.state.add_link(m["switch1"], m["switch2"], m.get("delay", 1), m.get("utilization", 0))
            self.notify_routes()
            return {"ok": True}
        if t == "add_host":
            self.state.add_host(m["client_ip"], m["switch"])
            return {"ok": True}
        if t == "select_server":
            sel = self.select_server(m["client_ip"])
            return {"client_ip": m["client_ip"], "policy": self.policy.current_policy,
                    "selected_server": sel["name"] if sel else None}
        if t == "state":
            return self.snapshot()
        if t == "cmd":
            return self._cmd(m.get("action") or m.get("command"), m, name)
        return {"ok": False, "error": "unknown message type %r" % t}

    def _cmd(self, a, m, name):
        if a == "set_policy":
            ok = self.policy.set_policy(m["policy"])
            return {"ok": ok, "policy": self.policy.current_policy}
        if a == "get_status":
            return self.snapshot()
        if a == "drain":
            self.state.drain_server(name)
            return {"ok": True}
        if a == "activate":
            self.state.activate_server(name)
            return {"ok": True}
        if a == "remove":
            self.state.remove_server(name)
            self.notify_server_down(name)
            return {"ok": True}
        if a == "build_paths":
            return {"destination": m["destination"], "next_hops": self.path_engine.build_next_hop_table(m["destination"])}
        if a == "set_utilization":      # Monitor: EWMA-smoothed link load; recompute routes on a big change
            self.monitor.update_link_utilization(m["switch1"], m["switch2"], m["utilization"])
            if self.path_engine.should_recompute():
                self.path_engine.save_utilization()
                self.notify_routes()
            return {"ok": True}
        return {"ok": False, "error": "unknown command %r" % a}

    def snapshot(self):
        st = self.state.get_state()
        st["policy"] = self.policy.current_policy
        st["forwarding_table"] = {"%s,%s" % k: v for k, v in self.next_hop_table().items()}
        return st
