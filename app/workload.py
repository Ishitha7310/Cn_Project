#!/usr/bin/env python3

"""
Smart Content Delivery System - Member A

Concurrent workload generator.
"""

import argparse
import csv
import threading
import time
from pathlib import Path

from client import ContentClient


DEFAULT_FILES = ["small.bin", "medium.bin", "large.bin"]


def run_client(
    client_id: int,
    server: str,
    port: int,
    files: list[str],
    requests: int,
    timeout: float,
    retries: int,
    output_dir: Path,
    results: list,
    lock: threading.Lock,
) -> None:
    """Run downloads for one simulated client."""
    client = ContentClient(
        server_host=server,
        server_port=port,
        timeout=timeout,
        retries=retries,
    )

    for request_id in range(requests):
        remote_file = files[request_id % len(files)]
        output_file = (
            output_dir / f"client_{client_id}" / f"request_{request_id}_{remote_file}"
        )
        start = time.time()

        try:
            result = client.download(remote_file, str(output_file))
            status = result["status"]
            server_id = result["server"]
            total_bytes = result["bytes"]
            response_time = result["response_time"]
            throughput = result["throughput_mbps"]
            retries_used = result["retries"]
        except Exception as exc:
            status = "ERROR"
            server_id = "unknown"
            total_bytes = 0
            response_time = time.time() - start
            throughput = 0
            retries_used = 0
            print(
                f"[WORKLOAD] client={client_id} request={request_id} failed: {exc}"
            )

        row = {
            "timestamp": time.time(),
            "client": client_id,
            "request": request_id,
            "file": remote_file,
            "served_by": server_id,
            "bytes": total_bytes,
            "response_time": response_time,
            "throughput_mbps": throughput,
            "retries": retries_used,
            "status": status,
        }
        with lock:
            results.append(row)

        print(
            f"[WORKLOAD] client={client_id} request={request_id} "
            f"file={remote_file} status={status}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Concurrent workload generator")
    parser.add_argument("--server", default="10.0.255.100", help="Service address (VIP)")
    parser.add_argument("--port", type=int, default=8000, help="Server TCP port")
    parser.add_argument("--clients", type=int, default=5, help="Number of concurrent clients")
    parser.add_argument("--requests", type=int, default=1, help="Number of downloads per client")
    parser.add_argument("--ramp", type=float, default=0.5, help="Delay between starting clients")
    parser.add_argument("--timeout", type=float, default=5, help="Client socket timeout")
    parser.add_argument("--retries", type=int, default=3, help="Maximum retries per download")
    parser.add_argument("--files", nargs="+", default=DEFAULT_FILES, help="Files to request")
    parser.add_argument("--output", default="downloads/workload", help="Download output directory")
    parser.add_argument("--csv", default="logs/workload.csv", help="CSV results file")
    args = parser.parse_args()

    if args.clients <= 0:
        raise ValueError("clients must be greater than 0")
    if args.requests <= 0:
        raise ValueError("requests must be greater than 0")
    if not args.files:
        raise ValueError("at least one file must be specified")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    results = []
    lock = threading.Lock()
    threads = []

    print()
    print("[WORKLOAD] Starting workload")
    print(f"  Server:       {args.server}:{args.port}")
    print(f"  Clients:      {args.clients}")
    print(f"  Requests:     {args.requests}")
    print(f"  Ramp delay:   {args.ramp}s")
    print(f"  Files:        {', '.join(args.files)}")
    print()

    for client_id in range(1, args.clients + 1):
        thread = threading.Thread(
            target=run_client,
            args=(
                client_id, args.server, args.port, args.files, args.requests,
                args.timeout, args.retries, output_dir, results, lock,
            ),
        )
        thread.start()
        threads.append(thread)
        if client_id < args.clients:
            time.sleep(args.ramp)

    for thread in threads:
        thread.join()

    results.sort(key=lambda row: (row["timestamp"], row["client"], row["request"]))
    fieldnames = [
        "timestamp", "client", "request", "file", "served_by", "bytes",
        "response_time", "throughput_mbps", "retries", "status",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    successful = sum(1 for row in results if row["status"] == "OK")
    print()
    print("[WORKLOAD] Complete")
    print(f"  Total requests: {len(results)}")
    print(f"  Successful:     {successful}")
    print(f"  Failed:         {len(results) - successful}")
    print(f"  CSV:            {csv_path}")


if __name__ == "__main__":
    main()
