"""
forwarding_app.py  --  MEMBER B: the data plane of the Smart Content Delivery System.

A Ryu (OpenFlow 1.3) application that
  * learns where hosts are (from ARP) and answers ARP itself (proxy ARP, no flooding between switches)
  * keeps a proactive routing table (table 1: eth_dst -> output port) in sync with Member C's routes
  * implements the Virtual IP (table 0): rewrites VIP <-> real server on the two EDGE switches only
  * remembers which server every TCP connection was given to (connection table)
  * cleans up flows when a server dies, resets clients that talk to a dead/unknown connection

Pipeline on every switch:
    table 0 (service)   prio 100  per-connection VIP rewrite rules          -> goto table 1
                        prio  60  LLDP                                      -> controller
                        prio  50  ARP                                       -> controller
                        prio  45  other multicast/broadcast                 -> drop
                        prio  40  IPv6                                      -> drop
                        prio  20  IPv4 to VIP (new connection)              -> controller
                        prio   0  everything else                           -> goto table 1
    table 1 (routing)   prio  10  eth_dst == host MAC                       -> output:<port>
                        prio   0  miss                                      -> controller (dropped, or forwarded once)

Everything Member B needs from Member C goes through the few _bcall(...) adapter calls (see CONTRACT.md).
"""
import collections
import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg  # noqa: E402

# ---- Ryu (or its maintained fork os-ken) -- same API, different package name ----
try:
    importlib.import_module("ryu")
    _PKG = "ryu"
except ImportError:  # pragma: no cover
    _PKG = "os_ken"


def _imp(name):
    return importlib.import_module(_PKG + "." + name)


app_manager = _imp("base.app_manager")
ofp_event = _imp("controller.ofp_event")
_handler = _imp("controller.handler")
CONFIG_DISPATCHER = _handler.CONFIG_DISPATCHER
MAIN_DISPATCHER = _handler.MAIN_DISPATCHER
DEAD_DISPATCHER = _handler.DEAD_DISPATCHER
set_ev_cls = _handler.set_ev_cls
ofproto_v1_3 = _imp("ofproto.ofproto_v1_3")
packet = _imp("lib.packet.packet")
ethernet = _imp("lib.packet.ethernet")
arp = _imp("lib.packet.arp")
ipv4 = _imp("lib.packet.ipv4")
tcp = _imp("lib.packet.tcp")
ether_types = _imp("lib.packet.ether_types")
in_proto = _imp("lib.packet.in_proto")
hub = _imp("lib.hub")
topo_event = _imp("topology.event")

ETH_IP = ether_types.ETH_TYPE_IP
ETH_ARP = ether_types.ETH_TYPE_ARP
ETH_LLDP = ether_types.ETH_TYPE_LLDP
ETH_IPV6 = ether_types.ETH_TYPE_IPV6
TCP = in_proto.IPPROTO_TCP

PRIO_CONN, PRIO_LLDP, PRIO_ARP, PRIO_MCAST, PRIO_V6, PRIO_VIP = 100, 60, 50, 45, 40, 20
PRIO_ROUTE = 10

Host = collections.namedtuple("Host", "mac ip dpid port")


