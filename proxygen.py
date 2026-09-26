#!/usr/bin/env python3
"""
ProxyGen - DPX / Image Sequence Proxy Generator for DaVinci Resolve
=====================================================================

Scans a folder tree for image sequences (DPX, EXR, TIFF, JPG, PNG) or large
video files, and transcodes them into Resolve-compatible proxy media using
ffmpeg, preserving source timecode and frame rate wherever the metadata is
present, and WITHOUT applying any color space conversion or LUT (the color
data is passed through unchanged so Resolve's color management handles it
identically to the original camera/scan files).

Requirements:
    - Python 3.8+
    - ffmpeg / ffprobe available on PATH
    - tkinter (bundled with most Python installs; on Linux: `sudo apt
      install python3-tk` if missing)

Usage:
    python3 proxygen.py

Author: generated for a DaVinci Resolve DPX proxy workflow.
"""

import os
import re
import sys
import json
import time
import shutil
import struct
import threading
import subprocess
import shlex
from dataclasses import dataclass, field, asdict
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SEQUENCE_EXTS = {".dpx", ".exr", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}
VIDEO_EXTS = {".mov", ".mp4", ".mxf", ".avi", ".mkv", ".m4v"}

# Regex to split "prefix" + "frame number" + "extension"
# e.g. hombres_del_norte-01-508-A_0259207.dpx
#      -> prefix="hombres_del_norte-01-508-A_", number="0259207", ext=".dpx"
FRAME_RE = re.compile(r"^(?P<prefix>.*?)(?P<num>\d+)(?P<ext>\.[A-Za-z0-9]+)$")

# Output codec profiles for the "encode to video" path.
# Each maps to ffmpeg args. All output MOV containers, which Resolve reads
# natively for ProRes, DNxHR, H.264 and H.265.
CODEC_PROFILES = {
    "ProRes Proxy": {
        "args": ["-c:v", "prores_ks", "-profile:v", "0",
                 "-vendor", "apl0", "-pix_fmt", "yuv422p10le"],
        "container": ".mov",
    },
    "DNxHR LB": {
        "args": ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb",
                 "-pix_fmt", "yuv422p"],
        "container": ".mov",
        "mod8": True,  # DNxHR requires dimensions divisible by 8
    },
    "H.264 (8-bit)": {
        "args": ["-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-crf", "18", "-preset", "medium"],
        "container": ".mov",
    },
    "H.265 (10-bit)": {
        "args": ["-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                 "-crf", "20", "-preset", "medium", "-tag:v", "hvc1"],
        "container": ".mov",
    },
}

# Output image-sequence formats for the "downres to sequence" path.
IMAGE_SEQ_PROFILES = {
    "EXR": {"ext": ".exr", "args": ["-pix_fmt", "rgba64le"], "vcodec": "exr"},
    "TIFF": {"ext": ".tif", "args": ["-pix_fmt", "rgb48le"], "vcodec": "tiff"},
    "JPG": {"ext": ".jpg", "args": ["-pix_fmt", "yuvj420p", "-q:v", "2"], "vcodec": "mjpeg"},
    "PNG": {"ext": ".png", "args": ["-pix_fmt", "rgb24"], "vcodec": "png"},
}

PROGRESS_FILENAME = ".proxygen_progress.json"
DEFAULT_FOLDER_FILE = os.path.expanduser("~/.default_dir")

# Matches the "frame=  123" field ffmpeg prints with -stats, used to track
# progress *within* a currently-running job for the live progress bar/ETA.
FFMPEG_FRAME_RE = re.compile(r"frame=\s*(\d+)")


def human_size(num_bytes):
    """Format a byte count as a human-readable string, e.g. '4.2 GB'."""
    if num_bytes is None:
        return "?"
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(seconds):
    """Format a duration in seconds as HH:MM:SS (or MM:SS if under an hour)."""
    if seconds is None or seconds < 0 or seconds != seconds:  # None/negative/NaN
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def read_default_dir():
    """
    Read the last-used source folder from ~/.default_dir, if present and
    still a valid directory. Returns None otherwise so callers can fall
    back to the user's home directory. Same file/convention used by
    dpx_scene_detect_gui.py.
    """
    try:
        with open(DEFAULT_FOLDER_FILE, "r") as f:
            path = f.read().strip()
        return path if path and os.path.isdir(path) else None
    except (FileNotFoundError, OSError):
        return None


