#!/usr/bin/env python3

"""
Smart Content Delivery System - Member A

TCP content client.
"""

import argparse
import socket
import time
from pathlib import Path


BUFFER_SIZE = 64 * 1024


class ContentClient:
    """Client for downloading files from a content server."""

    def __init__(
        self,
        server_host: str,
        server_port: int = 8000,
        timeout: float = 5.0,
        retries: int = 3,
    ):
        self.server_host = server_host
        self.server_port = server_port
        self.timeout = timeout
        self.max_retries = retries

    def connect(self):
        sock = socket.create_connection(
            (self.server_host, self.server_port),
            timeout=self.timeout,
        )
        sock.settimeout(self.timeout)
        return sock

    def receive_header(self, sock: socket.socket) -> bytes:
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError(
                    "Connection closed before response header"
                )
            data += chunk
            if len(data) > 4096:
                raise ValueError("Response header is too large")
        return data

    def parse_header(self, header: bytes):
        line = header.split(b"\n", 1)[0].decode(
            "utf-8",
            errors="replace",
        ).strip()
        parts = line.split()
        if not parts:
            raise ValueError("Empty response")
        if parts[0] == "ERR":
            code = parts[1] if len(parts) > 1 else "UNKNOWN"
            message = " ".join(parts[2:]) if len(parts) > 2 else ""
            raise RuntimeError(f"Server error {code}: {message}")
        if parts[0] != "OK" or len(parts) != 4:
            raise ValueError(f"Invalid response header: {line}")
        total_size = int(parts[1])
        offset = int(parts[2])
        server_id = parts[3]
        return total_size, offset, server_id

    def download(self, remote_file: str, output_file: str) -> dict:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        offset = output_path.stat().st_size if output_path.exists() else 0
        start_time = time.monotonic()
        retries = 0
        server_id = "unknown"
        total_size = None

        while True:
            try:
                with self.connect() as sock:
                    request = f"GET {remote_file} {offset}\n".encode()
                    sock.sendall(request)

                    header_data = self.receive_header(sock)
                    header_line, separator, initial_body = header_data.partition(b"\n")
                    total_size, response_offset, server_id = self.parse_header(
                        header_line + b"\n"
                    )

                    if response_offset != offset:
                        raise RuntimeError("Server returned unexpected offset")

                    mode = "ab" if offset > 0 else "wb"
                    with output_path.open(mode) as file:
                        if initial_body:
                            file.write(initial_body)
                            offset += len(initial_body)

                        while offset < total_size:
                            chunk = sock.recv(BUFFER_SIZE)
                            if not chunk:
                                raise ConnectionError(
                                    "Connection closed during download"
                                )
                            file.write(chunk)
                            offset += len(chunk)

                if offset >= total_size:
                    break

            except (
                socket.timeout,
                ConnectionError,
                ConnectionResetError,
                BrokenPipeError,
                OSError,
            ) as exc:
                retries += 1
                if retries > self.max_retries:
                    raise RuntimeError(
                        f"Download failed after {self.max_retries} retries: {exc}"
                    ) from exc
                print(
                    f"[CLIENT] transfer interrupted at {offset} bytes; "
                    f"retry {retries}/{self.max_retries}"
                )
                time.sleep(min(2 ** retries, 5))

        elapsed = time.monotonic() - start_time
        throughput_mbps = (
            (total_size * 8 / 1_000_000) / elapsed
            if elapsed > 0 else 0
        )
        return {
            "file": remote_file,
            "server": server_id,
            "bytes": total_size,
            "response_time": elapsed,
            "throughput_mbps": throughput_mbps,
            "retries": retries,
            "status": "OK",
        }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Smart Content Delivery TCP client"
    )
    parser.add_argument("--server", default="10.0.255.100", help="Service address (VIP)")
    parser.add_argument("--port", type=int, default=8000, help="Server TCP port")
    parser.add_argument("--file", required=True, help="Remote file name")
    parser.add_argument("--output", default=None, help="Local output file")
    parser.add_argument("--timeout", type=float, default=5, help="Socket timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="Maximum number of retries")
    return parser.parse_args()


def main():
    args = parse_args()
    output_file = args.output or f"downloads/{args.file}"
    client = ContentClient(
        server_host=args.server,
        server_port=args.port,
        timeout=args.timeout,
        retries=args.retries,
    )
    print(f"[CLIENT] downloading {args.file} from {args.server}:{args.port}")
    result = client.download(args.file, output_file)
    print()
    print("[CLIENT] download complete")
    print(f"  File:       {result['file']}")
    print(f"  Server:     {result['server']}")
    print(f"  Bytes:      {result['bytes']:,}")
    print(f"  Time:       {result['response_time']:.3f} seconds")
    print(f"  Throughput: {result['throughput_mbps']:.2f} Mbps")
    print(f"  Retries:    {result['retries']}")
    print(f"  Status:     {result['status']}")


if __name__ == "__main__":
    main()
