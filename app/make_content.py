#!/usr/bin/env python3

"""
Generate test content files for the Smart Content Delivery System.

Files generated:
- small.bin  -> 100 KB
- medium.bin -> 1 MB
- large.bin  -> 5 MB
"""

from pathlib import Path
import argparse


SIZES = {
    "small.bin": 100 * 1024,
    "medium.bin": 1 * 1024 * 1024,
    "large.bin": 5 * 1024 * 1024,
}


def create_file(path: Path, size: int) -> None:
    """Create a file containing deterministic test data."""
    block = bytes((i % 256 for i in range(1024)))

    with path.open("wb") as file:
        remaining = size

        while remaining > 0:
            chunk_size = min(len(block), remaining)
            file.write(block[:chunk_size])
            remaining -= chunk_size


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate content files for the SDN content delivery project."
    )
    parser.add_argument(
        "--output",
        default="data/content",
        help="Directory where content files will be created.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    for filename, size in SIZES.items():
        path = output_dir / filename
        create_file(path, size)
        print(f"Created {path} ({size:,} bytes)")


if __name__ == "__main__":
    main()
