#!/usr/bin/python3
# ReelSlim v3.4.2
# Authors: BOFH
# Group: SFS
# Version: 3.4.5
#
# Changelog (since v3.0):
# - v3.2: initial cleanup scaffold (oops, too aggressive)
# - v3.3: restored Analyzer/Encoder/Utils logic
# - v3.4: full, spec-aligned pipeline restored; JSON progress; skip markers;
#         presets; external subs; safe tmp handling; rename/clean-name; concurrency
# - v3.4.2: fixed audio re-encode logic so MP3 is always transcoded to AAC or EAC3 per spec
# - v3.4.3: fixed renaming issue where basename was ignored.
# - v3.4.4: Fixed issue where input audio bitrate was ignored and re-encoded to higher bitrate. 
# - v3.4.5: Added a delete to remove files without a trash dir. 

import os
import re
import sys
import json
import shutil
import argparse
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Event

# ---------------------------- Global State ----------------------------
progress_lock = Lock()
cumulative = {"processed": 0, "skipped": 0, "errors": 0, "saved_bytes": 0}
stop_event = Event()

# ---------------------------- Analyzer ----------------------------
class Analyzer:
    @staticmethod
    def probe_file(filepath):
        cmd = [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(filepath)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
        except Exception:
            return None

        streams = data.get("streams", [])
        vstreams = [s for s in streams if s.get("codec_type") == "video"]
        astreams = [s for s in streams if s.get("codec_type") == "audio"]

        info = {}
        if vstreams:
            v = vstreams[0]
            info["resolution"] = f"{v.get('width',0)}x{v.get('height',0)}"
            info["codec"] = v.get("codec_name", "Unknown")
        else:
            info["resolution"] = "Unknown"
            info["codec"] = "Unknown"

        aud_list = []
        for a in astreams:
            aud_list.append({
                "index": a.get("index"),
                "channels": int(a.get("channels") or 0),
                "codec": a.get("codec_name"),
                "bit_rate": int(a.get("bit_rate") or 0),
                "language": (a.get("tags", {}) or {}).get("language", "Unknown"),
            })
        info["audio_streams"] = aud_list

        fname = Path(filepath).name.lower()
        if "dvd" in fname:
            source = "DVD"
        elif "bluray" in fname or "bdrip" in fname:
            source = "BluRay"
        elif "web" in fname:
            source = "WEBRIP"
        elif "uhd" in fname or "4k" in fname:
            source = "UHD"
        else:
            source = "Unknown"
        info["source"] = source
        info["streams"] = streams
        info["format"] = data.get("format", {})

        return info

    @staticmethod
    def should_reencode(info, args):
        class Decision: ...
        d = Decision()
        d.skip = False
        d.reason = ""
        d.video_action = "copy"
        d.audio_action = "decide"  # decided per stream
        d.scale_height = None
        d.crf = getattr(args, "crf", 22) or 22

        fmt = info.get("format", {})
        try:
            container_bitrate = int(fmt.get("bit_rate", 0))
        except Exception:
            container_bitrate = 0

        res_str = info.get("resolution", "0x0")
        try:
            _, height = [int(x) for x in res_str.split("x")]
        except Exception:
            height = 0

        codec = info.get("codec", "Unknown")

        thresholds = {480: 1_200_000, 720: 2_500_000, 1080: 4_500_000}
        if height <= 480:
            max_allowed = thresholds[480]
        elif height <= 720:
            max_allowed = thresholds[720]
        elif height <= 1080:
            max_allowed = thresholds[1080]
        else:
            max_allowed = thresholds[1080]

        if height < 720:
            d.crf = 25
        elif height < 1080:
            d.crf = 22
        else:
            d.crf = 20

        if getattr(args, "crf", None) is not None:
            try:
                d.crf = int(args.crf)
            except Exception:
                pass

        preset_map = {"hq": 20, "medium": 24, "tv-action": 22, "tv-sitcom": 25}
        if getattr(args, "preset", None) in preset_map and getattr(args, "crf", None) is None:
            d.crf = preset_map[args.preset]

        if getattr(args, "_720p", False) and height > 720:
            d.video_action = "reencode"
            d.scale_height = 720
            d.reason = "Force downscale to 720p"
        elif height > 1080:
            d.video_action = "reencode"
            d.scale_height = 1080
            d.reason = "Downscale to 1080p"

        if codec != "hevc":
            d.video_action = "reencode"
            if not d.reason:
                d.reason = f"Not H.265 ({codec})"
        elif container_bitrate > max_allowed:
            d.video_action = "reencode"
            d.reason = f"H.265 but too high bitrate ({container_bitrate} > {max_allowed})"
        else:
            if d.scale_height is None:
                d.video_action = "copy"
                d.skip = True
                d.reason = "Already efficient H.265 within bitrate/resolution limits"

        return d

# ---------------------------- Encoder ----------------------------
class Encoder:
    PRESET_SETTINGS = {
        "uhq":        {"crf": 20, "maxbitrate": 4500, "audio_2ch": 192, "audio_5_1": 384},
        "hq":        {"crf": 22, "maxbitrate": 2300, "audio_2ch": 192, "audio_5_1": 384},
        "medium":    {"crf": 24, "maxbitrate": 2500, "audio_2ch": 192, "audio_5_1": 192},
        "tv-action": {"crf": 22, "maxbitrate": 2100, "audio_2ch": 192, "audio_5_1": 192},
        "tv-sitcom": {"crf": 25, "maxbitrate": 1500, "audio_2ch": 128, "audio_5_1": 128},
    }

    @staticmethod
    def select_audio_streams(audio_streams, keep_all=False):
        if keep_all:
            return audio_streams
        for lang in ["eng", "en", "swe", "sv"]:
            for a in audio_streams:
                if a.get("language", "").lower() == lang:
                    return [a]
        return audio_streams[:1] if audio_streams else []

    @staticmethod
    def _user_explicit_crf() -> bool:
        return any(arg == "--crf" or arg.startswith("--crf=") for arg in sys.argv)

    @staticmethod
    def _audio_action_for_stream(a, preset_name, keep_all_audio):
        channels = int(a.get("channels", 2) or 2)
        br = int(a.get("bit_rate") or 0) // 1000  # kbps
        codec = (a.get("codec") or "").lower()

        # Preset overrides (always re-encode to fixed target)
        if preset_name in ["medium", "tv-action"]:
            target_b = 192
            actual_b = min(br or target_b, target_b)
            return ["-c:a", "aac", "-ac", "2", "-b:a", f"{actual_b}k"]
        if preset_name == "tv-sitcom":
            target_b = 128
            actual_b = min(br or target_b, target_b)
            return ["-c:a", "aac", "-ac", "2", "-b:a", f"{actual_b}k"]

        # Default rules per spec
        if channels >= 6:
            if codec == "eac3" and br and br <= 384:
                return ["-c:a", "copy"]
            actual_b = min(br or 384, 384)
            return ["-c:a", "eac3", "-b:a", f"{actual_b}k"]
        else:
            # Stereo: AAC capped at 192, but not above source bitrate
            actual_b = 192
            if br and br < 192:
                actual_b = br
            return ["-c:a", "aac", "-b:a", f"{actual_b}k"]
			
    @staticmethod
    def build_ffmpeg_command(input_path, output_path, info, decision, args, crf_override=None):
        input_path_str = str(input_path)
        output_path_str = str(output_path)
        cmd = ["ffmpeg", "-y", "-i", input_path_str]

        # External subtitles
        dirpath = Path(input_path).parent
        base = Path(input_path).stem
        subdirs = [dirpath, dirpath / "Subs", dirpath / "subs", dirpath / "Subtitles", dirpath / "subtitles"]
        srt_files = []
        for d in subdirs:
            srt_files.extend(list(Path(d).glob(f"{base}*.srt")))
        for srt in srt_files:
            cmd += ["-i", str(srt)]

        cmd += ["-map", "0:v:0"]
        preset_settings = Encoder.PRESET_SETTINGS.get(getattr(args, "preset", ""), {})

        if decision.video_action == "reencode":
            cmd += ["-c:v", "libx265"]
            if Encoder._user_explicit_crf():
                final_crf = int(getattr(args, "crf", 22))
            elif crf_override is not None:
                final_crf = int(crf_override)
            elif hasattr(decision, "crf") and decision.crf is not None:
                final_crf = int(decision.crf)
            else:
                final_crf = int(preset_settings.get("crf", 22))
            cmd += ["-crf", str(final_crf)]

            target_h = getattr(decision, "scale_height", None)
            if target_h:
                try:
                    in_h = 0
                    for s in info.get("streams", []):
                        if s.get("codec_type") == "video":
                            in_h = int(s.get("height", 0))
                            break
                except Exception:
                    in_h = 0
                if in_h and target_h < in_h:
                    cmd += ["-vf", f"scale=-2:{target_h}"]

            if preset_settings.get("maxbitrate"):
                maxrate = int(preset_settings["maxbitrate"])
                bufsize = maxrate * 2
                cmd += ["-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k"]
        else:
            cmd += ["-c:v", "copy"]

        # Audio
        preset_name = getattr(args, "preset", "") or ""
        selected_audio = Encoder.select_audio_streams(info.get("audio_streams", []), keep_all=getattr(args, "keep_all_audio", False))
        for a in selected_audio:
            idx = a.get("index")
            cmd += ["-map", f"0:{idx}"]
            cmd += Encoder._audio_action_for_stream(a, preset_name, getattr(args, "keep_all_audio", False))

        # Subtitles
        cmd += ["-map", "0:s?", "-c:s", "copy"]
        subtitle_idx = 1
        for _ in srt_files:
            cmd += ["-map", f"{subtitle_idx}:0", "-c:s", "srt"]
            subtitle_idx += 1

        cmd += ["-map_metadata", "0", "-map_chapters", "0"]
        cmd += ["-metadata", f"original_filename={Path(input_path).name}"]
        cmd += ["-metadata", f"original_folder={Path(input_path).parent}"]
        cmd += ["-metadata", f"reencode_time={datetime.now().isoformat()}"]
        cmd += ["-metadata", f"resolution={info.get('resolution')}"]
        cmd += ["-metadata", "group=SFS"]
        cmd += ["-metadata", f"source={info.get('source')}"]

        cmd += ["-f", "matroska", output_path_str]
        return cmd

    @staticmethod
    def encode_file(input_path, output_path, info, decision, args, logger, max_retries=2):
        tmp_output = output_path + ".tmp"
        retries = 0
        if Encoder._user_explicit_crf():
            current_crf = int(getattr(args, "crf", 22))
        else:
            current_crf = getattr(decision, "crf", 22)

        while retries <= max_retries:
            cmd = Encoder.build_ffmpeg_command(input_path, tmp_output, info, decision, args, crf_override=current_crf)
            logger.log_ffmpeg(input_path, cmd, retries + 1)

            if getattr(args, "dry_run", False):
                return True

            try:
                subprocess.run(cmd, check=True)
            except subprocess.CalledProcessError:
                logger.log_error(input_path, f"FFmpeg failed attempt {retries+1}")
                return False

            try:
                if os.path.getsize(tmp_output) > os.path.getsize(input_path):
                    retries += 1
                    current_crf = int(current_crf) + 2
                    logger.log(f"{input_path}: Oversized after attempt {retries}, retrying with CRF {current_crf}")
                    if retries > max_retries:
                        skip_marker = Path(input_path).parent / f".skip-reencode-{Path(input_path).name}"
                        skip_marker.touch()
                        os.remove(tmp_output)
                        logger.log_skip(input_path, "Oversized after retries")
                        return False
                    continue
            except Exception as e:
                logger.log_error(input_path, f"Size compare failed: {e}")
                return False

            break

        if os.path.exists(tmp_output):
            os.replace(tmp_output, output_path)
        return True

# ---------------------------- Logging ----------------------------
class Logger:
    def __init__(self, log_file=None):
        self.log_file = Path(log_file) if log_file else None
        self.lock = threading.Lock()
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, msg):
        with self.lock:
            print(msg)
            if self.log_file:
                with self.log_file.open("a", encoding="utf-8") as f:
                    f.write(msg + "\n")

    def log(self, msg):
        self._write(msg)

    def log_error(self, filepath, msg):
        self._write(f"ERROR: {filepath} -> {msg}")

    def log_skip(self, filepath, reason):
        self._write(f"SKIPPED: {filepath} ({reason})")

    def log_ffmpeg(self, filepath, cmd, attempt):
        self._write(f"FFmpeg command attempt {attempt}: {filepath}")
        self._write(" ".join(cmd))

