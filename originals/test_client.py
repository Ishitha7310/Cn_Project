import socket
import json

HOST = "127.0.0.1"
PORT = 9000

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_socket.connect((HOST, PORT))

message = {
    "type": "heartbeat",
    "server_id": "server1"
}

client_socket.send(json.dumps(message).encode())

client_socket.close()

print("Heartbeat sent")