class Forwarding(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(Forwarding, self).__init__(*args, **kwargs)
        self.datapaths = {}                # dpid -> datapath
        self.sw_ports = {}                 # dpid -> set(port_no)
        self.links = {}                    # (dpid, port) -> (dpid2, port2)   inter-switch ports (from LLDP)
        self.hosts = {}                    # ip -> Host
        self.installed = {}                # (dpid, mac) -> out_port           table-1 entries we pushed
        self.conns = {}                    # (client_ip, client_port, vip_port) -> dict(server, sip, smac, ts)
        self.stats = collections.Counter()
        self._sync_pending = False
        self.logger.info("Forwarding app up. VIP=%s VMAC=%s", cfg.VIP, cfg.VMAC)

    # ================================================================== adapters to Member C
    # (the ONLY place where B talks to C. If C's real code differs, fix here.)
    def _brain(self):
        return app_manager.lookup_service_brick(cfg.BRAIN_APP)

    def _bcall(self, fn, *args):
        b = self._brain()
        if b is None or not hasattr(b, fn):
            return None
        try:
            return getattr(b, fn)(*args)
        except Exception:
            self.logger.exception("brain.%s failed", fn)
            return None

    # ================================================================== callbacks FOR Member C to call
    def on_routes_changed(self, *args, **kwargs):
        """C: the topology/routes changed -> re-read next_hop_table() and fix table 1."""
        self._request_sync()

    def on_server_down(self, name):
        """C: server <name> is dead/draining-complete -> remove its connection rules."""
        n = 0
        for key, e in list(self.conns.items()):
            if e["server"] == name:
                self._del_conn_flows(key, e)
                self.conns.pop(key, None)
                n += 1
        self.stats["server_down_purged"] += n
        self.logger.info("server %s down: purged %d connections", name, n)

    on_server_removed = on_server_down

    def snapshot(self):
        """Read-only view of B's state (C or the dashboard may call this)."""
        return {
            "hosts": {ip: h._asdict() for ip, h in self.hosts.items()},
            "connections": len(self.conns),
            "route_flows": len(self.installed),
            "switches": sorted(self.datapaths),
            "stats": dict(self.stats),
        }

    # ================================================================== small OpenFlow helpers
    def _flow(self, dp, table, prio, match, inst, idle=0, flags=0, command=None):
        p, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPFlowMod(
            datapath=dp, table_id=table, priority=prio, match=match, instructions=inst,
            idle_timeout=idle, flags=flags,
            command=ofp.OFPFC_ADD if command is None else command,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))

    def _apply(self, dp, actions):
        return dp.ofproto_parser.OFPInstructionActions(dp.ofproto.OFPIT_APPLY_ACTIONS, actions)

    def _to_ctrl(self, dp):
        p, ofp = dp.ofproto_parser, dp.ofproto
        return [self._apply(dp, [p.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])]

    def _packet_out(self, dp, actions, data, in_port=None):
        p, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPPacketOut(
            datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
            in_port=ofp.OFPP_CONTROLLER if in_port is None else in_port,
            actions=actions, data=data))

    # ================================================================== switch lifecycle
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _features(self, ev):
        dp = ev.msg.datapath
        ofp, p = dp.ofproto, dp.ofproto_parser
        dpid = dp.id
        self.datapaths[dpid] = dp
        # fresh start: forget anything we believed about this switch (reconnect / new run)
        for h in [h for h in self.hosts.values() if h.dpid == dpid]:
            self._host_gone(h)
        for k in [k for k in self.installed if k[0] == dpid]:
            del self.installed[k]
        # wipe all old flows
        dp.send_msg(p.OFPFlowMod(datapath=dp, table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                                 out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY, match=p.OFPMatch()))
        goto1 = [p.OFPInstructionGotoTable(cfg.TABLE_ROUTE)]
        to_ctrl = self._to_ctrl(dp)
        T0, T1 = cfg.TABLE_SERVICE, cfg.TABLE_ROUTE
        self._flow(dp, T0, 0, p.OFPMatch(), goto1)
        self._flow(dp, T0, PRIO_V6, p.OFPMatch(eth_type=ETH_IPV6), [])
        self._flow(dp, T0, PRIO_MCAST, p.OFPMatch(eth_dst=("01:00:00:00:00:00", "01:00:00:00:00:00")), [])
        self._flow(dp, T0, PRIO_ARP, p.OFPMatch(eth_type=ETH_ARP), to_ctrl)
        self._flow(dp, T0, PRIO_LLDP, p.OFPMatch(eth_type=ETH_LLDP), to_ctrl)
        self._flow(dp, T0, PRIO_VIP, p.OFPMatch(eth_type=ETH_IP, ipv4_dst=cfg.VIP), to_ctrl)
        self._flow(dp, T1, 0, p.OFPMatch(), to_ctrl)
        dp.send_msg(p.OFPPortDescStatsRequest(dp, 0))
        self.logger.info("switch %s connected, base flows installed", dpid)
        self._request_sync()

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if dp is None:
            return
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER and self.datapaths.get(dp.id) is dp:
            del self.datapaths[dp.id]
            self.sw_ports.pop(dp.id, None)
            for k in [k for k in self.installed if k[0] == dp.id]:
                del self.installed[k]
            self.logger.info("switch %s disconnected", dp.id)

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def _port_desc(self, ev):
        dp = ev.msg.datapath
        self.sw_ports[dp.id] = set(pt.port_no for pt in ev.msg.body if pt.port_no < dp.ofproto.OFPP_MAX)

    @set_ev_cls(ofp_event.EventOFPPortStatus, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def _port_status(self, ev):
        msg = ev.msg
        dp, ofp = msg.datapath, msg.datapath.ofproto
        no = msg.desc.port_no
        if no >= ofp.OFPP_MAX:
            return
        ports = self.sw_ports.setdefault(dp.id, set())
        if msg.reason == ofp.OFPPR_ADD:
            ports.add(no)
        elif msg.reason == ofp.OFPPR_DELETE:
            ports.discard(no)
            for h in [h for h in self.hosts.values() if (h.dpid, h.port) == (dp.id, no)]:
                self._host_gone(h)
        # OFPPR_MODIFY (link up/down) is handled by LLDP link events + C's route recompute.

    # inter-switch links, from Ryu's LLDP discovery (--observe-links)
    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        l = ev.link
        self.links[(l.src.dpid, l.src.port_no)] = (l.dst.dpid, l.dst.port_no)

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_del(self, ev):
        l = ev.link
        self.links.pop((l.src.dpid, l.src.port_no), None)

    # ================================================================== host table
    def _loc_alive(self, h):
        if h.dpid not in self.datapaths:
            return False
        ports = self.sw_ports.get(h.dpid)
        return ports is None or h.port in ports

    def _learn(self, mac, ip, dpid, port):
        """Learn a host from an ARP packet. ARP is never forwarded between switches, so the
        switch/port it arrives on is the host's true attachment point."""
        if ip == cfg.VIP or ip == "0.0.0.0" or (dpid, port) in self.links:
            return
        mac = mac.lower()
        cur = self.hosts.get(ip)
        if cur is not None:
            if (cur.dpid, cur.port) == (dpid, port):
                if cur.mac == mac:
                    return
            elif self._loc_alive(cur):
                return          # first-learn wins (stops duplicates from inter-switch ports)
        h = Host(mac, ip, dpid, port)
        self.hosts[ip] = h
        self.stats["hosts_learned"] += 1
        self.logger.info("learned host %s mac=%s at s%s:%s", ip, mac, dpid, port)
        self._bcall("on_host_learned", mac, ip, dpid, port)
        self._request_sync()

    def _host_gone(self, h):
        if self.hosts.get(h.ip) is h or self.hosts.get(h.ip) == h:
            self.hosts.pop(h.ip, None)
        self.logger.info("host %s gone", h.ip)
        for key, e in list(self.conns.items()):
            if key[0] == h.ip or e["sip"] == h.ip:
                self._del_conn_flows(key, e)
                self.conns.pop(key, None)
        self._bcall("on_host_removed", h.mac, h.ip)
        self._request_sync()

    # ================================================================== table 1: proactive routing
    def _request_sync(self):
        if self._sync_pending:
            return
        self._sync_pending = True
        hub.spawn(self._sync_later)

    def _sync_later(self):
        hub.sleep(0.15)                  # debounce bursts of events into one recompute
        self._sync_pending = False
        try:
            self._sync_routes()
        except Exception:
            self.logger.exception("route sync failed")

    def _sync_routes(self):
        table = self._bcall("next_hop_table") or {}
        want = {}
        for (dpid, mac), port in table.items():
            want[(dpid, str(mac).lower())] = port
        changed = 0
        for (dpid, mac), port in want.items():
            dp = self.datapaths.get(dpid)
            if dp is None or self.installed.get((dpid, mac)) == port:
                continue
            p = dp.ofproto_parser
            self._flow(dp, cfg.TABLE_ROUTE, PRIO_ROUTE, p.OFPMatch(eth_dst=mac),
                       [self._apply(dp, [p.OFPActionOutput(port)])])
            self.installed[(dpid, mac)] = port
            changed += 1
        for key in [k for k in self.installed if k not in want]:
            dp = self.datapaths.get(key[0])
            if dp is not None:
                p, ofp = dp.ofproto_parser, dp.ofproto
                self._flow(dp, cfg.TABLE_ROUTE, PRIO_ROUTE, p.OFPMatch(eth_dst=key[1]), [],
                           command=ofp.OFPFC_DELETE_STRICT)
            del self.installed[key]
            changed += 1
        if changed:
            self.stats["route_updates"] += changed
            self.logger.info("routes synced: %d flow changes, %d route flows total", changed, len(self.installed))

    # ================================================================== packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ETH_LLDP, ETH_IPV6):
            return
        try:
            a = pkt.get_protocol(arp.arp)
            if a is not None:
                return self._arp_in(dp, in_port, msg, a)
            ip = pkt.get_protocol(ipv4.ipv4)
            if ip is None:
                return
            if ip.dst == cfg.VIP:
                return self._vip_in(dp, in_port, msg, pkt, eth, ip)
            self._miss_in(dp, in_port, msg, eth)
        except Exception:
            self.logger.exception("packet-in handling failed")

    # ---------------------------------------------------------------- ARP proxy
    def _arp_in(self, dp, in_port, msg, a):
        dpid = dp.id
        self._learn(a.src_mac, a.src_ip, dpid, in_port)
        origin = self.hosts.get(a.src_ip)
        at_origin = origin is not None and (origin.dpid, origin.port) == (dpid, in_port)
        if a.opcode == arp.ARP_REQUEST:
            tip = a.dst_ip
            if tip == a.src_ip:
                return                                   # gratuitous ARP / announcement
            if tip == cfg.VIP:
                self._arp_reply(dp, in_port, cfg.VMAC, cfg.VIP, a.src_mac, a.src_ip)
            elif tip in self.hosts:
                self._arp_reply(dp, in_port, self.hosts[tip].mac, tip, a.src_mac, a.src_ip)
            elif at_origin:
                self.stats["arp_flood"] += 1
                self._flood_host_ports(msg.data, skip=(dpid, in_port))
        elif a.opcode == arp.ARP_REPLY and at_origin:
            dst = self.hosts.get(a.dst_ip)
            if dst is not None and dst.dpid in self.datapaths:
                p = self.datapaths[dst.dpid].ofproto_parser
                self._packet_out(self.datapaths[dst.dpid], [p.OFPActionOutput(dst.port)], msg.data)

    def _arp_reply(self, dp, in_port, src_mac, src_ip, dst_mac, dst_ip):
        pk = packet.Packet()
        pk.add_protocol(ethernet.ethernet(ethertype=ETH_ARP, dst=dst_mac, src=src_mac))
        pk.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=src_mac, src_ip=src_ip,
                                dst_mac=dst_mac, dst_ip=dst_ip))
        pk.serialize()
        self._packet_out(dp, [dp.ofproto_parser.OFPActionOutput(in_port)], pk.data)
        self.stats["arp_replies"] += 1

    def _flood_host_ports(self, data, skip):
        """Send out of host-facing ports only (never inter-switch ports) => no broadcast storms."""
        for dpid, dp in list(self.datapaths.items()):
            outs = [dp.ofproto_parser.OFPActionOutput(pn)
                    for pn in sorted(self.sw_ports.get(dpid, ()))
                    if (dpid, pn) not in self.links and (dpid, pn) != skip]
            if outs:
                self._packet_out(dp, outs, data)

    # ---------------------------------------------------------------- table-1 miss
    def _miss_in(self, dp, in_port, msg, eth):
        """Route entry not there yet (race right after a host was learned): forward this one packet."""
        port = self.installed.get((dp.id, eth.dst.lower()))
        if port is not None and port != in_port:
            self._packet_out(dp, [dp.ofproto_parser.OFPActionOutput(port)], msg.data, in_port=in_port)
            self.stats["miss_forwarded"] += 1
        else:
            self.stats["miss_dropped"] += 1

    # ---------------------------------------------------------------- VIP: new connection
    def _vip_in(self, dp, in_port, msg, pkt, eth, ip):
        t = pkt.get_protocol(tcp.tcp)
        if t is None:
            return                                       # only TCP is served
        ch = self.hosts.get(ip.src)
        if ch is None or (ch.dpid, ch.port) != (dp.id, in_port):
            return                                       # not from the client's own edge port
        key = (ip.src, t.src_port, t.dst_port)
        syn = bool(t.bits & tcp.TCP_SYN) and not (t.bits & tcp.TCP_ACK)
        now = time.time()
        e = self.conns.get(key)
        if e is not None and (not syn or now - e["ts"] < 3.0) and e["sip"] in self.hosts:
            pass                                         # same connection (SYN retransmit / rule expired): same server
        elif syn:
            srv = self._bcall("select_server", ip.src)
            if not srv:
                self.stats["no_server"] += 1
                self.logger.info("no server available for %s -> RST", ip.src)
                return self._send_rst(dp, in_port, eth, ip, t)
            sh = self.hosts.get(srv["ip"])
            if sh is None:
                self.stats["server_not_located"] += 1
                self.logger.warning("server %s (%s) not located yet, dropping SYN", srv.get("name"), srv["ip"])
                return                                   # client retransmits the SYN
            e = {"server": srv["name"], "sip": srv["ip"], "smac": sh.mac, "ts": now}
            self.conns[key] = e
            self.stats["conn_new"] += 1
            self.stats["to_" + str(srv["name"])] += 1
            self._bcall("on_connection_assigned", ip.src, t.src_port, srv["name"])
            self.logger.info("conn %s:%s -> %s (%s)", ip.src, t.src_port, srv["name"], srv["ip"])
        else:
            self.stats["rst_unknown_conn"] += 1
            return self._send_rst(dp, in_port, eth, ip, t)
        sh = self.hosts.get(e["sip"])
        if sh is None or sh.dpid not in self.datapaths:
            return
        self._install_conn(key, e, ch, sh)
        e["ts"] = now
        # push the very packet that triggered us through the (now populated) pipeline
        self._packet_out(dp, [dp.ofproto_parser.OFPActionOutput(dp.ofproto.OFPP_TABLE)], msg.data, in_port=in_port)

    def _conn_matches(self, p, key, e):
        cip, cport, vport = key
        m_client = p.OFPMatch(eth_type=ETH_IP, ip_proto=TCP, ipv4_src=cip, tcp_src=cport,
                              ipv4_dst=cfg.VIP, tcp_dst=vport)
        m_server = p.OFPMatch(eth_type=ETH_IP, ip_proto=TCP, ipv4_src=e["sip"], tcp_src=vport,
                              ipv4_dst=cip, tcp_dst=cport)
        return m_client, m_server

    def _install_conn(self, key, e, ch, sh):
        """Two rules, only on the two edge switches (core switches never see per-connection state)."""
        dpc, dps = self.datapaths[ch.dpid], self.datapaths[sh.dpid]
        goto = dpc.ofproto_parser.OFPInstructionGotoTable(cfg.TABLE_ROUTE)
        # client edge: VIP -> real server
        pc = dpc.ofproto_parser
        m_client, _ = self._conn_matches(pc, key, e)
        self._flow(dpc, cfg.TABLE_SERVICE, PRIO_CONN, m_client,
                   [self._apply(dpc, [pc.OFPActionSetField(eth_dst=e["smac"]),
                                      pc.OFPActionSetField(ipv4_dst=e["sip"])]), goto],
                   idle=cfg.CONN_IDLE_TIMEOUT, flags=dpc.ofproto.OFPFF_SEND_FLOW_REM)
        # server edge: real server -> VIP
        ps = dps.ofproto_parser
        _, m_server = self._conn_matches(ps, key, e)
        self._flow(dps, cfg.TABLE_SERVICE, PRIO_CONN, m_server,
                   [self._apply(dps, [ps.OFPActionSetField(eth_src=cfg.VMAC),
                                      ps.OFPActionSetField(ipv4_src=cfg.VIP)]),
                    ps.OFPInstructionGotoTable(cfg.TABLE_ROUTE)],
                   idle=cfg.CONN_IDLE_TIMEOUT)

    def _del_conn_flows(self, key, e):
        ch, sh = self.hosts.get(key[0]), self.hosts.get(e["sip"])
        for host, which in ((ch, 0), (sh, 1)):
            if host is None:
                continue
            dp = self.datapaths.get(host.dpid)
            if dp is None:
                continue
            p, ofp = dp.ofproto_parser, dp.ofproto
            m = self._conn_matches(p, key, e)[which]
            self._flow(dp, cfg.TABLE_SERVICE, PRIO_CONN, m, [], command=ofp.OFPFC_DELETE_STRICT)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed(self, ev):
        """Client-edge rule expired/deleted -> forget the connection."""
        msg = ev.msg
        if msg.table_id != cfg.TABLE_SERVICE or msg.priority != PRIO_CONN:
            return
        try:
            m = msg.match
            if m["ipv4_dst"] == cfg.VIP:
                self.conns.pop((m["ipv4_src"], m["tcp_src"], m["tcp_dst"]), None)
        except KeyError:
            pass

    # ---------------------------------------------------------------- TCP reset to a client
    def _send_rst(self, dp, in_port, eth, ip, t):
        paylen = max(0, ip.total_length - ip.header_length * 4 - t.offset * 4)
        if t.bits & tcp.TCP_ACK:
            seq, ack, bits = t.ack, 0, tcp.TCP_RST
        else:
            seg = paylen + (1 if t.bits & tcp.TCP_SYN else 0) + (1 if t.bits & tcp.TCP_FIN else 0)
            seq, ack, bits = 0, (t.seq + seg) & 0xFFFFFFFF, tcp.TCP_RST | tcp.TCP_ACK
        pk = packet.Packet()
        pk.add_protocol(ethernet.ethernet(ethertype=ETH_IP, dst=eth.src, src=cfg.VMAC))
        pk.add_protocol(ipv4.ipv4(src=cfg.VIP, dst=ip.src, proto=TCP))
        pk.add_protocol(tcp.tcp(src_port=t.dst_port, dst_port=t.src_port, seq=seq, ack=ack,
                                bits=bits, window_size=0))
        pk.serialize()
        self._packet_out(dp, [dp.ofproto_parser.OFPActionOutput(in_port)], pk.data)
        self.stats["rst_sent"] += 1
