#!/usr/bin/python3
import argparse
import os
from pathlib import Path
from collections import defaultdict

DEFAULT_NON_MKV = {
    "avi", "mp4", "m4v", "mov", "webm", "wmv", "ts", "mpg", "mpeg", "3gp", "flv"
}

def parse_args():
    p = argparse.ArgumentParser(
        description="Delete non-MKV video files when an MKV with the same basename exists."
    )
    p.add_argument("-d", "--directory", default=".", help="Directory to scan (default: .)")
    p.add_argument("-r", "--recursive", action="store_true", help="Recurse into subdirectories")
    p.add_argument("-n", "--dry-run", action="store_true", help="Show what would be deleted")
    p.add_argument(
        "--exts",
        help="Comma-separated non-MKV extensions to consider (override defaults). "
             f"Defaults: {','.join(sorted(DEFAULT_NON_MKV))}"
    )
    return p.parse_args()

def main():
    args = parse_args()
    root = Path(args.directory)
    if not root.is_dir():
        raise SystemExit(f"Not a directory: {root}")

    non_mkv = (
        {e.strip().lower().lstrip(".") for e in args.exts.split(",")}
        if args.exts else set(DEFAULT_NON_MKV)
    )

    # First pass: collect basenames that have an .mkv in each directory (case-insensitive).
    mkv_keys_by_dir = defaultdict(set)
    it = list(root.rglob("*") if args.recursive else root.iterdir())
    for p in it:
        if p.is_file() and p.suffix.lower() == ".mkv":
            dir_key = str(p.parent.resolve())
            base_key = p.stem.lower()
            mkv_keys_by_dir[dir_key].add(base_key)

    # Second pass: delete non-MKV files whose basename has an MKV neighbor.
    deleted = 0
    kept = 0
    for p in it:
        if not p.is_file():
            continue
        ext = p.suffix.lower().lstrip(".")
        if ext == "mkv" or ext not in non_mkv:
            kept += 1
            continue
        dir_key = str(p.parent.resolve())
        base_key = p.stem.lower()
        if base_key in mkv_keys_by_dir.get(dir_key, set()):
            if args.dry_run:
                print(f"[DRY-RUN] delete: {p}")
            else:
                try:
                    os.remove(p)
                    print(f"deleted: {p}")
                except Exception as e:
                    print(f"failed:  {p}  ({e})")
            deleted += 1
        else:
            kept += 1

    print(f"kept: {kept}   deleted: {deleted}   scanned: {root}   recursive: {args.recursive}")

if __name__ == "__main__":
    main()

