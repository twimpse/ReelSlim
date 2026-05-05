#!/usr/bin/python3
"""
Find likely broken-index video files by parsing ffprobe/ffmpeg warnings.
Defaults to scanning *.avi. Progress bar enabled by default via tqdm.

Examples:
  ./scan_broken_index.py -s /media
  ./scan_broken_index.py -s /media --exts avi,mp4,mov --full-read
  ./scan_broken_index.py -s /media -m --directory /media/quarantine
  ./scan_broken_index.py -s /media -m --directory /media/quarantine -n
  ./scan_broken_index.py -s /media --silent
  ./scan_broken_index.py -s /media --very-silent
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

PAT = re.compile(
    r"(missing index|idx1|estimating duration from bitrate|non-?interleaved|index not found)",
    re.I,
)

def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Detect likely broken-index videos (by warnings). Optionally move them."
    )
    ap.add_argument("-s", "--scan-root", default=".", help="Directory to scan (default: .)")
    ap.add_argument(
        "--exts",
        default="avi",
        help="Comma-separated extensions to scan (no dots). Default: avi. Example: avi,mp4,mov",
    )
    ap.add_argument(
        "--full-read",
        action="store_true",
        help="Use ffmpeg to fully read stream (slower, catches mid-stream errors).",
    )
    ap.add_argument("--timeout", type=int, default=30, help="Per-file timeout seconds (default 30)")
    ap.add_argument("-m", "--move", action="store_true", help="Move suspect files instead of listing")
    ap.add_argument("-D", "--directory", help="Destination directory when using -m")
    ap.add_argument("-n", "--dry-run", action="store_true", help="Preview actions (no changes)")
    mx = ap.add_mutually_exclusive_group()
    mx.add_argument("--silent", action="store_true", help="Hide progress bar; still print hits/summary")
    mx.add_argument("--very-silent", action="store_true", help="Print nothing at all")
    return ap

def suspect(p: Path, full_read: bool, timeout: int) -> bool:
    cmd = (
        ["ffmpeg", "-v", "warning", "-hide_banner", "-i", str(p), "-f", "null", "-"]
        if full_read
        else ["ffprobe", "-v", "warning", "-hide_banner", "-i", str(p)]
    )
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout
        )
        return bool(PAT.search(proc.stdout))
    except Exception:
        return False

def safe_move(src: Path, dst_dir: Path, dry: bool, write) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    target = dst_dir / src.name
    if target.exists():
        stem, suf = src.stem, src.suffix
        i = 1
        while (dst_dir / f"{stem}__dup{i}{suf}").exists():
            i += 1
        target = dst_dir / f"{stem}__dup{i}{suf}"
    if dry:
        write(f"[DRY-RUN] move: {src}  ->  {target}")
    else:
        shutil.move(str(src), str(target))
        write(f"moved: {src}  ->  {target}")
    return target

def main():
    args = build_argparser().parse_args()

    root = Path(args.scan_root)
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")

    exts = {e.strip().lower() for e in args.exts.split(",") if e.strip()}
    if args.move and not args.directory:
        sys.exit("Refusing to move without --directory")

    # Progress: streaming, ticks immediately per file visited
    use_progress = False
    tqdm = None
    bar = None
    if not args.silent and not args.very_silent:
        try:
            from tqdm import tqdm as _tqdm  # type: ignore
            tqdm = _tqdm
            bar = tqdm(desc="Scanning", unit="file")
            bar.refresh()  # force initial draw
            use_progress = True
        except Exception:
            use_progress = False
            if not args.very_silent:
                print("Scanning...")  # minimal heartbeat if tqdm missing

    def writer(msg: str) -> None:
        if args.very_silent:
            return
        if use_progress and tqdm is not None:
            tqdm.write(msg)
        else:
            print(msg)

    found = 0
    moved = 0

    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if bar:
                bar.update(1)  # tick for EVERY file, not just matches
            if p.suffix.lower().lstrip(".") not in exts:
                continue
            if suspect(p, args.full_read, args.timeout):
                found += 1
                if args.move:
                    safe_move(p, Path(args.directory), args.dry_run, writer)
                    moved += 1
                else:
                    writer(f"SUSPECT: {p}")
    except KeyboardInterrupt:
        if not args.very_silent:
            writer("Interrupted. Finishing up...")
    finally:
        if bar:
            bar.close()

    if not args.very_silent:
        writer(f"suspects: {found}   moved: {moved if args.move else 0}   scanned: {root}")

if __name__ == "__main__":
    main()
