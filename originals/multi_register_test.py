import socket
import json

HOST = "127.0.0.1"
PORT = 9000


def register_server(server_id, ip, switch):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))

    message = {
        "type": "register",
        "server_id": server_id,
        "ip": ip,
        "switch": switch
    }

    client_socket.send(json.dumps(message).encode())
    client_socket.close()

    print(server_id, "registered")


register_server("server1", "10.0.0.1", "s1")
register_server("server2", "10.0.0.2", "s2")
register_server("server3", "10.0.0.3", "s3")