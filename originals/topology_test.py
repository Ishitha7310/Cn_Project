import socket
import json

HOST = "127.0.0.1"
PORT = 9000


def add_link(switch1, switch2, delay, utilization):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))

    message = {
        "type": "add_link",
        "switch1": switch1,
        "switch2": switch2,
        "delay": delay,
        "utilization": utilization
    }

    client_socket.send(json.dumps(message).encode())
    client_socket.close()

    print("Added:", switch1, "<->", switch2)


add_link("s1", "s2", 5, 20)
add_link("s2", "s3", 2, 10)
add_link("s1", "s3", 10, 5)