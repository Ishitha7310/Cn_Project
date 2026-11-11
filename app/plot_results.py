#!/usr/bin/env python3

"""
Smart Content Delivery System - Member A

Generate graphs from workload CSV results.
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def load_results(csv_file: str) -> pd.DataFrame:
    """Load and validate workload results."""
    path = Path(csv_file)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    data = pd.read_csv(path)
    required_columns = [
        "timestamp", "client", "request", "file", "served_by", "bytes",
        "response_time", "throughput_mbps", "retries", "status",
    ]
    missing = [column for column in required_columns if column not in data.columns]
    if missing:
        raise ValueError("Missing CSV columns: " + ", ".join(missing))
    return data


def save_response_time_plot(data: pd.DataFrame, output_dir: Path) -> None:
    plt.figure(figsize=(9, 5))
    plt.bar(data["client"].astype(str), data["response_time"])
    plt.xlabel("Client")
    plt.ylabel("Response Time (seconds)")
    plt.title("Client Response Time")
    plt.tight_layout()
    output = output_dir / "response_time.png"
    plt.savefig(output, dpi=150)
    plt.close()
    print(f"[PLOT] Created {output}")


def save_throughput_plot(data: pd.DataFrame, output_dir: Path) -> None:
    plt.figure(figsize=(9, 5))
    plt.bar(data["client"].astype(str), data["throughput_mbps"])
    plt.xlabel("Client")
    plt.ylabel("Throughput (Mbps)")
    plt.title("Client Throughput")
    plt.tight_layout()
    output = output_dir / "throughput.png"
    plt.savefig(output, dpi=150)
    plt.close()
    print(f"[PLOT] Created {output}")


def save_status_plot(data: pd.DataFrame, output_dir: Path) -> None:
    counts = data["status"].value_counts()
    plt.figure(figsize=(7, 5))
    plt.bar(counts.index, counts.values)
    plt.xlabel("Status")
    plt.ylabel("Number of Requests")
    plt.title("Request Success / Failure")
    plt.tight_layout()
    output = output_dir / "status.png"
    plt.savefig(output, dpi=150)
    plt.close()
    print(f"[PLOT] Created {output}")


def save_server_share_plot(data: pd.DataFrame, output_dir: Path) -> None:
    counts = data["served_by"].value_counts()
    plt.figure(figsize=(8, 5))
    plt.bar(counts.index, counts.values)
    plt.xlabel("Server")
    plt.ylabel("Number of Requests")
    plt.title("Requests Served by Server")
    plt.tight_layout()
    output = output_dir / "server_share.png"
    plt.savefig(output, dpi=150)
    plt.close()
    print(f"[PLOT] Created {output}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate performance graphs from workload results."
    )
    parser.add_argument("--input", default="logs/workload.csv", help="Input workload CSV")
    parser.add_argument("--output", default="results", help="Directory for generated graphs")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    data = load_results(args.input)

    print()
    print("[PLOT] Loaded results")
    print(f"  Rows:       {len(data)}")
    print(f"  Successful: {(data['status'] == 'OK').sum()}")
    print(f"  Failed:     {(data['status'] != 'OK').sum()}")
    print()

    save_response_time_plot(data, output_dir)
    save_throughput_plot(data, output_dir)
    save_status_plot(data, output_dir)
    save_server_share_plot(data, output_dir)
    print()
    print("[PLOT] All graphs generated successfully.")


if __name__ == "__main__":
    main()
