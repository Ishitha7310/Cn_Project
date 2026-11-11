import socket
import json

HOST = "127.0.0.1"
PORT = 9000

client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

client_socket.connect((HOST, PORT))

message = {
    "type": "state"
}

client_socket.send(json.dumps(message).encode())

data = client_socket.recv(4096)

current_state = json.loads(data.decode())

print("\nController State:")
print(json.dumps(current_state, indent=2))

client_socket.close()