class Policy:
    def __init__(self, net_state, path_engine=None):
        self.net_state = net_state
        self.path_engine = path_engine
        self.round_robin_index = 0
        self.current_policy = "static"
    def set_policy(self, policy_name):

        valid_policies = [
            "static",
            "round_robin",
            "least_conn",
            "smart"
        ]

        if policy_name not in valid_policies:
            return False

        self.current_policy = policy_name
        return True
    def select_server(self, client_ip):

        if self.current_policy == "static":
            return self.static(client_ip)

        elif self.current_policy == "round_robin":
            return self.round_robin(client_ip)

        elif self.current_policy == "least_conn":
            return self.least_conn(client_ip)

        elif self.current_policy == "smart":
            return self.smart(client_ip)

        return None
    def static(self, client_ip):
        for server_id, server in self.net_state.servers.items():
            if server["state"] == "ACTIVE":
                return server_id
        return None
    def round_robin(self, client_ip):
        active_servers = []
        for server_id, server in self.net_state.servers.items():
            if server["state"] == "ACTIVE":
                active_servers.append(server_id)
        if not active_servers:
            return None
        server = active_servers[self.round_robin_index % len(active_servers)]
        self.round_robin_index += 1
        return server
    def least_conn(self, client_ip):
        active_servers = []
        for server_id, server in self.net_state.servers.items():
            if server["state"] == "ACTIVE":
                active_servers.append((server_id, server["load"]))
        if not active_servers:
            return None
        active_servers.sort(key=lambda x: x[1])
        return active_servers[0][0]
    def smart_score(self, server_id, bottleneck_util, path_delay,
                w1=1.0, w2=1.0, w3=1.0):

        server = self.net_state.servers[server_id]

    # Normalize load
        max_load = max(
            server_data["load"]
            for server_data in self.net_state.servers.values()
        )

        if max_load == 0:
            normalized_load = 0
        else:
            normalized_load = server["load"] / max_load

    # Normalize utilization
        normalized_util = bottleneck_util / 100

    # Normalize path delay
        max_delay = 10

        normalized_delay = path_delay / max_delay

        score = (
            w1 * normalized_load
            + w2 * normalized_util
            + w3 * normalized_delay
        )

        return score
    def get_path_info(self, start, destination):

        if start == destination:
            return 0, 0

        path, cost = self.net_state.shortest_path(
            start, destination
    )
        if len(path) == 1:
            return 0, 0

        if len(path) == 0:
            return None, None
        total_delay = 0
        bottleneck_util = 0
        for i in range(len(path) - 1):
            switch1 = path[i]
            switch2 = path[i + 1]
            link = self.net_state.links.get((switch1, switch2))
            if link is None:
                return None, None
            total_delay += link["delay"]
            if link["utilization"] > bottleneck_util:
                bottleneck_util = link["utilization"]
        return total_delay, bottleneck_util
    def smart(self, client_ip):
        active_servers = []

        for server_id, server in self.net_state.servers.items():
            if server["state"] == "ACTIVE":
                active_servers.append(server_id)

        if not active_servers:
            return None

    # For now, the client is connected to s1.
        host = self.net_state.hosts.get(client_ip)

        if host is None:
            return None

        client_switch = host["switch"]

        best_server = None
        best_score = float("inf")

        for server_id in active_servers:

            server_switch = self.net_state.servers[server_id]["switch"]
 
            if server_switch is None:
                continue

            path_delay, bottleneck_util = self.get_path_info(
                client_switch,
                server_switch
            )

            if path_delay is None:
                continue

            score = self.smart_score(
                server_id,
                bottleneck_util,
                path_delay
            )

            print(
                server_id,
                "load:",
                 self.net_state.servers[server_id]["load"],
                 "path:",
                 client_switch,
                 "->",
                 server_switch,
                 "delay:",
                 path_delay,
                "utilization:",
                bottleneck_util,
                "score:",
                score
            )

            if score < best_score:
                best_score = score
                best_server = server_id

        return best_server
