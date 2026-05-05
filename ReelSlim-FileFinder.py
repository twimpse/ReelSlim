#!/usr/bin/python3
import os
from pathlib import Path

# Size threshold: 2.5 GB in bytes
SIZE_THRESHOLD = 2.5 * 1024**3

def find_large_files(root_dir):
    large_files = []
    for dirpath, _, filenames in os.walk(root_dir):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                size = path.stat().st_size
                if size > SIZE_THRESHOLD:
                    large_files.append((str(path), size))
            except OSError as e:
                print(f"Skipping {path}: {e}")
    return large_files

if __name__ == "__main__":
    root = input("Enter directory to scan: ").strip()
    results = find_large_files(root)

    if results:
        print("\nFiles larger than 2.5 GB:\n")
        for path, size in results:
            print(f"{path} — {size / (1024**3):.2f} GB")
    else:
        print("No files larger than 2.5 GB found.")