class ProgressStore:
    def __init__(self, progress_file: str):
        self.progress_path = Path(progress_file)
        self.data = {"files": {}, "summary": {}}
        self.lock = threading.Lock()
        if not self.progress_path.parent.exists():
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)

    def log_done(self, src, orig_size, new_size, out_path):
        with self.lock:
            self.data["files"][src] = {
                "status": "done",
                "timestamp": datetime.now().isoformat(),
                "original_size": orig_size,
                "new_size": new_size,
                "saved": max(0, orig_size - new_size),
                "output": out_path,
            }
            self._flush()

    def log_skip(self, src, reason):
        with self.lock:
            self.data["files"][src] = {
                "status": "skip",
                "timestamp": datetime.now().isoformat(),
                "reason": reason,
            }
            self._flush()

    def log_error(self, src, reason):
        with self.lock:
            self.data["files"][src] = {
                "status": "error",
                "timestamp": datetime.now().isoformat(),
                "reason": reason,
            }
            self._flush()

    def summary(self, processed_count, saved_mb):
        with self.lock:
            self.data["summary"] = {
                "processed": processed_count,
                "saved_mb": round(saved_mb, 2),
                "updated": datetime.now().isoformat(),
            }
            self._flush()

    def _flush(self):
        try:
            with self.progress_path.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass

