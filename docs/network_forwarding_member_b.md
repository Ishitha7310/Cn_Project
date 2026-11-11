# Smart Content Delivery System: Member B (dynamic network + forwarding)

Clients download files over raw TCP from **one virtual service address** (`10.0.255.100:8000`).
An SDN controller decides which real server handles each connection and keeps traffic flowing when links or servers fail.
This repo part is **Member B**: the Mininet network that can change while running (`netmanager.py`) and the Ryu
forwarding application that implements the virtual IP and routing in the switches (`forwarding_app.py`).
Member A (servers, clients, measurements) and Member C (server selection, monitoring, routing decisions) plug in through
[`CONTRACT.md`](CONTRACT.md).

## 1. Files

| File | What it is |
|---|---|
| `netmanager.py` | Owns the live Mininet network. Adds/removes switches, links, hosts, servers and clients **at runtime**; injects failures and congestion; topology generators; command prompt + Unix-socket API. |
| `forwarding_app.py` | Ryu OpenFlow 1.3 app (name `Forwarding`): host learning, proxy ARP, VIP rewrite, routing flows, connection table, cleanup. |
| `config.py` | Shared constants (VIP, ports, subnets, how servers are launched). |
| `CONTRACT.md` | The exact interfaces between A, B and C. |
| `setup.sh`, `run.sh`, `requirements.txt` | Installation and running. |
| `mocks/` | **Test stand-ins for A and C** (fake brain, server, client, automated smoke test). Not part of Member B. |

## 2. Install and run

```bash
./setup.sh              # once: apt packages + Python 3.9 venv with Ryu
./run.sh test           # automated end-to-end check (about 3 minutes); prints PASS/FAIL per feature
```
Manual / demo mode, two terminals:
```bash
./run.sh controller     # terminal 1: Ryu with Forwarding + (mock) brain
./run.sh net --run "gen_topo leaf-spine --spines 2 --leaves 3 --servers 3 --clients 3"     # terminal 2
```
Terminal 2 gives a prompt `nm>`. Type `help`. To use the real Member C file: `BRAIN=path/to/brain.py ./run.sh controller`.
If Open vSwitch has no kernel module on your machine: `export NM_DATAPATH=user` (slower, works everywhere).
Cleanup after a crash: `./run.sh clean`.

## 3. Architecture

```
                 +--------------------- Ryu (one process) -----------------------+
                 |  Forwarding (B)  <---- contract ---->  Brain (C)              |
                 |  ARP proxy, host table                 topology graph, routes  |
                 |  VIP rewrite, conn table               server registry/policy  |
                 +------^------------------------------------------^--------------+
        OpenFlow 1.3    |                                          | JSON/TCP :9000
   +--------------------+--------------+                  (management network, plain bridge "sm")
   |  Mininet (netmanager.py)           |                           |
   |  s1 .. sN  OVS switches + links    |   servers have 2 interfaces: data (10.0.x.y) + mgmt (192.168.100.n)
   |  srv1.. c1..  hosts = real Linux   |
   +------------------------------------+
```
Nothing about the topology is hard-coded. Switches/links are discovered by LLDP, hosts by their first ARP, servers by
their `register` message. `add_server` at runtime is all it takes for a new server to start receiving traffic.

### Flow tables (on every switch)
```
table 0  "service"   prio 100  per-connection VIP rewrite (edge switches only)   -> goto 1
                     prio  60  LLDP -> controller      prio 50  ARP -> controller
                     prio  45  other multicast/broadcast -> drop     prio 40  IPv6 -> drop
                     prio  20  IPv4 to VIP (= a new connection) -> controller
                     prio   0  anything else -> goto 1
table 1  "routing"   prio  10  eth_dst = host MAC -> output:port      (one entry per host per switch)
                     prio   0  miss -> controller (forwarded once if a route exists, else dropped)
```
See it live: `flows s3 --table 0` and `flows s3 --table 1` (equivalent to `ovs-ofctl -O OpenFlow13 dump-flows s3`).

### One request, step by step
1. Client does `connect(10.0.255.100, 8000)`. It first ARPs for the VIP -> table 0 sends the ARP to the controller, which
   **answers itself** with the virtual MAC `02:00:00:00:ff:64` (proxy ARP). No broadcast ever crosses a switch link.
2. The TCP SYN matches `prio 20` -> Packet-In. `Forwarding` asks C `select_server(client_ip)` and stores
   `(client_ip, client_port) -> server` in its **connection table**.
3. It installs two rules, **only on the client's edge switch and the server's edge switch**:
   * client edge: `match client->VIP`, `set eth_dst=server MAC, ipv4_dst=server IP`, `goto table 1`
   * server edge: `match server->client`, `set eth_src=VMAC, ipv4_src=VIP`, `goto table 1`
   and re-injects the SYN into the pipeline (`output:TABLE`). The client never sees the real server.
4. Table 1 forwards by destination MAC along the path C computed. The rest of the download stays in the data plane.
5. Idle connections expire after 30 s (`idle_timeout`); a Flow-Removed message deletes the connection-table entry.

### Why two tables (the key design decision)
* **Routing** (table 1) is *proactive* and independent of connections: `hosts x switches` entries, no matter how many
  clients or connections exist. **Core switches never hold per-connection state**, so it scales.
* **Service rewrite** (table 0) is *reactive* and only on two edge switches.
* Because they are separate, a **link failure only touches table 1**. The rewrite rules and the TCP connection are not
  touched, so a download survives a link failure with the same TCP connection (verified: 0 reconnects).

