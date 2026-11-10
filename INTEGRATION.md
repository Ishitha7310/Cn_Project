# Integrated project: A (application) + B (network/forwarding) + C (controller brain)

```
sdn-cdn/
  app/            Member A  server.py client.py workload.py make_content.py plot_results.py
  controller/     Member C  brain.py (Ryu app "Brain") + core.py + net_state/path_engine/policy/monitor + ctl.py
  forwarding_app.py, netmanager.py, config.py, CONTRACT.md, run.sh, setup.sh   Member B
  tests/test_core.py   integration test that needs no Mininet
  originals/      untouched copies of A's and C's submitted files
```

## Run (Ubuntu with Mininet; first time `./setup.sh`)
```bash
python3 app/make_content.py                 # creates data/content/{small,medium,large}.bin
./run.sh controller                         # terminal 1: Forwarding (B) + Brain (C) in one Ryu process
./run.sh net --run "gen_topo leaf-spine --spines 2 --leaves 3 --servers 3 --clients 3"   # terminal 2 (nm> prompt)
nm> c1 python3 app/client.py --file large.bin          # goes to the VIP 10.0.255.100:8000
python3 controller/ctl.py state | policy least_conn | drain srv2
python3 tests/test_core.py                  # sanity check, no root needed
```

## What was changed to make the three parts fit
| Part | Change | Why |
|---|---|---|
| C | `of_controller.py` (learning switch) and `control_server.py` (stand-alone server on 127.0.0.1:9000) are **replaced** by `brain.py` (Ryu app `Brain`) + `core.py`; the originals are in `originals/member_c/` | B's `Forwarding` app already does switching/ARP/VIP; the contract requires one app named `Brain` and a control port reachable from the servers (0.0.0.0:9000) |
| C | Control messages accept A's format (`name`, `action`) and C's old one (`server_id`, `command`) | A's server sends `name` |
| C | Routes use C's Dijkstra (delay + k*utilization) but are converted to `{(dpid, mac): port}` using LLDP port numbers | contract `next_hop_table()` |
| C | `policy.py`: servers B has not yet located are never selected (`eligible` hook); default policy is `round_robin` (was `static`, which sends everything to one server; change with `BRAIN_POLICY=...` or `ctl.py policy`) | contract |
| C | `net_state.py`: REMOVED servers skipped by the health check | avoids REMOVED -> DOWN flapping |
| A | `server.py`: heartbeat every **1 s** (was 5 s; C marks DOWN after 3 s, so servers would flap DOWN) | |
| A | `server.py`: new `--ip` (IP registered with the controller; before it registered its bind address 0.0.0.0), `--name` alias, `--ctrl host:port`, content dir relative to the repo; heartbeat now carries `ip` so a restarted controller re-learns servers | NetManager launch command |
| A | `client.py`, `workload.py`: default `--server` is the VIP `10.0.255.100` | clients must only know the VIP |
| B | `config.py` `SERVER_CMD` points to `app/server.py`; `run.sh` default brain is `controller/brain.py` | |

## Status: what was and was not verified
* `tests/test_core.py` passes (18 checks): A's real server process registers/heartbeats, C's logic selects servers, B's `next_hop_table` contract is met and re-routes after a link failure, DOWN/drain call `on_server_down`.
* `brain.py` imports and subclasses the Ryu app base class (checked with os-ken).
* **Verified:** the full Mininet + Ryu end-to-end passes on an Ubuntu VM with `./run.sh test` (9/9 checks, then 3/3 in the `--big` run). Re-run it after any change.