# ---------------------------- Utils ----------------------------
DEFAULT_CLEAN_KEYWORDS = [
    "1080p","720p", "480p", "1080", "720", "480", "HD","Webrip","DVD","DVDrip","10bit",
    "DDP5.1","H265","H264","AMZN","Web-DL","BluRay", "HULU", "Web-Dl", "web", "dl", "AC3",
    "HDRIP", "XVID", "DVDIVX", "INT", "INTERNAL", "SUBS", "X265", "x264", "AAC"
]

class Utils:
    @staticmethod
    def clean_filename(filename, keywords=None):
        if keywords is None:
            keywords = DEFAULT_CLEAN_KEYWORDS
        name = Path(filename).stem
        for kw in keywords:
            pattern = re.compile(re.escape(kw), re.IGNORECASE)
            name = pattern.sub("", name)
        name = re.sub(r"-.*$", "", name)
        name = name.strip()
        return name

    @staticmethod
    def replace_spaces_with_periods(filename):
        ext = Path(filename).suffix
        name = Path(filename).stem
        name = name.replace(" ", ".")
        return f"{name}{ext}"

    @staticmethod
    def rename_file(original, info, mode="FULL"):
        # Clean base name (no extension)
        base = Path(original).stem
        base_clean = Utils.clean_filename(base)
        base_clean = base_clean.replace(" ", ".")
        base_clean = re.sub(r"[^\\w\\.\\-]", "", base_clean)

        # Extract resolution height and aspect ratio if available
        res_str = info.get("resolution", "0x0")
        try:
            w, h = [int(x) for x in res_str.split("x")]
        except Exception:
            w, h = (0, 0)
        res_token = "Unknown"
        if h:
            res_token = f"{h}p"
            if w and (w / h) >= 1.7:
                res_token = f"WS.{res_token}"

        src = info.get("source", "Unknown")
        audio = info.get("audio", "Unknown")
        codec = "x265"
        group = globals().get("dist_group", "SFS")

        mode_up = (mode or "").upper()
        if mode_up == "FULL":
            tokens = [res_token, src, audio, codec]
        elif mode_up == "BASIC":
            tokens = [res_token, audio, codec]
        elif mode_up == "ULL":
            tokens = [res_token, src, audio, f"{codec}-[{group}]"]
        elif mode_up == "SIMPLE":
            tokens = []
        else:
            tokens = []

        suffix = ("." + ".".join(tokens)) if tokens else ""
        new_name = f"{base_clean}{suffix}.mkv"
        return new_name

