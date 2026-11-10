#!/usr/bin/env bash
# Usage:
#   ./run.sh controller        start the Ryu controller   (terminal 1)
#   ./run.sh net [--run "..."] start the network + prompt (terminal 2, asks for sudo)
#   ./run.sh test              run the automated end-to-end checks (starts/stops the controller itself)
#   ./run.sh experiments [opts] D2: compare policies under normal/congestion/failure (starts/stops the controller itself)
#                              opts: --quick (about 5 min check)  --reps N  --policies static round_robin least_conn smart  --dry-run
#   ./run.sh clean             kill leftovers (mininet, controller)
# Env: BRAIN=path/to/brain.py  (default: controller/brain.py = real Member C; mocks/mock_brain.py = fake one)
cd "$(dirname "$0")"
BRAIN="${BRAIN:-controller/brain.py}"
if [ -x .venv/bin/ryu-manager ]; then MGR=.venv/bin/ryu-manager
elif [ -x .venv/bin/osken-manager ]; then MGR=.venv/bin/osken-manager
else echo "run ./setup.sh first"; exit 1; fi

SUDO=sudo; [ "$(id -u)" = 0 ] && SUDO=""
start_ctrl() { "$MGR" --observe-links --ofp-tcp-listen-port 6653 forwarding_app.py "$BRAIN"; }

case "$1" in
  controller) start_ctrl ;;
  net)        shift; $SUDO env NM_DATAPATH="${NM_DATAPATH:-kernel}" /usr/bin/python3 netmanager.py serve "$@" ;;
  clean)      $SUDO mn -c >/dev/null 2>&1; pkill -f ryu-manager; pkill -f osken-manager; rm -f /tmp/netmanager.sock; echo cleaned ;;
  test)
    mkdir -p logs
    rc=0
    for mode in "" "--big"; do
      $SUDO mn -c >/dev/null 2>&1
      ( exec "$MGR" --observe-links --ofp-tcp-listen-port 6653 forwarding_app.py "$BRAIN" ) > logs/controller.log 2>&1 &
      CPID=$!
      for i in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/6653) 2>/dev/null && break; sleep 0.5; done
      echo; echo "################ smoke test $mode"
      $SUDO env NM_DATAPATH="${NM_DATAPATH:-kernel}" /usr/bin/python3 mocks/smoke_test.py $mode || rc=1
      kill $CPID 2>/dev/null; wait $CPID 2>/dev/null
      for i in $(seq 1 20); do (echo > /dev/tcp/127.0.0.1/6653) 2>/dev/null || break; sleep 0.5; done
    done
    echo; [ $rc = 0 ] && echo "ALL GOOD" || echo "SOME CHECKS FAILED - see logs/controller.log"
    exit $rc ;;
  experiments)
    shift
    mkdir -p logs
    $SUDO mn -c >/dev/null 2>&1
    ( exec "$MGR" --observe-links --ofp-tcp-listen-port 6653 forwarding_app.py "$BRAIN" ) > logs/controller.log 2>&1 &
    CPID=$!
    for i in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/6653) 2>/dev/null && break; sleep 0.5; done
    $SUDO env NM_DATAPATH="${NM_DATAPATH:-kernel}" /usr/bin/python3 experiments/run_experiments.py "$@"
    rc=$?
    kill $CPID 2>/dev/null; wait $CPID 2>/dev/null
    exit $rc ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
