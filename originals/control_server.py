
import socket
import json
import threading
import time

from net_state import NetState
from policy import Policy
from path_engine import PathEngine


HOST = "127.0.0.1"
PORT = 9000


# -----------------------------
# Create controller components
# -----------------------------

state = NetState()
path_engine = PathEngine(state)
policy = Policy(state, path_engine)

def get_sdn_forwarding_table():
    try:
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(("127.0.0.1", 9100))

        request = {
            "type": "get_table"
        }

        client.send(json.dumps(request).encode())

        data = client.recv(4096)
        client.close()

        if not data:
            return {}

        raw_table = json.loads(data.decode())

        table = {}

        for key, port in raw_table.items():
            dpid, mac = key.split(",", 1)
            table[(int(dpid), mac)] = port

        return table

    except Exception as e:
        print("SDN table error:", e)
        return {}



# -----------------------------
# Health Check Thread
# -----------------------------

def health_check():
    while True:
        state.check_server_health()

        print("Health check:", state.servers)

        time.sleep(1)


health_thread = threading.Thread(
    target=health_check,
    daemon=True
)

health_thread.start()


# -----------------------------
# Create TCP Server
# -----------------------------

server_socket = socket.socket(
    socket.AF_INET,
    socket.SOCK_STREAM
)

server_socket.bind((HOST, PORT))
server_socket.listen(5)


print("Control Server started")
print("Listening on port", PORT)


# -----------------------------
# Handle Client Requests
# -----------------------------

while True:

    client_socket, client_address = server_socket.accept()

    print("\nClient connected:", client_address)

    data = client_socket.recv(1024)

    if data:

        message = data.decode().strip()

        print("Received:", message)

        request = json.loads(message)


        # ==========================================
        # GET CONTROLLER STATE
        # ==========================================

        if request["type"] == "state":
            current_state = state.get_state()
            current_state["policy"] = policy.current_policy
            current_state["next_hops"] = path_engine.next_hops
            current_state["sdn_forwarding_table"] = {str(key): port for key, port in get_sdn_forwarding_table().items()}
            response = json.dumps(current_state)
            client_socket.send(response.encode())
            print("Current controller state:")
            print(json.dumps(current_state, indent=2))

        elif request["type"] == "add_link":

            switch1 = request["switch1"]
            switch2 = request["switch2"]

            delay = request.get(
                "delay",
                1
            )

            utilization = request.get(
                "utilization",
                0
            )

            state.add_link(
                switch1,
                switch2,
                delay,
                utilization
            )
        
            print(
                "Link added:",
                switch1,
                "<->",
                switch2
            )


        # ==========================================
        # REGISTER SERVER
        # ==========================================

        elif request["type"] == "register":

            server_id = request["server_id"]

            ip = request["ip"]

            switch = request.get("switch")


            state.add_server(
                server_id,
                ip
            )


            if switch is not None:

                state.set_server_switch(
                    server_id,
                    switch
                )


            state.activate_server(
                server_id
            )


            print(
                "Server registered and activated:"
            )

            print(
                state.servers
            )


        # ==========================================
        # ADD CLIENT HOST
        # ==========================================

        elif request["type"] == "add_host":

            client_ip = request["client_ip"]

            switch = request["switch"]


            state.add_host(
                client_ip,
                switch
            )


            print("Host added:")

            print(
                state.hosts
            )


        # ==========================================
        # UPDATE SERVER LOAD
        # ==========================================

        elif request["type"] == "set_load":

            server_id = request["server_id"]

            load = request["load"]


            if server_id in state.servers:

                state.servers[server_id]["load"] = load


                print(
                    "Load updated:",
                    server_id,
                    "=",
                    load
                )


        # ==========================================
        # SELECT SERVER
        # ==========================================

        elif request["type"] == "select_server":

            client_ip = request["client_ip"]


            selected_server = policy.select_server(
                client_ip
            )


            response = {

                "client_ip": client_ip,

                "policy": policy.current_policy,

                "selected_server": selected_server

            }


            client_socket.send(
                json.dumps(response).encode()
            )


            print(
                "Server selected for",
                client_ip,
                ":",
                selected_server
            )


        # ==========================================
        # SERVER HEARTBEAT
        # ==========================================

        elif request["type"] == "heartbeat":

            server_id = request["server_id"]


            state.heartbeat(
                server_id
            )


            print(
                "Heartbeat received from:",
                server_id
            )

            print(
                state.servers
            )


        # ==========================================
        # DEREGISTER SERVER
        # ==========================================

        elif request["type"] == "deregister":

            server_id = request["server_id"]


            state.remove_server(
                server_id
            )


            print(
                "Server deregistered:",
                server_id
            )

            print(
                state.servers
            )


        # ==========================================
        # CONTROL COMMANDS
        # ==========================================

        elif request["type"] == "cmd":

            command = request["command"]
            if command == "build_paths":
                destination = request["destination"]

                next_hops = path_engine.build_next_hop_table(destination)

                print("Next-hop table:")
                print(next_hops)

                response = {
                    "destination": destination,
                    "next_hops": next_hops
                }

                client_socket.send(json.dumps(response).encode())


            # --------------------------------------
            # ACTIVATE SERVER
            # --------------------------------------

            if command == "activate":

                server_id = request["server_id"]


                state.activate_server(
                    server_id
                )


                print(
                    "Server activated:",
                    server_id
                )

                print(
                    state.servers
                )


            # --------------------------------------
            # DRAIN SERVER
            # --------------------------------------

            elif command == "drain":

                server_id = request["server_id"]


                state.drain_server(
                    server_id
                )


                print(
                    "Server draining:",
                    server_id
                )

                print(
                    state.servers
                )


            # --------------------------------------
            # REMOVE SERVER
            # --------------------------------------

            elif command == "remove":

                server_id = request["server_id"]


                state.remove_server(
                    server_id
                )


                print(
                    "Server removed:",
                    server_id
                )

                print(
                    state.servers
                )


            # --------------------------------------
            # CHANGE POLICY
            # --------------------------------------

            elif command == "set_policy":

                policy_name = request["policy"]


                if policy.set_policy(
                    policy_name
                ):

                    print(
                        "Policy changed to:",
                        policy.current_policy
                    )

                else:

                    print(
                        "Invalid policy:",
                        policy_name
                    )


    client_socket.close()