# ---------------------------- Pipeline ----------------------------
VIDEO_EXTS = [".mp4", ".mkv", ".avi"]

def discover_videos(source_dir):
    files = []
    for root, _, filenames in os.walk(source_dir):
        for f in filenames:
            if any(f.lower().endswith(ext) for ext in VIDEO_EXTS):
                files.append(os.path.join(root, f))
    return files


def process_file(filepath, args, console_log: Logger, progress: ProgressStore):
    if stop_event.is_set():
        return

    filepath = str(filepath)
    src_path = Path(filepath)

    # Skip markers
    if (src_path.parent / ".skip-reencode").exists():
        console_log.log_skip(filepath, "Global skip marker present")
        progress.log_skip(filepath, "Global skip marker present")
        return
    if (src_path.parent / f".skip-reencode-{src_path.name}").exists():
        console_log.log_skip(filepath, "File-specific skip marker present")
        progress.log_skip(filepath, "File-specific skip marker present")
        return

    console_log.log(f"Probing: {filepath}")
    info = Analyzer.probe_file(filepath)
    if info is None:
        console_log.log_error(filepath, "ffprobe failed")
        progress.log_error(filepath, "ffprobe failed")
        with progress_lock:
            cumulative["errors"] += 1
        return

    decision = Analyzer.should_reencode(info, args)
    if decision.skip and not args.force:
        reason = decision.reason or "Marked skip / already optimized"
        console_log.log_skip(filepath, reason)
        progress.log_skip(filepath, reason)
        with progress_lock:
            cumulative["skipped"] += 1
        return

    out_path = src_path.with_suffix(".mkv")
    console_log.log(f"Encoding: {filepath} -> {out_path} (video_action={decision.video_action}, crf={decision.crf}, downscale={decision.scale_height})")

    success = Encoder.encode_file(filepath, str(out_path), info, decision, args, console_log)
    if not success:
        console_log.log_error(filepath, "Encoding failed")
        progress.log_error(filepath, "encoding_failed")
        with progress_lock:
            cumulative["errors"] += 1
        return

    try:
        orig_size = os.path.getsize(filepath)
        new_size = os.path.getsize(out_path)
    except Exception as e:
        console_log.log_error(filepath, f"Size check failed: {e}")
        progress.log_error(filepath, f"size_check_failed: {e}")
        with progress_lock:
            cumulative["errors"] += 1
        return

    if new_size > orig_size and not args.force:
        console_log.log_skip(filepath, "New file larger than original after encoding")
        progress.log_skip(filepath, "oversized_after_encoding")
        try:
            Path(out_path).unlink(missing_ok=True)
        except Exception:
            pass
        (src_path.parent / f".skip-reencode-{src_path.name}").touch()
        with progress_lock:
            cumulative["skipped"] += 1
        return

    final_path = Path(out_path)

    # Optional rename
    if args.rename:
        meta = {
            "resolution": info.get("resolution", "Unknown"),
            "source": info.get("source", "Unknown"),
            "audio": ",".join([str(a.get("codec","Unknown")) for a in info.get("audio_streams",[])]),
            "video_codec": info.get("codec","Unknown"),
            "group": "SFS",
        }
        new_name = Utils.rename_file(str(out_path), meta, mode=args.rename)
        new_path = final_path.parent / new_name
        if new_path.exists():
            new_path.unlink()
        final_path.rename(new_path)
        final_path = new_path

    # Optional clean-name if not using rename
    if args.clean_name and not args.rename:
        clean = Utils.clean_filename(str(final_path))
        clean_path = final_path.parent / f"{clean}.mkv"
        if clean_path.exists():
            clean_path.unlink()
        final_path.rename(clean_path)
        final_path = clean_path

    # Remove or move original after success
    if args.trash:
        Path(args.trash).mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src_path), str(Path(args.trash) / src_path.name))
        except Exception as e:
            console_log.log_error(filepath, f"Failed move to trash: {e}")
    elif args.delete:
        try:
            os.remove(src_path)
        except Exception as e:
            console_log.log_error(filepath, f"Failed to delete original: {e}")

    saved = orig_size - os.path.getsize(final_path)
    with progress_lock:
        cumulative["processed"] += 1
        cumulative["saved_bytes"] += max(0, saved)
    progress.log_done(filepath, orig_size, os.path.getsize(final_path), str(final_path))
    console_log.log(f"Done: {filepath} -> {final_path} (saved {saved//1024//1024} MB)")

