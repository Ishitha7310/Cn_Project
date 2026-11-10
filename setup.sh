#!/usr/bin/env bash
# One-time setup (Ubuntu / Pop!_OS). Run:  ./setup.sh
# - installs Mininet + Open vSwitch + tools with apt
# - builds a Python 3.9 virtualenv (.venv) with Ryu, because Ryu does not work on newer system Pythons
set -e
cd "$(dirname "$0")"

echo "== apt packages"
sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch iperf ethtool net-tools iputils-ping iproute2 curl

echo "== Open vSwitch service"
sudo systemctl enable --now openvswitch-switch 2>/dev/null || sudo service openvswitch-switch start
sudo ovs-vsctl show >/dev/null && echo "open vswitch running"
if ! sudo modprobe openvswitch 2>/dev/null; then
  echo "WARNING: kernel module 'openvswitch' not available. Use the slower userspace mode:  export NM_DATAPATH=user"
fi

echo "== Python 3.9 virtualenv with Ryu (via uv)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.9
uv venv --python 3.9 --seed .venv
.venv/bin/pip install -q "setuptools==65.5.0" wheel pbr
.venv/bin/pip install -q --no-build-isolation "eventlet==0.30.2" ryu networkx

echo "== checks"
.venv/bin/ryu-manager --version
/usr/bin/python3 -c "import mininet; print('mininet python package: ok')"
echo
echo "Setup done.  Next:  ./run.sh test"
