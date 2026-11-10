# Smart Content Delivery System: SDN-Based Content Delivery (Project 19, Deliverable 2)

Multiple TCP content servers sit behind **one virtual service address**. An SDN controller chooses the server for every new
connection, keeps traffic flowing when links or servers fail, and tracks server health. Everything runs in Mininet with an
OpenFlow 1.3 Ryu controller.

| Team member | SRN | Contribution area | Where |
|---|---|---|---|
| Ishitha (Member A + C) | PES1UG25CS216 | TCP content servers and clients, workload generator, performance plots; controller brain (server registry, health monitoring, selection policies, routing logic and utilization monitoring); Mininet topology configuration/testing; VIP and ARP-proxy integration | `app/`, `controller/`, topology configuration/testing, selected VIP/ARP integration work |
| Hruthik (Member B) | PES1UG25CS211 | Mininet network manager implementation and Ryu forwarding application, including OpenFlow flow-rule installation and packet forwarding | `netmanager.py`, `forwarding_app.py`, `config.py` |

The team has two members. Ishitha is responsible for Member A and Member C, plus selected Member B tasks (Mininet topology configuration/testing and VIP/ARP-proxy integration). Hruthik remains responsible for the core Ryu forwarding implementation and OpenFlow flow-rule installation. Keep `CONTRIBUTIONS.md` consistent with this division.

## 1. Problem and objectives
Clients should fetch files from a service without knowing which physical server answers. The network, not the clients, decides.

Project requirements and where each is met:

| Requirement | Implementation |
|---|---|
| TCP content requests | `app/client.py` and `app/server.py` (raw `socket`, thread per connection) |
| Multiple content servers | `srv1..srvN`, started inside Mininet hosts |
| Server availability | servers register and send a heartbeat every 1 s; no heartbeat for 3 s means DOWN |
| Select appropriate servers | `controller/policy.py`: static, round_robin, least_conn, smart (load + path delay + utilization) |
| Dynamically modify SDN forwarding | on link or server events the controller recomputes routes and rewrites only the changed flows |
| Simulate server/path failures | `kill_server`, `stop_server`, `link_down`, `set_link`, `iperf_noise` in NetManager |
| Monitor network conditions | `controller/monitor.py` (EWMA link utilization) feeds route cost; collected automatically by `monitor_poller.py` (D2, `nm> monitor on`) |
| Measure response time and throughput | `app/workload.py` and `app/plot_results.py` (D2 experiments) |

## 2. Architecture
```
 Clients c1..cN                                         Content servers srv1..srvN
 connect to VIP 10.0.255.100:8000                       app/server.py, port 8000
        |                                                  |            ^
        |  TCP data (data network, OpenFlow switches)      |            | register / heartbeat
        v                                                  v            | JSON lines, TCP :9000
 +--------------- Mininet: leaf-spine (OVS, OpenFlow 1.3) ---------------+  management network
 |   s1..sN, links with bandwidth / delay / loss, changed at runtime     |          |
 +----------------------------^------------------------------------------+          |
                              | OpenFlow 1.3                                         |
          +-------------------+------------------------------------------------------v--+
          |  Ryu process                                                                  |
          |  Forwarding (B): ARP proxy, VIP rewrite, flow rules, connection table         |
          |        ^  select_server / next_hop_table / host events (contract)             |
          |        v                                                                      |
          |  Brain (C): server registry + health, policies, Dijkstra routes, monitor      |
          +--------------------------------------------------------------------------------+
```

**Communication flow of one request**
1. Client ARPs for the VIP; the controller answers with a virtual MAC (no host owns the VIP).
2. Client sends a TCP SYN to the VIP; the switch has no per-connection rule, so it sends a Packet-In to the controller.
3. `Forwarding` asks `Brain.select_server(client_ip)`. The policy picks an ACTIVE server whose location is known.
4. `Forwarding` installs rewrite rules on the client's and the server's edge switches (VIP to server address and back). The client never sees the real server.
5. Later packets follow the routing table (table 1, by destination MAC) computed by `Brain`.
6. The server answers `OK <size> <offset> <server_name>` followed by the bytes; the client resumes from the byte offset after any interruption.

**Application protocol (TCP):** `GET <file> <offset>\n` returns `OK <size> <offset> <server>\n` + bytes, or `ERR <code> <msg>\n`
(`NOT_FOUND`, `BAD_OFFSET`, `INVALID_PATH`, `BUSY`, `DRAINING`).
**Control protocol (TCP, JSON lines, port 9000):** `register`, `heartbeat`, `deregister`, `cmd` (`set_policy`, `drain`, `get_status`, ...).
The interface between the three parts is specified in `CONTRACT.md`.

**Expected network behavior:** new connections are spread over healthy servers; a link failure changes only routing entries and
the TCP download continues; a dead server is detected within 3 s, its connections are reset and clients reconnect and resume.

## 3. Reproduce (Ubuntu with Mininet and Open vSwitch)
```bash
./setup.sh                          # once: apt packages + Python 3.9 venv with Ryu
python3 app/make_content.py         # small.bin 100 KB, medium.bin 1 MB, large.bin 5 MB
python3 tests/test_core.py          # logic test, no root needed
./run.sh test                       # full automated end-to-end check in Mininet
```
Manual demo, two terminals:
```bash
./run.sh controller                                                    # terminal 1
./run.sh net --run "gen_topo leaf-spine --spines 2 --leaves 3 --servers 3 --clients 3"   # terminal 2 (nm> prompt)
```
Policy and status from a third terminal: `python3 controller/ctl.py state | policy least_conn | drain srv2`.
Cleanup after a crash: `./run.sh clean`.