# ---------------------------- CLI / Main ----------------------------

def find_and_delete_tmps(root: Path, logger: Logger):
    count = 0
    for p in root.rglob("*.tmp"):
        try:
            p.unlink()
            count += 1
        except Exception:
            pass
    for p in root.rglob("*.tmp.mkv"):
        try:
            p.unlink()
            count += 1
        except Exception:
            pass
    if count:
        logger.log(f"Removed {count} stray .tmp files")


def discover_source_list(source: str):
    p = Path(source)
    if p.is_file():
        return [str(p)]
    return discover_videos(str(p))


def main():
    parser = argparse.ArgumentParser(description="ReelSlim - Batch Video Re-Encoder with Optional Downscale (v8.4)")
    parser.add_argument("-s", "--source", required=True, help="Input directory containing videos or single file")
    parser.add_argument("-t", "--trash", help="Directory to move originals after re-encode")
    parser.add_argument("--progress", default="progress.json", help="Progress JSON file path")
    parser.add_argument("-w", "--workers", type=int, default=2, help="Number of parallel workers")
    parser.add_argument("--total-vids", type=int, help="Limit number of videos processed")
    parser.add_argument("--dry-run", action="store_true", help="Simulate encoding, no changes")
    parser.add_argument("--crf", type=int, default=22, help="Override CRF value")
    parser.add_argument("--720p", dest="_720p", action="store_true", help="Force downscale to 720p")
    parser.add_argument("--preset", choices=["hq", "medium", "tv-action", "tv-sitcom"], help="Quality preset")
    parser.add_argument("--force", "-f", action="store_true", help="Force re-encode even if criteria not met")
    parser.add_argument("--keep-all-audio", action="store_true", help="Preserve all audio streams")
    parser.add_argument("--clean-name", action="store_true", help="Remove keywords from filename")
    parser.add_argument("--rename", help="Rename mode: FULL|BASIC|ULL|SIMPLE")
    parser.add_argument("--debug", help="Debug log filename (defaults to re-encode.log in source dir)")
    parser.add_argument("--delete", action="store_true", help="Delete original file names without a trash dir")
    args = parser.parse_args()

    # Determine log location
    src_path = Path(args.source)
    log_dir = src_path.parent if src_path.is_file() else src_path
    log_file = args.debug if args.debug else str(log_dir / "re-encode.log")
    console_log = Logger(log_file=log_file)
    progress = ProgressStore(progress_file=str(log_dir / args.progress))

    # Safety: clean orphan .tmp files at start
    root_for_tmp = log_dir if src_path.is_dir() else src_path.parent
    find_and_delete_tmps(root_for_tmp, console_log)

    # Discover files
    files = discover_source_list(args.source)
    if args.total_vids:
        files = files[: args.total_vids]

    # Warn if workers > CPU cores
    try:
        cpu = os.cpu_count() or 1
        if args.workers > cpu:
            console_log.log(f"Warning: workers ({args.workers}) > CPU cores ({cpu})")
    except Exception:
        pass

    # Process
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_file, f, args, console_log, progress): f for f in files}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except KeyboardInterrupt:
                    stop_event.set()
                    console_log.log("Interrupted by user. Stopping new work and waiting for running jobs to finish...")
                    break
                except Exception as e:
                    console_log.log(f"Unhandled exception: {e}")
                    with progress_lock:
                        cumulative["errors"] += 1
    except KeyboardInterrupt:
        stop_event.set()
        console_log.log("Interrupted by user. Waiting for running jobs to finish...")
    finally:
        # Finalize JSON summary
        saved_mb = cumulative["saved_bytes"] / 1024.0 / 1024.0
        console_log.log(
            f"Batch complete. Processed: {cumulative['processed']}, Skipped: {cumulative['skipped']}, Errors: {cumulative['errors']}, Total saved: {saved_mb:.2f} MB"
        )
        progress.summary(cumulative["processed"], saved_mb)

if __name__ == "__main__":
    main()


