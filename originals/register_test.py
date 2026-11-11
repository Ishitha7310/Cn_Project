import socket
import json

HOST = "127.0.0.1"
PORT = 9000

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_socket.connect((HOST, PORT))

message = {
    "type": "register",
    "server_id": "server1",
    "ip": "10.0.0.1",
    "switch": "s1"
}
client_socket.send(json.dumps(message).encode())

client_socket.close()

print("Server registered")