## 4. Test cases (D1)
| # | Test | How | Expected result |
|---|---|---|---|
| T1 | Controller logic and application protocol | `python3 tests/test_core.py` | 18 checks print PASS |
| T2 | Topology up, all hosts reachable | `nm> pingall` | 0 lost |
| T3 | Download through the VIP | `nm> c1 python3 app/client.py --file large.bin` | completes; output names the serving server |
| T4 | Server registration and health | `python3 controller/ctl.py state` | every `srvN` is ACTIVE |
| T5 | Load distribution | run T3 from c1, c2, c3 several times | more than one server appears as the serving server |
| T6 | Rewrite only at the edges | `nm> exec c1 "python3 app/client.py --file large.bin" --bg`, then `nm> flows s3 --table 0` and `nm> flows s1 --table 0` (rules expire 30 s after the last packet) | `set_field` rewrite rules on the edge switch, none on the spine |
| T7 | Runtime scale-out | `nm> add_server --on s5`, then more downloads | new server starts serving without a restart |
| T8 | Link failure (bonus) | `nm> exec c1 "python3 app/client.py --file large.bin > /tmp/t8.log 2>&1" --bg`, then `nm> link_down s3 s1`, then `nm> c1 cat /tmp/t8.log` (the serving server must be on another leaf than c1) | download completes (`Status: OK`) over the other spine |
| T9 | Server crash (bonus) | start a large download, then `nm> kill_server srv1` | client reconnects to another server and resumes |

Record the outputs or screenshots of T2 to T9 in `docs/results/` for the demo.

## 5. Demo script (about 5 minutes)
1. Show the architecture diagram (section 2) and explain the VIP idea in one sentence.
2. Start the controller and the network; run `info` and `pingall` (T2).
3. Download a file from c1 through the VIP (T3); point out that the client only knows `10.0.255.100`.
4. Show `ctl.py state` with the registered servers (T4); download from several clients (T5).
5. Show the flow tables of an edge switch and a spine (T6).
6. Optional: add a server (T7), break a link (T8), crash a server (T9).

## 6. Status and plan
* D1 scope: sections 1 to 5, with measured results in section 7. T7, T8 (link failure mid-download) and T9 (server crash with resume) are verified by `./run.sh test`.
* D2: automatic link-utilization monitoring, the `smart` policy under congestion, and a comparison against a static baseline (`./run.sh experiments`). See `docs/D2.md`; results are generated into `docs/results/d2/`.

Details of the merge of the three parts: `INTEGRATION.md`. Network and forwarding internals: `docs/network_forwarding_member_b.md`. The consolidated architecture/design is in `docs/ARCHITECTURE.md`; team responsibilities are listed in `CONTRIBUTIONS.md`.

### Web interfaces

The project contains three interfaces with different purposes:

- `python3 ui/dashboard.py` — read-only live dashboard (default `http://localhost:8080`).
- `python3 ui/control.py` — lightweight control panel for building the D2 topology, changing policies, downloads, congestion and failure demonstrations (default `http://localhost:8081`). Start the controller and NetManager first, following the order in the script header.
- `sudo python3 ui/automation.py` — broader automation interface (default `http://localhost:8090`) that can start services, run scenarios and experiments.

Use one control interface at a time during the demo. These interfaces can issue network commands; keep them bound to a trusted local environment and do not expose them to the public internet. `ui/control.py` is included as part of this repository.

Screenshots/evidence guidance is in `docs/screenshots/README.md`. The optional video can be placed in `docs/demo/`.

## 7. Results (measured on our machine)
Environment: Ubuntu on WSL2, Mininet 2.3.0, Open vSwitch 3.7.1, Ryu 4.34. Topology: leaf-spine, 2 spines (s1, s2), 3 leaves (s3 to s5), 3 servers, 3 clients. Policy: round_robin.

| Test | Measured result |
|---|---|
| T1 logic test | 18 of 18 checks pass (`python3 tests/test_core.py`) |
| T2 network | built in 32.1 s, all hosts ready after 8.7 s; `pingall`: 30 sent, 0 lost |
| T3 downloads via the VIP | large.bin 5,242,880 bytes in 1.120 s (37.5 Mbps); medium.bin 1,048,576 bytes in 0.127 s; small.bin 102,400 bytes in 0.046 s; 0 retries, status OK |
| T4 server health | `ctl.py state`: srv1, srv2, srv3 all ACTIVE; 6 hosts located; 6 links (12 directed entries) known to the controller |
| T5 load distribution | c1 -> srv1, c2 -> srv2, c3 -> srv3 (round robin); each decision is logged by the controller (`conn ... -> srvN`) |
| T6 flow rules | client edge `s3`, table 0: two per-connection rules with `set_field` (client -> VIP rewritten to srv1, and srv1 -> VIP back), `idle_timeout=30`. Spine `s1`, table 0: only base rules and 0 packets on the VIP rule; table 1: 6 MAC routing entries |
| T8 link failure | after `link_down s3 s1`, large.bin from c1 was served by srv2 (on another leaf), 1.040 s, 40.3 Mbps, 0 retries, status OK. The routing entries via the failed link were removed and re-created after `link_up s3 s1` |

Screenshots for each row are in `docs/results/`.

Limitations: the simulated network is small (20 Mbps links), link utilization is measured from interface byte counters once per second or two, and the controller treats a link's utilization as the same in both directions. See `docs/D2.md` for the D2 evaluation and its limits.
