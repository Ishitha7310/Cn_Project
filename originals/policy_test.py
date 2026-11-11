from net_state import NetState
from policy import Policy
from path_engine import PathEngine

state = NetState()

# Add servers
state.add_server("server1", "10.0.0.1")
state.add_server("server2", "10.0.0.2")
state.add_server("server3", "10.0.0.3")

# Activate servers
state.activate_server("server1")
state.activate_server("server2")
state.activate_server("server3")

# Connect each server to a switch
state.set_server_switch("server1", "s1")
state.set_server_switch("server2", "s2")
state.set_server_switch("server3", "s3")

# Set server loads
state.servers["server1"]["load"] = 5
state.servers["server2"]["load"] = 2
state.servers["server3"]["load"] = 8

# Add network links
state.add_link("s1", "s2", delay=5, utilization=20)
state.add_link("s2", "s3", delay=2, utilization=10)
state.add_link("s1", "s3", delay=10, utilization=5)

# Create PathEngine and Policy
path_engine = PathEngine(state)
policy = Policy(state, path_engine)

# Use smart policy
policy.set_policy("smart")

print("\nSmart policy test:")

selected = policy.select_server("client1")

print("\nSelected server:", selected)