### Failure handling
| Event | Detection | What happens |
|---|---|---|
| Link down / up / added / removed | Ryu LLDP link event -> C recomputes -> `on_routes_changed()` | B diffs `next_hop_table()` against installed flows, rewrites only changed entries (~0.2 s). TCP continues. |
| Server crash | C's heartbeat timeout -> `on_server_down(name)` | B deletes that server's connection rules; further packets of those connections get a TCP RST; clients reconnect to the VIP and are given a live server (resume from byte offset). A *killed* process also resets connections immediately via the server's own kernel. |
| Server hang (`stop_server`) | heartbeat timeout | same as crash, after the timeout. |
| No server available | `select_server` returns `None` | B answers the SYN with a TCP RST (fast failure instead of a timeout). |
| Host removed | OpenFlow port-delete event | host, its connections and routes are cleaned up. |

### Loop safety
The topology can contain loops (ring, random). B **never floods between switches**: unknown ARP targets are flooded
only out of host-facing ports, and only if the request came from the sender's true attachment port. So there is no
broadcast storm for any topology shape and no spanning tree is needed.

## 4. NetManager cheat sheet (prompt, `ctl`, or Python)

```
gen_topo leaf-spine --spines 2 --leaves 4 --servers 6 --clients 12      ring / random: --switches 8
add_server --on s5                 add_clients 4 --on s3            remove_host srv2
add_link s3 s4 --bw 20 --delay 2   del_link s3 s4                   set_link s3 s1 --bw 2 --delay 40 --loss 1
link_down s3 s1    link_up s3 s1   kill_server srv1 (crash)         stop_server srv1 (hang)    start_server srv1
iperf_noise c2 c3 --secs 20 --mbps 8                                  pingall    wait_ready    info
c1 python3 mocks/client.py --file large          (any command on a host)    flows s3 --table 0
```
From another terminal while `serve` runs: `python3 netmanager.py ctl add_server --on s3`.

## 5. Demo script (about 5 minutes)

1. `./run.sh controller`, then `./run.sh net --run "gen_topo leaf-spine --spines 2 --leaves 3 --servers 3 --clients 3"`.
   *Show:* `info` (topology), `pingall` (everything reachable, nothing hard-coded).
2. `c1 python3 mocks/client.py --file large` ... *Show:* the VIP is used; then `flows s3 --table 0`: the rewrite rules
   (`set_field ... ->ip_dst`, `->ip_src`) exist only on the edge switches. `flows s1 --table 0` (a spine) has none.
3. **Scale-out:** `add_server --on s5`; a few more fetches -> `served_by` now includes the new server. No restart, no config.
4. **Link failure:** `exec c1 "python3 mocks/client.py --file large" --bg --log /tmp/res.txt`, then immediately
   `link_down s3 s1` (use the link that carries the traffic, shown by `flows s3 --table 1`); later `cat /tmp/res.txt`:
   transfer completed, `reconnects: 0`.
5. **Server crash:** start a large download, `kill_server srv1`; the client reconnects and resumes on another server.
6. **Congestion:** `set_link s3 s1 --bw 2` and `iperf_noise c2 c3 --secs 20 --mbps 8` (Member C's policy reacts to the load).
7. **Bigger network:** `gen_topo leaf-spine --spines 3 --leaves 6 --servers 6 --clients 14` (9 switches, 20 hosts).

## 6. Rubric mapping (Member B's share)

| Rubric item | Where |
|---|---|
| Mininet + SDN implementation | `netmanager.py` (runtime topology), `forwarding_app.py` (OpenFlow 1.3 pipeline, flow rules) |
| Socket-SDN integration | Servers' TCP register/heartbeat -> C -> B; connection assignment and `on_server_down` change flows; new TCP connection = Packet-In |
| Dynamic SDN | Route recompute on link events, proactive diff-based flow updates, dead-server flow cleanup, runtime scale-out |
| Challenging scenario | link failure, server crash/hang, congestion tools, big topologies |
| Documentation / viva | this file; `CONTRACT.md` |

## 7. Viva cheat sheet (Member B)

* **What is the VIP and why?** One fake address hides the server pool; adding/removing servers is invisible to clients.
* **Who answers ARP for the VIP?** The controller (proxy ARP) with a virtual MAC; no machine owns the VIP.
* **Where is the rewrite done?** In the OpenFlow switches (`set_field`), at the two edge switches. Return traffic is rewritten back so the client believes it talks to the VIP.
* **Why not per-connection rules on every hop?** Table size would grow with connections x hops. With a MAC-routed underlay only the edges hold per-connection state.
* **What happens on link failure?** LLDP link-delete -> C recomputes -> B rewrites changed table-1 entries; table 0 untouched, TCP survives.
* **Why no broadcast storm in a ring?** ARP is answered by the controller; unknown requests are flooded only to host ports, never switch-to-switch.
* **How does a new server get traffic?** It registers (TCP) with C, announces itself with an ARP so B learns its location, then `select_server` can return it.
* **What is the connection table for?** Same client connection -> same server (retransmitted SYN, rule expired mid-stream) and cleanup when a server dies.
* **Reactive vs proactive?** Server choice is reactive (first packet); routing is proactive (installed before traffic).
* **Table-miss / Packet-In / Flow-Mod / Flow-Removed / Port-Status?** See the code comments in `forwarding_app.py`.

## 8. Limits and notes

* Tested with the mock brain/server/client in `mocks/`. The real A and C only need to follow `CONTRACT.md`.
* Mininet on one laptop comfortably handles tens of hosts (20 hosts / 9 switches in the scale test); for heavier load,
  run many client threads per host rather than many hosts.
* After a controller restart, hosts that already have the VIP in their ARP cache only reappear after their cache entry
  expires or after `announce_all`.
* Ryu needs Python <= 3.9-ish dependencies, hence the `.venv` built by `setup.sh`. Mininet itself runs on the system Python.
