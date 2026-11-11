class PathEngine:

    def __init__(self, net_state, k=0.1, threshold=20):
        self.net_state = net_state
        self.k = k
        self.threshold = threshold

        # Cached next-hop table
        self.next_hops = {}

        # Destination of cached table
        self.destination = None

        # Last utilization values used for computation
        self.last_utilization = {}

    def calculate_path(self, source, destination):
        path, cost = self.net_state.shortest_path(
            source, destination
        )

        return path, cost

    def build_next_hop_table(self, destination):
        self.destination = destination

        self.next_hops = self.net_state.next_hop_table(
            destination
        )

        # Save current utilization
        self.save_utilization()

        return self.next_hops

    def get_next_hop(self, source):
        return self.next_hops.get(source)

    def print_paths(self, destination=None):

        if destination is None:
            destination = self.destination

        print("\nNext-hop table for", destination)

        for switch, next_hop in self.next_hops.items():
            print(switch, "->", next_hop)

    def save_utilization(self):
        self.last_utilization = {}

        for link, data in self.net_state.links.items():
            self.last_utilization[link] = data["utilization"]

    def should_recompute(self):

        for link, data in self.net_state.links.items():

            current = data["utilization"]

            previous = self.last_utilization.get(
                link,
                current
            )

            difference = abs(current - previous)

            if difference >= self.threshold:
                return True

        return False

    def recompute(self):

        if self.destination is None:
            return {}

        self.next_hops = self.net_state.next_hop_table(
            self.destination
        )

        self.save_utilization()

        return self.next_hops

    def update_paths(self):

        if self.destination is None:
            return {}

        if self.should_recompute():

            print("\nUtilization changed significantly")
            print("Recomputing paths...")

            return self.recompute()

        else:

            print("\nUtilization change is small")
            print("Using cached paths")

            return self.next_hops