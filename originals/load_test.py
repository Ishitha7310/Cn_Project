import socket
import json

HOST = "127.0.0.1"
PORT = 9000


def set_load(server_id, load):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))

    message = {
        "type": "set_load",
        "server_id": server_id,
        "load": load
    }

    client_socket.send(json.dumps(message).encode())
    client_socket.close()

    print(server_id, "load =", load)


set_load("server1", 8)
set_load("server2", 2)
set_load("server3", 5)