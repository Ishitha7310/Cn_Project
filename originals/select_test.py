import socket
import json

HOST = "127.0.0.1"
PORT = 9000

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client_socket.connect((HOST, PORT))

message = {
    "type": "select_server",
    "client_ip": "10.0.0.10"
}

client_socket.send(json.dumps(message).encode())

data = client_socket.recv(4096)

print("Response:")
print(json.dumps(json.loads(data.decode()), indent=2))

client_socket.close()