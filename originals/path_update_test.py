from net_state import NetState
from path_engine import PathEngine
from monitor import Monitor

state = NetState()

state.add_link("s1", "s2", delay=5, utilization=20)
state.add_link("s2", "s3", delay=2, utilization=10)
state.add_link("s1", "s3", delay=10, utilization=5)

monitor = Monitor(state, alpha=0.5)

engine = PathEngine(state, k=0.1)

print("Initial path:")

path, cost = engine.calculate_path("s1", "s3")

print("Path:", path)
print("Cost:", cost)

print("\nBuilding initial next-hop table:")

engine.build_next_hop_table("s3")

engine.print_paths("s3")


print("\nIncreasing s1 -> s2 utilization:")

monitor.update_link_utilization(
    "s1",
    "s2",
    100
)

print("\nUpdating PathEngine:")

engine.update_paths()

engine.print_paths("s3")