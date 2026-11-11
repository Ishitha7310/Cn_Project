from net_state import NetState
from path_engine import PathEngine


state = NetState()

state.add_link(
    "s1",
    "s2",
    delay=5,
    utilization=20
)

state.add_link(
    "s2",
    "s3",
    delay=2,
    utilization=10
)

state.add_link(
    "s1",
    "s3",
    delay=10,
    utilization=5
)


engine = PathEngine(
    state,
    k=0.1
)


print("Path from s1 to s3:")

path, cost = engine.calculate_path(
    "s1",
    "s3"
)

print("Path:", path)
print("Cost:", cost)


print("\nBuilding next-hop table:")

table = engine.build_next_hop_table(
    "s3"
)

engine.print_paths("s3")