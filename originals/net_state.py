import time
class NetState:
    def __init__(self):
        self.servers = {}
        self.hosts = {}
        self.links = {}
    def add_server(self, server_id, ip):
        self.servers[server_id] = {
            "ip": ip,
            "state": "JOINING",
            "load": 0,
            "last_heartbeat": 0,
            "switch": None
        }
    def activate_server(self, server_id):
        if server_id in self.servers:
            self.servers[server_id]["state"] = "ACTIVE"
    def drain_server(self, server_id):
        if server_id in self.servers:
            self.servers[server_id]["state"] = "DRAINING"
    def remove_server(self, server_id):
        if server_id in self.servers:
            self.servers[server_id]["state"] = "REMOVED"
    def heartbeat(self, server_id):
        if server_id in self.servers:
            self.servers[server_id]["last_heartbeat"] = time.time()

            if self.servers[server_id]["state"] == "DOWN":
                self.servers[server_id]["state"] = "ACTIVE"
    def check_server_health(self):
        current_time = time.time()
        for server_id, server in self.servers.items():
            if server["last_heartbeat"] == 0:
                continue

            if current_time - server["last_heartbeat"] > 3:
                server["state"] = "DOWN"
    def add_link(self, switch1, switch2, delay=1, utilization=0):

        self.links[(switch1, switch2)] = {
            "delay": delay,
            "utilization": utilization
        }
        self.links[(switch2, switch1)] = {
            "delay": delay,
            "utilization": utilization
        }
    def remove_link(self, switch1, switch2):
        self.links.pop((switch1, switch2), None)
        self.links.pop((switch2, switch1), None)
    
    def get_link_cost(self, switch1, switch2, k=0.1):
        link = self.links.get((switch1, switch2))
        if link is None:
            return None
        return link["delay"] + k * link["utilization"]
    def get_neighbors(self, switch):
        neighbors = []
        for link in self.links:
            if link[0] == switch:
                neighbors.append(link[1])
        return neighbors
    def shortest_path(self, start, destination):
        distances = {}
        previous = {}
        switches = set()
        for link in self.links:
            switches.add(link[0])
            switches.add(link[1])
        unvisited=set(switches)
        for switch in switches:
            distances[switch] = float("inf")
            previous[switch] = None
        distances[start] = 0
        while unvisited:
            current = None
            current_distance = float("inf")
            for switch in switches:
                if switch in unvisited and distances[switch] < current_distance:
                    current = switch
                    current_distance = distances[switch]
            if current is None:
                break
            unvisited.remove(current)
            for neighbor in self.get_neighbors(current):
                cost = self.get_link_cost(current, neighbor)
                new_distance = distances[current] + cost
                if new_distance < distances[neighbor]:
                    distances[neighbor] = new_distance
                    previous[neighbor] = current
        path = []
        current = destination
        while current is not None:
            path.append(current)
            current = previous[current]
        path.reverse()
        return path, distances[destination]
    def next_hop_table(self, destination):
        table = {}
        switches = set()
        for link in self.links:
            switches.add(link[0])
            switches.add(link[1])
        for switch in switches:
            if switch == destination:
                continue
            path, cost = self.shortest_path(switch, destination)
            if len(path) >= 2:
                next_hop = path[1]
                table[switch] = next_hop
        return table
    def get_state(self):
        links = []

        for (switch1, switch2), data in self.links.items():
            links.append({
                "switch1": switch1,
                "switch2": switch2,
                "delay": data["delay"],
                "utilization": data["utilization"]
            })

        return {
            "servers": self.servers,
            "hosts": self.hosts,
            "links": links
        }
    def set_server_switch(self, server_id, switch):
        if server_id in self.servers:
            self.servers[server_id]["switch"] = switch
    def add_host(self, client_ip, switch):
        self.hosts[client_ip] = {
            "switch": switch
    }
if __name__ == "__main__":
    state = NetState()
    state.add_server("server1", "10.0.0.1")
    print("After adding server:")
    print(state.servers)
    state.activate_server("server1")
    print("\nAfter activation:")
    print(state.servers)
    state.heartbeat("server1")
    print("\nAfter heartbeat:")
    print(state.servers)
    print("\nWaiting 4 seconds...")
    time.sleep(4)
    state.check_server_health()
    print("\nAfter health check:")
    print(state.servers)
    print("\nSending heartbeat again...")
    state.heartbeat("server1")
    print("\nAfter recovery heartbeat:")
    print(state.servers)
    state.add_link("s1", "s2", delay=5, utilization=20)
    state.add_link("s2", "s3", delay=2, utilization=10)
    state.add_link("s1", "s3", delay=10, utilization=5)
    print("\nNetwork links:")
    print(state.links)
    cost = state.get_link_cost("s1", "s2")
    print("\nLink cost:")
    print(cost)
    print("\nNeighbors of s1:")
    print(state.get_neighbors("s1"))
    print("\nNeighbors of s2:")
    print(state.get_neighbors("s2"))
    path, cost = state.shortest_path("s1", "s3")
    print("\nShortest path from s1 to s3:")
    print(path)
    print("Total cost:")
    print(cost)
    next_hops = state.next_hop_table("s3")
    print("\nNext-hop table to s3:")
    print(next_hops)