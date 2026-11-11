import socket
import json
import sys

HOST = "127.0.0.1"
PORT = 9000


def send_command(message):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((HOST, PORT))

    client_socket.send(json.dumps(message).encode())

    data = client_socket.recv(4096)

    client_socket.close()

    if data:
        try:
            response = json.loads(data.decode())
            print(json.dumps(response, indent=2))
        except json.JSONDecodeError:
            print(data.decode())


if len(sys.argv) < 2:
    print("Usage:")
    print("python ctl.py state")
    print("python ctl.py paths <destination>")
    print("python ctl.py policy <policy>")
    print("python ctl.py activate <server_id>")
    print("python ctl.py drain <server_id>")
    print("python ctl.py remove <server_id>")
    sys.exit()


command = sys.argv[1]


if command == "state":

    message = {
        "type": "state"
    }
elif command == "paths":
    if len(sys.argv) < 3:
        print("Please specify destination")
        sys.exit()

    message = {
        "type": "cmd",
        "command": "build_paths",
        "destination": sys.argv[2]
    }

elif command == "policy":

    if len(sys.argv) < 3:
        print("Please specify a policy")
        sys.exit()

    message = {
        "type": "cmd",
        "command": "set_policy",
        "policy": sys.argv[2]
    }

elif command == "activate":

    message = {
        "type": "cmd",
        "command": "activate",
        "server_id": sys.argv[2]
    }

elif command == "drain":

    message = {
        "type": "cmd",
        "command": "drain",
        "server_id": sys.argv[2]
    }

elif command == "remove":

    message = {
        "type": "cmd",
        "command": "remove",
        "server_id": sys.argv[2]
    }

else:

    print("Unknown command")
    sys.exit()


send_command(message)