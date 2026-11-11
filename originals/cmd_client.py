import socket
import json

HOST = "127.0.0.1"
PORT = 9000

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_socket.connect((HOST, PORT))

message = {
    "type": "cmd",
    "command": "set_policy",
    "policy": "round_robin"
}
data = json.dumps(message) + "\n"

client_socket.send(data.encode())

client_socket.close()

print("Command sent")