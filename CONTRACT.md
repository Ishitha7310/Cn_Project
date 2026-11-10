# CONTRACT: the interfaces between Member A, B and C

This is the only thing the three parts must agree on. Member B (network + forwarding) is already built to this.
Shared constants (VIP, ports, subnets) live in `config.py` and are changed there, in one place.

All three parts run like this:

```
ryu-manager --observe-links forwarding_app.py  <C's brain file>        (B and C are two Ryu apps in ONE process)
sudo python3 netmanager.py serve                                       (B: Mininet; A's servers/clients run inside it)
```

## 1. Member C (controller brain)  <->  Member B

C is a Ryu app whose **class is named `Brain`** (see `BRAIN_APP` in `config.py`). B's app is named `Forwarding`.
They find each other with `app_manager.lookup_service_brick("Forwarding")` / `("Brain")`.

### B calls these on C
| Method | Meaning |
|---|---|
| `select_server(client_ip) -> {"name": str, "ip": str} or None` | Pick the server for a NEW TCP connection. `None` = no server available (B answers the client with a TCP reset). Must only return servers whose IP B has located (`Forwarding.hosts`). |
| `next_hop_table() -> {(dpid:int, mac:str): out_port:int}` | The routing table. For **every host B has reported**, one entry per switch that can reach it: at the host's own switch the port to the host, at every other switch the port toward it. Leave out unreachable pairs. MACs lower-case. Called after every `on_routes_changed()`; B diffs it against what is installed. |
| `on_host_learned(mac, ip, dpid, port)` | A host appeared (or moved / got a new MAC: a later call for the same IP replaces the old one). |
| `on_host_removed(mac, ip)` *(optional)* | A host's port was deleted. |
| `on_connection_assigned(client_ip, client_port, server_name)` | B gave this new connection to this server (for C's load counters). |

### C calls these on B (`Forwarding`)
| Method | Meaning |
|---|---|
| `on_routes_changed()` | Topology/costs changed: B re-reads `next_hop_table()` (debounced 150 ms). Call it on every link add/delete, switch leave, or cost change. |
| `on_server_down(name)` | Server is DOWN (heartbeat timeout) or finished DRAINING: B deletes its connection rules so clients are reset and reconnect. |
| `hosts` (attribute) | `{ip: Host(mac, ip, dpid, port)}`: where B located each host. |
| `snapshot()` | JSON-able dict of B's state (hosts, connection count, counters). |

Topology discovery is C's job: use Ryu's `ryu.topology.event` (`EventLinkAdd`, `EventLinkDelete`, ...).
`mocks/mock_brain.py` is a ~150-line reference implementation of all of the above.

### C's control server (TCP, `0.0.0.0:9000`, newline-delimited JSON)
Servers reach it over the management network (`192.168.100.254:9000`).
```
{"type":"register","name":"srv4","ip":"10.0.0.14","capacity_mbps":20,"max_conns":50}
{"type":"heartbeat","name":"srv4","active_conns":3,"avg_service_ms":120,"tx_mbps":4.2}
{"type":"deregister","name":"srv4","graceful":true}
{"type":"cmd","action":"set_policy|set_weights|get_status|drain", ...}
```

## 2. Member A (application)  <->  Member B

* Clients connect to **`10.0.255.100:8000`** (`VIP`, `SERVICE_PORT`). Nothing else about the network is visible to them.
* App protocol: `GET <file> <offset>\n` -> `OK <size> <offset> <server_name>\n` + bytes, or `ERR <code> <msg>\n`.
* NetManager starts each server with `SERVER_CMD` from `config.py`; it must accept `--name --ip --ctrl host:port --port`
  (or edit that one line). A server listens on `0.0.0.0:8000`, and sends register/heartbeat to the control server above.
* Servers and clients run **inside Mininet hosts**; A drives the network in one of two ways:

**a) Python import**
```python
from netmanager import NetManager
nm = NetManager(); nm.start(serve=False)
nm.gen_topo("leaf-spine", spines=2, leaves=3, servers=3, clients=6)
nm.exec("c1", "python3 client.py --file large", timeout=60)["out"]
nm.link_down("s3", "s1"); nm.add_server(on="s4"); nm.kill_server("srv2"); nm.stop()
```
**b) Unix socket** `/tmp/netmanager.sock`, one JSON per line: `{"cmd":"add_server","args":[],"opts":{"on":"s3"}}`
-> `{"ok":true,"result":{...}}`. Same from a shell: `python3 netmanager.py ctl add_server --on s3`.

### NetManager commands (same names everywhere)
`gen_topo [leaf-spine|ring|random]`, `add_switch`, `add_link A B --bw --delay --loss`, `del_link A B`,
`set_link A B --bw --delay --loss`, `link_down A B`, `link_up A B`, `add_server [name] --on sX`, `add_clients N --on sX`,
`add_host`, `remove_host`, `start_server`, `kill_server` (crash), `stop_server` (hang), `cont_server`,
`iperf_noise A B --secs --mbps`, `exec HOST "cmd" [--bg --log file]`, `pingall`, `wait_ready`, `flows sX [--table N]`, `info`.

Naming: servers `srv1..`, clients `c1..`, switches `s1..` (dpid = number). Host IPs `10.0.x.y`, deterministic.
