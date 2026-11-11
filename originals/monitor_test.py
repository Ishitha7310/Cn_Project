from net_state import NetState
from monitor import Monitor


state = NetState()

state.add_link(
    "s1",
    "s2",
    delay=5,
    utilization=0
)

monitor = Monitor(state, alpha=0.5)


print("\nUpdating s1 -> s2:")

monitor.update_link_utilization(
    "s1",
    "s2",
    20
)

print("\nBoth directions:")

print(
    "s1 -> s2:",
    state.links[("s1", "s2")]
)

print(
    "s2 -> s1:",
    state.links[("s2", "s1")]
)


print("\nSecond measurement:")

monitor.update_link_utilization(
    "s1",
    "s2",
    40
)

print("\nBoth directions after EWMA:")

print(
    "s1 -> s2:",
    state.links[("s1", "s2")]
)

print(
    "s2 -> s1:",
    state.links[("s2", "s1")]
)