def write_default_dir(path):
    """
    Persist the source folder to ~/.default_dir so it's offered as the
    starting point next time the app is launched. Only ever called from
    the source-folder picker -- the output folder picker reads it for
    convenience but never writes back to it.
    """
    try:
        with open(DEFAULT_FOLDER_FILE, "w") as f:
            f.write(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# DPX header parsing (SMPTE 268M)
# --------------------------------------------------------------------------

def parse_dpx_header(path):
    """
    Read timecode, frame rate, width/height directly from a DPX file's
    binary header. Returns a dict; missing/unreadable fields are None.
    This does NOT decode or touch pixel data, so color is left untouched.
    """
    result = {"timecode": None, "framerate": None, "width": None, "height": None}
    try:
        with open(path, "rb") as f:
            data = f.read(2048)
        if len(data) < 2048:
            return result

        magic = data[0:4]
        if magic == b"SDPX":
            endian = ">"
        elif magic == b"XPDS":
            endian = "<"
        else:
            return result  # not a DPX file / unreadable header

        # Generic Image Header (offset 768): width/height
        img_hdr = 768
        result["width"] = struct.unpack(endian + "I", data[img_hdr + 4:img_hdr + 8])[0]
        result["height"] = struct.unpack(endian + "I", data[img_hdr + 8:img_hdr + 12])[0]

        # Motion Picture Film Header (offset 1664): frame rate (fallback)
        mp = 1664
        try:
            mp_rate = struct.unpack(endian + "f", data[mp + 64:mp + 68])[0]
            if mp_rate and mp_rate == mp_rate and mp_rate > 0:  # NaN check
                result["framerate"] = round(mp_rate, 3)
        except struct.error:
            pass

        # Television Header (offset 1920): timecode + frame rate (preferred)
        tv = 1920
        timecode_raw = data[tv:tv + 4]
        if timecode_raw != b"\xff\xff\xff\xff" and timecode_raw != b"\x00\x00\x00\x00":
            tc = _bcd_to_timecode(timecode_raw, endian)
            if tc:
                result["timecode"] = tc
        try:
            tv_rate = struct.unpack(endian + "f", data[tv + 20:tv + 24])[0]
            if tv_rate and tv_rate == tv_rate and tv_rate > 0:
                result["framerate"] = round(tv_rate, 3)
        except struct.error:
            pass

    except (OSError, struct.error):
        pass
    return result


def _bcd_to_timecode(raw, endian):
    try:
        b = raw if endian == ">" else raw[::-1]
        hh = ((b[0] >> 4) & 0xF) * 10 + (b[0] & 0xF)
        mm = ((b[1] >> 4) & 0xF) * 10 + (b[1] & 0xF)
        ss = ((b[2] >> 4) & 0xF) * 10 + (b[2] & 0xF)
        ff = ((b[3] >> 4) & 0xF) * 10 + (b[3] & 0xF)
        if hh > 23 or mm > 59 or ss > 59 or ff > 59:
            return None
        return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
    except (IndexError, ValueError):
        return None


def probe_generic_metadata(path):
    """
    For non-DPX sequence formats (EXR/TIFF/JPG/PNG) reliable embedded
    timecode/framerate is uncommon, so we try ffprobe and fall back to
    None if nothing usable is found. Caller should apply user defaults.
    """
    result = {"timecode": None, "framerate": None}
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(out.stdout or "{}")
        tags = data.get("format", {}).get("tags", {})
        for key in ("timecode", "time_code", "TIMECODE"):
            if key in tags:
                result["timecode"] = tags[key]
                break
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass
    return result


def probe_image_dimensions(path):
    """Get width/height of a single image frame via ffprobe (non-DPX formats)."""
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        data = json.loads(out.stdout or "{}")
        for stream in data.get("streams", []):
            if stream.get("width") and stream.get("height"):
                return stream["width"], stream["height"]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass
    return None, None


def probe_video(path):
    """Get width/height/duration/fps/estimated-frame-count/size for a video file."""
    info = {"width": None, "height": None, "duration": None, "fps": None,
            "frames": None, "size_bytes": None}
    try:
        info["size_bytes"] = os.path.getsize(path)
    except OSError:
        pass
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        data = json.loads(out.stdout or "{}")
        duration = float(data.get("format", {}).get("duration") or 0) or None
        info["duration"] = duration
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width")
                info["height"] = stream.get("height")
                nb = stream.get("nb_frames")
                rate_str = stream.get("r_frame_rate", "0/1")
                try:
                    num, den = rate_str.split("/")
                    fps = float(num) / float(den) if float(den) else None
                except (ValueError, ZeroDivisionError):
                    fps = None
                info["fps"] = fps
                if nb and str(nb).isdigit():
                    info["frames"] = int(nb)
                elif duration and fps:
                    info["frames"] = int(round(duration * fps))
                break
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass
    return info


def build_job_info(job):
    """
    Gather display/progress info for a scanned job: frame count, format,
    resolution, and total size on disk. Attached to the job so the GUI can
    show it in the status table and the controller can use it for the
    frame-weighted progress bar / ETA.
    """
    if isinstance(job, SequenceJob):
        width = height = None
        if job.ext == ".dpx":
            meta = parse_dpx_header(job.sample_file)
            width, height = meta.get("width"), meta.get("height")
        if width is None:
            width, height = probe_image_dimensions(job.sample_file)
        try:
            per_frame = os.path.getsize(job.sample_file)
        except OSError:
            per_frame = 0
        return {
            "frames": job.frame_count,
            "format": job.ext.lstrip(".").upper(),
            "resolution": f"{width}x{height}" if width and height else "?",
            "size_bytes": per_frame * job.frame_count,
        }
    else:  # VideoJob
        v = probe_video(job.path)
        return {
            "frames": v["frames"] or 1,
            "format": Path(job.path).suffix.lstrip(".").upper(),
            "resolution": f"{v['width']}x{v['height']}" if v["width"] and v["height"] else "?",
            "size_bytes": v["size_bytes"],
        }


# --------------------------------------------------------------------------
# Job discovery
# --------------------------------------------------------------------------

@dataclass
class SequenceJob:
    kind: str  # "sequence"
    directory: str
    prefix: str
    ext: str
    padding: int
    start_frame: int
    end_frame: int
    frame_count: int
    missing_frames: list = field(default_factory=list)

    @property
    def key(self):
        return f"seq::{self.directory}::{self.prefix}::{self.ext}"

    @property
    def output_name(self):
        return resolve_clip_name(self.prefix)

    @property
    def input_pattern(self):
        return str(Path(self.directory) / f"{self.prefix}%0{self.padding}d{self.ext}")

    @property
    def sample_file(self):
        return str(Path(self.directory) / f"{self.prefix}{str(self.start_frame).zfill(self.padding)}{self.ext}")


@dataclass
class VideoJob:
    kind: str  # "video"
    path: str

    @property
    def key(self):
        return f"vid::{self.path}"

    @property
    def output_name(self):
        return Path(self.path).stem


def resolve_clip_name(prefix):
    """
    Replicates DaVinci Resolve's proxy-matching clip name: strip the
    trailing separator left after removing the frame-number portion.
    Confirmed against Resolve test import:
      "hombres_del_norte-01-508-A_0259207.dpx" (sequence)
        -> Resolve proxy clip name: "hombres_del_norte-01-508-A"
    """
    return prefix.rstrip("_-. ")


def scan_folder(root_dir, mode):
    """
    Walk root_dir. In 'sequence' mode, group image-sequence files into
    SequenceJob objects. In 'video' mode, collect video files as VideoJob.
    """
    jobs = []
    root_dir = str(root_dir)

    if mode == "sequence":
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            groups = {}  # (prefix, ext, padding) -> sorted list of frame numbers
            for fn in filenames:
                ext = Path(fn).suffix.lower()
                if ext not in SEQUENCE_EXTS:
                    continue
                m = FRAME_RE.match(fn)
                if not m:
                    continue
                prefix = m.group("prefix")
                num_str = m.group("num")
                padding = len(num_str)
                key = (prefix, ext, padding)
                groups.setdefault(key, []).append(int(num_str))

            for (prefix, ext, padding), nums in groups.items():
                nums.sort()
                start, end = nums[0], nums[-1]
                full_range = set(range(start, end + 1))
                missing = sorted(full_range - set(nums))
                jobs.append(SequenceJob(
                    kind="sequence",
                    directory=dirpath,
                    prefix=prefix,
                    ext=ext,
                    padding=padding,
                    start_frame=start,
                    end_frame=end,
                    frame_count=len(nums),
                    missing_frames=missing,
                ))
    else:  # video downres mode
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for fn in filenames:
                ext = Path(fn).suffix.lower()
                if ext in VIDEO_EXTS:
                    jobs.append(VideoJob(kind="video", path=str(Path(dirpath) / fn)))

    return jobs


# --------------------------------------------------------------------------
# ffmpeg command construction
# --------------------------------------------------------------------------

def build_scale_filter(half_res, mod8=False):
    if not half_res and not mod8:
        return None
    if half_res:
        base = "iw/2:ih/2"
    else:
        base = "iw:ih"
    if mod8:
        # round down to nearest multiple of 8 (required by DNxHR)
        return f"scale=trunc(({base.split(':')[0]})/8)*8:trunc(({base.split(':')[1]})/8)*8"
    else:
        # round down to nearest even number (required by most YUV codecs)
        return f"scale=trunc(({base.split(':')[0]})/2)*2:trunc(({base.split(':')[1]})/2)*2"


def build_video_output_cmd(job, settings, out_path):
    """Build an ffmpeg command that encodes a proxy MOV from a job."""
    profile = CODEC_PROFILES[settings["codec"]]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats"]

    fps = settings["fallback_fps"]
    tc = settings["fallback_tc"]

    if job.kind == "sequence":
        meta = parse_dpx_header(job.sample_file) if job.ext == ".dpx" else probe_generic_metadata(job.sample_file)
        if meta.get("framerate"):
            fps = meta["framerate"]
        if meta.get("timecode"):
            tc = meta["timecode"]
        cmd += ["-f", "image2", "-framerate", str(fps),
                "-start_number", str(job.start_frame), "-i", job.input_pattern]
    else:
        cmd += ["-i", job.path]

    vf = build_scale_filter(settings["half_res"], mod8=profile.get("mod8", False))
    if vf:
        cmd += ["-vf", vf]

    cmd += profile["args"]

    if tc:
        cmd += ["-timecode", tc]

    # No color conversion / LUT: colorspace tags are left unspecified so
    # Resolve does not attempt any implicit correction on the proxy that
    # wasn't already present on the source.
    cmd += ["-color_primaries", "unknown", "-color_trc", "unknown", "-colorspace", "unknown"]

    if job.kind == "video":
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    else:
        cmd += ["-an"]

    cmd += [str(out_path)]
    return cmd


def build_image_seq_output_cmd(job, settings, out_dir):
    """Build an ffmpeg command that downconverts a video into an image sequence."""
    profile = IMAGE_SEQ_PROFILES[settings["image_format"]]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats", "-i", job.path]

    vf = build_scale_filter(settings["half_res"])
    if vf:
        cmd += ["-vf", vf]

    cmd += profile["args"]
    out_pattern = str(Path(out_dir) / f"{job.output_name}_%06d{profile['ext']}")
    cmd += [out_pattern]
    return cmd, out_pattern


# --------------------------------------------------------------------------
# Progress persistence (enables pause / resume across app restarts)
# --------------------------------------------------------------------------

class ProgressStore:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"jobs": {}}
        self.load()

    def load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.data = {"jobs": {}}

    def save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)
        except OSError:
            pass

    def status(self, key):
        return self.data["jobs"].get(key, {}).get("status", "pending")

    def mark(self, key, status, extra=None):
        entry = self.data["jobs"].setdefault(key, {})
        entry["status"] = status
        if extra:
            entry.update(extra)
        self.save()


