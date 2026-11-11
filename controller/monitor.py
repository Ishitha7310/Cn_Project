class Monitor:

    def __init__(self, net_state, alpha=0.5):
        self.net_state = net_state
        self.alpha = alpha
        self.ewma = {}

    def update_link_utilization(self, switch1, switch2, new_utilization):

        link = (switch1, switch2)

        if link not in self.ewma:
            self.ewma[link] = new_utilization
        else:
            old_value = self.ewma[link]

            self.ewma[link] = (
                self.alpha * new_utilization
                + (1 - self.alpha) * old_value
            )

        # Update forward direction
        if link in self.net_state.links:
            self.net_state.links[link]["utilization"] = self.ewma[link]

        # Update reverse direction
        reverse_link = (switch2, switch1)

        if reverse_link in self.net_state.links:
            self.net_state.links[reverse_link]["utilization"] = self.ewma[link]

        print(
            switch1,
            "->",
            switch2,
            "utilization:",
            self.ewma[link]
        )

    def get_utilization(self, switch1, switch2):

        return self.ewma.get(
            (switch1, switch2),
            0
        )