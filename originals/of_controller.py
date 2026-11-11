from os_ken.base import app_manager
import socket
import json
import threading
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER
from os_ken.controller.handler import MAIN_DISPATCHER
from os_ken.controller.handler import set_ev_cls
from os_ken.controller import ofp_event
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet
from os_ken.lib.packet import ethernet
from os_ken.lib.packet import ether_types


class SimpleSwitch(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.forwarding_table = {}

        self.table_thread = threading.Thread(
            target=table_server,
            args=(self,),
            daemon=True
        )
        self.table_thread.start()

    def get_next_hop_table(self):
        return dict(self.forwarding_table)

    def print_forwarding_table(self):
        print("\n========== FORWARDING TABLE ==========")

        for (dpid, mac), port in self.forwarding_table.items():
            print(
                "(s" + str(dpid) + ",",
                mac + ") ->",
                port
            )

        print("======================================")
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):

        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        request = parser.OFPPortDescStatsRequest(datapath, 0)
        datapath.send_msg(request)
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]

        self.add_flow(datapath, 0, match, actions)

        print("Switch connected:", datapath.id)

    def add_flow(self, datapath, priority, match, actions):

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions
        )

        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):

        msg = ev.msg
        datapath = msg.datapath

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src

        dpid = datapath.id

        self.mac_to_port.setdefault(dpid, {})

        # Learn source MAC
        self.mac_to_port[dpid][src] = in_port
        self.forwarding_table[(dpid,src)]=in_port
        print(
            "Forwarding entry:",
            "(s" + str(dpid) + ",",
            src + ") ->",
            in_port
        )
        print(
            "Learned MAC:",
            src,
            "on switch s" + str(dpid),
            "port",
            in_port
)

        # Find output port
        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [
            parser.OFPActionOutput(out_port)
        ]

        # Install flow if destination is known
        if out_port != ofproto.OFPP_FLOOD:

            match = parser.OFPMatch(
                in_port=in_port,
                eth_dst=dst
            )

            self.add_flow(
                datapath,
                1,
                match,
                actions
            )

        # Send packet
        data = None

        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )

        datapath.send_msg(out)
    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_stats_reply_handler(self, ev):

        datapath = ev.msg.datapath
        dpid = datapath.id

        print("\nPort information for switch s" + str(dpid))

        for port in ev.msg.body:
            if port.port_no < datapath.ofproto.OFPP_MAX:
                print(
                    "Port:",
                    port.port_no,
                    "MAC:",
                    port.hw_addr
                )


def table_server(app):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    server.bind(("127.0.0.1", 9100))
    server.listen(5)

    print("Table server listening on port 9100")

    while True:
        client, address = server.accept()

        try:
            data = client.recv(1024)

            if data:
                request = json.loads(data.decode())

                if request.get("type") == "get_table":
                    table = app.get_next_hop_table()
                    print("SDN forwarding table requested")

                    response = {}

                    for (dpid, mac), port in table.items():
                        key = str(dpid) + "," + mac
                        response[key] = port

                    client.send(
                        json.dumps(response).encode()
                    )

        except Exception as e:
            print("Table server error:", e)

        finally:
            client.close()
