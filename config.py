"""Shared constants. Everything that more than one component must agree on lives here.

Teammates: if your code uses a different value, change it HERE (one place).
"""
import os

REPO = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(REPO, "logs")

# ---- the virtual service (what clients connect to) -------------------------
VIP = "10.0.255.100"            # virtual IP, no real machine owns it
VMAC = "02:00:00:00:ff:64"      # virtual MAC the controller answers ARP with
SERVICE_PORT = 8000             # TCP port of the content service

# ---- data network (OpenFlow switches) --------------------------------------
PREFIX = 16                     # hosts live in 10.0.0.0/16

# ---- management network (plain OVS bridge "sm", NO controller) -------------
# Servers get a 2nd interface here so heartbeats never compete with data traffic.
MGMT_NET = "192.168.100"        # servers: .1 - .250
MGMT_PREFIX = 24
MGMT_CTRL_IP = "192.168.100.254"  # the controller machine (root namespace)
CTRL_PORT = 9000                # TCP port of Member C's control server

# ---- OpenFlow --------------------------------------------------------------
OF_IP = "127.0.0.1"
OF_PORT = 6653
# "kernel" = normal Open vSwitch. Use "user" only if the kernel module is missing.
DATAPATH = os.environ.get("NM_DATAPATH", "kernel")

# ---- NetManager command socket ---------------------------------------------
NM_SOCK = "/tmp/netmanager.sock"

# ---- names of the Ryu apps (used to find each other inside ryu-manager) ----
FWD_APP = "Forwarding"          # Member B  (forwarding_app.py)
BRAIN_APP = "Brain"             # Member C  (the controller brain)

# ---- how NetManager starts a content server inside a Mininet host ----------
# Member A's real server (app/server.py); content comes from data/content (run app/make_content.py once).
SERVER_CMD = ("python3 {repo}/app/server.py --id {name} --ip {ip} "
              "--ctrl {ctrl_ip}:{ctrl_port} --port {port} --capacity-mbps 20")

# ---- flow-table layout -----------------------------------------------------
TABLE_SERVICE = 0               # VIP rewrite (per-connection, edge switches only)
TABLE_ROUTE = 1                 # plain forwarding by destination MAC
CONN_IDLE_TIMEOUT = 30          # seconds without packets before a connection rule expires


def host_ip(i):
    """i-th host (1-based) -> 10.0.<hi>.<lo>, lo in 1..250. Deterministic."""
    hi, lo = divmod(i - 1, 250)
    return "10.0.%d.%d" % (hi, lo + 1)


def host_mac(i):
    hi, lo = divmod(i - 1, 250)
    return "02:00:0a:00:%02x:%02x" % (hi, lo + 1)


def mgmt_ip(i):
    return "%s.%d" % (MGMT_NET, i)