# --------------------------------------------------------------------------
# Background processing controller
# --------------------------------------------------------------------------

class ProcessController:
    def __init__(self, log_fn, progress_fn, job_status_fn=None):
        self.log_fn = log_fn
        self.progress_fn = progress_fn          # (frames_done, frames_total, elapsed, eta)
        self.job_status_fn = job_status_fn or (lambda key, status: None)
        self._thread = None
        self._pause_evt = threading.Event()
        self._pause_evt.set()  # set = not paused
        self._stop_evt = threading.Event()
        self._current_proc = None
        self.running = False
        self._start_time = None
        self._frames_done_base = 0   # frames completed in fully-finished jobs
        self._frames_total = 0
        self._current_job_frames = 0

    def start(self, jobs, settings, store):
        if self.running:
            return
        self.running = True
        self._stop_evt.clear()
        self._pause_evt.set()
        self._start_time = time.time()
        self._frames_total = sum(getattr(j, "total_frames", 1) for j in jobs)
        self._frames_done_base = sum(getattr(j, "total_frames", 1) for j in jobs if store.status(j.key) == "done")
        self._thread = threading.Thread(target=self._run, args=(jobs, settings, store), daemon=True)
        self._thread.start()

    def pause(self):
        self._pause_evt.clear()
        self.log_fn("Paused.")

    def resume(self):
        self._pause_evt.set()
        self.log_fn("Resumed.")

    def stop(self):
        self._stop_evt.set()
        self._pause_evt.set()  # unblock if paused, so it can exit
        if self._current_proc and self._current_proc.poll() is None:
            self._current_proc.terminate()
        self.log_fn("Stop requested.")

    def _emit_progress(self):
        done = self._frames_done_base + self._current_job_frames
        elapsed = time.time() - self._start_time
        remaining = max(self._frames_total - done, 0)
        # Avoid a noisy/misleading ETA in the first moment of a run, before
        # there's enough throughput data to extrapolate from.
        eta = (elapsed / done * remaining) if (done > 0 and elapsed > 1) else None
        self.progress_fn(min(done, self._frames_total), self._frames_total, elapsed, eta)

    def _run(self, jobs, settings, store):
        total = len(jobs)
        done_count = sum(1 for j in jobs if store.status(j.key) == "done")
        self._emit_progress()

        for i, job in enumerate(jobs, 1):
            if self._stop_evt.is_set():
                break
            self._pause_evt.wait()
            if self._stop_evt.is_set():
                break

            if store.status(job.key) == "done":
                continue

            self._current_job_frames = 0
            self.job_status_fn(job.key, "Processing")
            out_dir = Path(settings["output_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)

            try:
                if settings["mode"] == "sequence" or (settings["mode"] == "video" and not settings["to_sequence"]):
                    profile = CODEC_PROFILES[settings["codec"]]
                    out_path = out_dir / f"{job.output_name}{profile['container']}"
                    cmd = build_video_output_cmd(job, settings, out_path)
                    self.log_fn(f"[{i}/{total}] Encoding: {job.output_name} -> {out_path.name}")
                    self._run_ffmpeg(cmd, job)
                else:
                    seq_out_dir = out_dir / job.output_name
                    seq_out_dir.mkdir(parents=True, exist_ok=True)
                    cmd, out_pattern = build_image_seq_output_cmd(job, settings, seq_out_dir)
                    self.log_fn(f"[{i}/{total}] Converting to sequence: {job.output_name}")
                    self._run_ffmpeg(cmd, job)

                if self._stop_evt.is_set():
                    store.mark(job.key, "pending")
                    self.job_status_fn(job.key, "Stopped")
                    self.log_fn(f"Stopped during: {job.output_name} (will resume from here)")
                    break

                store.mark(job.key, "done")
                self.job_status_fn(job.key, "Done")
                done_count += 1
                self._frames_done_base += getattr(job, "total_frames", 1)
                self._current_job_frames = 0
                self._emit_progress()

            except subprocess.CalledProcessError as e:
                store.mark(job.key, "error", {"error": str(e)})
                self.job_status_fn(job.key, "Error")
                self.log_fn(f"ERROR on {job.output_name}: {e}")

        self.running = False
        if self._stop_evt.is_set():
            self.log_fn("Stopped by user.")
        else:
            self.log_fn("All jobs complete." if done_count == total else "Batch finished (some jobs skipped/errored).")

    def _run_ffmpeg(self, cmd, job):
        self.log_fn("  " + " ".join(shlex.quote(c) for c in cmd))
        self._current_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True)
        for line in self._current_proc.stdout:
            if self._stop_evt.is_set():
                self._current_proc.terminate()
                break
            line = line.strip()
            if line:
                self.log_fn("  " + line)
                m = FFMPEG_FRAME_RE.search(line)
                if m:
                    self._current_job_frames = min(int(m.group(1)), getattr(job, "total_frames", 1))
                    self._emit_progress()
        ret = self._current_proc.wait()
        if ret != 0 and not self._stop_evt.is_set():
            raise subprocess.CalledProcessError(ret, cmd)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class ProxyGenApp:
    def __init__(self, root):
        self.root = root
        root.title("ProxyGen - DaVinci Resolve Proxy Generator")
        root.geometry("880x680")

        self.source_dir = tk.StringVar(value=read_default_dir() or "")
        self.output_dir = tk.StringVar()
        self.mode = tk.StringVar(value="sequence")  # "sequence" or "video"
        self.to_sequence = tk.BooleanVar(value=False)
        self.half_res = tk.BooleanVar(value=True)
        self.codec = tk.StringVar(value="ProRes Proxy")
        self.image_format = tk.StringVar(value="EXR")
        self.fallback_fps = tk.StringVar(value="24")
        self.fallback_tc = tk.StringVar(value="01:00:00:00")

        self.jobs = []
        self.controller = None
        self.store = None

        self._build_ui()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # Mode selection
        mode_frame = ttk.LabelFrame(self.root, text="Mode")
        mode_frame.pack(fill="x", **pad)
        ttk.Radiobutton(mode_frame, text="Image Sequence -> Proxy Video (DPX/EXR/TIFF/JPG/PNG)",
                        variable=self.mode, value="sequence",
                        command=self._on_mode_change).pack(anchor="w", padx=8, pady=2)
        ttk.Radiobutton(mode_frame, text="Downres Video File -> Smaller Video or Image Sequence",
                        variable=self.mode, value="video",
                        command=self._on_mode_change).pack(anchor="w", padx=8, pady=2)

        # Folders
        folder_frame = ttk.LabelFrame(self.root, text="Folders")
        folder_frame.pack(fill="x", **pad)
        self._folder_row(folder_frame, "Source folder:", self.source_dir, self._pick_source)
        self._folder_row(folder_frame, "Output folder:", self.output_dir, self._pick_output)

        # Settings
        settings_frame = ttk.LabelFrame(self.root, text="Settings")
        settings_frame.pack(fill="x", **pad)

        res_row = ttk.Frame(settings_frame)
        res_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(res_row, text="Resolution:").pack(side="left")
        ttk.Radiobutton(res_row, text="Full", variable=self.half_res, value=False).pack(side="left", padx=4)
        ttk.Radiobutton(res_row, text="Half", variable=self.half_res, value=True).pack(side="left", padx=4)

        self.codec_row = ttk.Frame(settings_frame)
        self.codec_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(self.codec_row, text="Output codec:").pack(side="left")
        self.codec_combo = ttk.Combobox(self.codec_row, textvariable=self.codec,
                                         values=list(CODEC_PROFILES.keys()), state="readonly", width=20)
        self.codec_combo.pack(side="left", padx=4)

        self.seq_out_row = ttk.Frame(settings_frame)
        self.to_seq_check = ttk.Checkbutton(self.seq_out_row, text="Output as image sequence instead of video",
                                             variable=self.to_sequence, command=self._on_to_seq_change)
        self.to_seq_check.pack(side="left")
        self.img_fmt_combo = ttk.Combobox(self.seq_out_row, textvariable=self.image_format,
                                           values=list(IMAGE_SEQ_PROFILES.keys()), state="readonly", width=10)
        self.img_fmt_combo.pack(side="left", padx=8)
        self.img_fmt_combo.configure(state="disabled")

        fallback_row = ttk.Frame(settings_frame)
        fallback_row.pack(fill="x", padx=8, pady=2)
        ttk.Label(fallback_row, text="Fallback frame rate (used only if not found in source metadata):").pack(side="left")
        ttk.Entry(fallback_row, textvariable=self.fallback_fps, width=6).pack(side="left", padx=4)
        ttk.Label(fallback_row, text="Fallback timecode:").pack(side="left", padx=(12, 0))
        ttk.Entry(fallback_row, textvariable=self.fallback_tc, width=12).pack(side="left", padx=4)

        # Controls
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill="x", **pad)
        self.scan_btn = ttk.Button(ctrl_frame, text="Scan Folder", command=self.on_scan)
        self.scan_btn.pack(side="left", padx=4)
        self.start_btn = ttk.Button(ctrl_frame, text="Start", command=self.on_start, state="disabled")
        self.start_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(ctrl_frame, text="Pause", command=self.on_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctrl_frame, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        self.job_count_label = ttk.Label(ctrl_frame, text="No jobs scanned yet.")
        self.job_count_label.pack(side="left", padx=16)

        # Detected items table: name, format, frame count, resolution, size, status
        table_frame = ttk.LabelFrame(self.root, text="Detected Sequences / Files")
        table_frame.pack(fill="both", **pad)
        columns = ("format", "frames", "resolution", "size", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="tree headings", height=6)
        self.tree.heading("#0", text="Name")
        self.tree.heading("format", text="Format")
        self.tree.heading("frames", text="Frames")
        self.tree.heading("resolution", text="Resolution")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=280)
        self.tree.column("format", width=70, anchor="center")
        self.tree.column("frames", width=70, anchor="center")
        self.tree.column("resolution", width=100, anchor="center")
        self.tree.column("size", width=90, anchor="center")
        self.tree.column("status", width=100, anchor="center")
        tree_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        self.summary_label = ttk.Label(self.root, text="")
        self.summary_label.pack(fill="x", padx=8)

        # Progress
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=(4, 0))
        self.time_label = ttk.Label(self.root, text="")
        self.time_label.pack(fill="x", padx=8, pady=(0, 4))

        # Log
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_widget = scrolledtext.ScrolledText(log_frame, height=18, state="disabled", wrap="word")
        self.log_widget.pack(fill="both", expand=True)

        self._on_mode_change()

    def _folder_row(self, parent, label, var, cmd):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=2)
        ttk.Label(row, text=label, width=14).pack(side="left")
        ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="Browse...", command=cmd).pack(side="left")

    # -- UI callbacks --------------------------------------------------------

    def _on_mode_change(self):
        if self.mode.get() == "sequence":
            self.codec_row.pack(fill="x", padx=8, pady=2)
            self.seq_out_row.pack_forget()
            self.to_sequence.set(False)
        else:
            self.seq_out_row.pack(fill="x", padx=8, pady=2)
            self._on_to_seq_change()

    def _on_to_seq_change(self):
        if self.to_sequence.get():
            self.codec_row.pack_forget()
            self.img_fmt_combo.configure(state="readonly")
        else:
            self.codec_row.pack(fill="x", padx=8, pady=2)
            self.img_fmt_combo.configure(state="disabled")

    def _pick_source(self):
        initial = read_default_dir() or os.path.expanduser("~")
        d = filedialog.askdirectory(title="Choose source folder", initialdir=initial)
        if d:
            self.source_dir.set(d)
            write_default_dir(d)
            if not self.output_dir.get():
                self.output_dir.set(str(Path(d) / "Proxy"))

    def _pick_output(self):
        # Deliberately does NOT write back to ~/.default_dir -- only the
        # source picker updates it.
        initial = read_default_dir() or os.path.expanduser("~")
        d = filedialog.askdirectory(title="Choose output folder", initialdir=initial)
        if d:
            self.output_dir.set(d)

    def log(self, msg):
        def _append():
            self.log_widget.configure(state="normal")
            self.log_widget.insert("end", msg + "\n")
            self.log_widget.see("end")
            self.log_widget.configure(state="disabled")
        self.root.after(0, _append)

    def set_progress(self, frames_done, frames_total, elapsed, eta):
        def _upd():
            self.progress["maximum"] = max(frames_total, 1)
            self.progress["value"] = frames_done
            pct = (frames_done / frames_total * 100) if frames_total else 0
            self.time_label.configure(
                text=(f"{frames_done}/{frames_total} frames ({pct:.1f}%)  |  "
                      f"Elapsed: {human_time(elapsed)}  |  ETA: {human_time(eta)}")
            )
        self.root.after(0, _upd)

    def set_job_status(self, job_key, status):
        def _upd():
            if self.tree.exists(job_key):
                self.tree.set(job_key, "status", status)
        self.root.after(0, _upd)

    # -- Actions -------------------------------------------------------------

    def on_scan(self):
        src = self.source_dir.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("Error", "Please choose a valid source folder.")
            return
        if not self.output_dir.get().strip():
            self.output_dir.set(str(Path(src) / "Proxy"))

        self.tree.delete(*self.tree.get_children())
        self.log(f"Scanning {src} ...")
        self.jobs = scan_folder(src, self.mode.get())
        n = len(self.jobs)
        if n == 0:
            self.log("No matching files found.")
            self.job_count_label.configure(text="No jobs found.")
            self.summary_label.configure(text="")
            self.start_btn.configure(state="disabled")
            return

        self.log("Reading metadata (frame count, format, resolution, size) ...")
        total_frames = 0
        total_bytes = 0
        for j in self.jobs:
            info = build_job_info(j)
            j.total_frames = info["frames"]
            total_frames += info["frames"]
            total_bytes += info["size_bytes"] or 0

            label = j.output_name
            if isinstance(j, SequenceJob) and j.missing_frames:
                label += f"  (missing {len(j.missing_frames)} frames!)"

            self.tree.insert("", "end", iid=j.key, text=label, values=(
                info["format"], info["frames"], info["resolution"],
                human_size(info["size_bytes"]), "Pending",
            ))

        self.job_count_label.configure(text=f"{n} job(s) found.")
        self.summary_label.configure(
            text=f"Total: {n} item(s), {total_frames} frames, {human_size(total_bytes)} on disk."
        )
        self.start_btn.configure(state="normal")

        progress_path = Path(self.output_dir.get()) / PROGRESS_FILENAME
        Path(self.output_dir.get()).mkdir(parents=True, exist_ok=True)
        self.store = ProgressStore(progress_path)
        already_done = 0
        already_done_frames = 0
        for j in self.jobs:
            if self.store.status(j.key) == "done":
                already_done += 1
                already_done_frames += j.total_frames
                self.tree.set(j.key, "status", "Done")
        if already_done:
            self.log(f"Found existing progress file: {already_done} job(s) already completed, will be skipped.")
        self.set_progress(already_done_frames, total_frames, 0, None)

    def on_start(self):
        if not self.jobs:
            return
        settings = {
            "mode": self.mode.get(),
            "to_sequence": self.to_sequence.get(),
            "half_res": self.half_res.get(),
            "codec": self.codec.get(),
            "image_format": self.image_format.get(),
            "output_dir": self.output_dir.get(),
            "fallback_fps": self._safe_float(self.fallback_fps.get(), 24.0),
            "fallback_tc": self.fallback_tc.get().strip() or None,
        }
        self.controller = ProcessController(self.log, self.set_progress, self.set_job_status)
        self.controller.start(self.jobs, settings, self.store)
        self.start_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="Pause")
        self.stop_btn.configure(state="normal")
        self._poll_running()

    def _poll_running(self):
        if self.controller and self.controller.running:
            self.root.after(500, self._poll_running)
        else:
            self.pause_btn.configure(state="disabled")
            self.stop_btn.configure(state="disabled")
            self.scan_btn.configure(state="normal")
            self.start_btn.configure(state="normal")

    def on_pause(self):
        if not self.controller:
            return
        if self.pause_btn["text"] == "Pause":
            self.controller.pause()
            self.pause_btn.configure(text="Resume")
        else:
            self.controller.resume()
            self.pause_btn.configure(text="Pause")

    def on_stop(self):
        if self.controller:
            self.controller.stop()
        self.stop_btn.configure(state="disabled")

    @staticmethod
    def _safe_float(val, default):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default


def main():
    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg was not found on PATH. Please install ffmpeg first.")
        sys.exit(1)
    root = tk.Tk()
    ProxyGenApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
