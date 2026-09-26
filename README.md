# ProxyGen

A Tkinter GUI tool that scans a folder tree for DPX/EXR/TIFF/JPG/PNG image
sequences (or video files) and transcodes them into DaVinci Resolve-compatible
proxy media using ffmpeg — preserving source timecode and frame rate from
metadata wherever present, with **no color space conversion or LUT applied**
(color is passed through unchanged so Resolve's color management handles the
proxy identically to the original camera/scan files).

## What it does

- **Sequence discovery**: recursively walks a source folder, groups numbered
  frames into sequences (e.g. `shot_0259207.dpx` → `shot_` + frame `0259207`),
  and detects missing frames in a range.
- **Metadata-aware**: for DPX, reads timecode and frame rate directly from the
  binary header (SMPTE 268M — Television Header preferred, falls back to the
  Motion Picture Film Header). For other sequence formats and video files,
  tries `ffprobe`. Falls back to user-specified defaults when nothing is
  found.
- **Two modes**:
  1. *Image Sequence → Proxy Video* — DPX/EXR/TIFF/JPG/PNG sequences encoded
     to ProRes Proxy, DNxHR LB, H.264 (8-bit), or H.265 (10-bit) `.mov` files.
  2. *Downres Video File* — an existing video file downscaled to a smaller
     video (same 4 codec choices) or exported back out as an EXR/TIFF/JPG/PNG
     image sequence.
- **Resolution**: full or half, selectable per run.
- **Resolve-matching output names**: strips the frame-number suffix and
  trailing separator so the proxy clip name matches what Resolve assigns on
  import (confirmed against a real test import — e.g.
  `my_film-01-508-A_0259207.dpx` → `my_film-01-508-A.mov`).
- **Status table**: after scanning, shows every detected item — name, format,
  frame count, resolution, size on disk — with a live status column
  (Pending/Processing/Done/Error/Stopped) as the batch runs.
- **Frame-weighted progress bar + ETA**: tracks actual frames encoded across
  the whole batch (parsed from ffmpeg's own `-stats` output), with elapsed
  time and an extrapolated time-remaining estimate.
- **Pause / Resume / Stop**: a `.proxygen_progress.json` file is written into
  the output folder after each completed job. Closing the app mid-batch is
  safe — relaunching, scanning the same source/output pair, and hitting Start
  again skips everything already marked done and resumes from there.
- **Remembers the last source folder**: the source-folder picker reads/writes
  `~/.default_dir` (same convention used by `dpx_scene_detect_gui.py`) so it
  reopens where you left off. The output-folder picker reads that same file
  for convenience but never writes to it.

## Dependencies

- **Python 3.8+**
- **ffmpeg** and **ffprobe** on `PATH`, built with `prores_ks`, `dnxhd`,
  `libx264`, and `libx265` support (standard in most distro/official builds).
- **tkinter** — bundled with most Python installs; on Linux Mint / Debian /
  Ubuntu it's a separate package if missing:
  ```
  sudo apt install python3-tk
  ```

No third-party Python packages are required — everything (DPX header
parsing, sequence grouping, the GUI) is standard library.

## Usage

```
python3 proxygen.py
```

1. Choose **Mode**: encode an image sequence to proxy video, or downres an
   existing video file.
2. Pick the **source folder** (defaults to the last folder you used) and an
   **output folder** (defaults to `<source>/Proxy`).
3. Set **Resolution** (Full/Half), the **output codec** (or, in video mode,
   check "Output as image sequence instead of video" and pick a format).
4. Set the **fallback frame rate / timecode** — only used when a file's own
   metadata doesn't have them.
5. Click **Scan Folder** — review the detected items table and totals before
   committing to a batch.
6. Click **Start**. Use **Pause/Resume** or **Stop** as needed; Stop is safe
   at any point and the next Start on the same output folder picks up where
   it left off.

## Known limitations / things to sanity-check on your own footage

- **DNxHR LB** requires frame dimensions divisible by 8; the script rounds
  down automatically, which can trim a couple of pixels off odd-width
  sources.
- Sequences with **missing frames** are flagged in the status table but not
  auto-repaired — ffmpeg's `image2` demuxer expects contiguous frame
  numbers, so gaps will need handling before a clean run.
- Frame rate / timecode detection is **best-effort** for non-DPX sequence
  formats (EXR/TIFF/JPG/PNG rarely embed it reliably) — always confirm the
  fallback values are correct for that batch before starting.
- Before a large batch, it's worth test-importing one output file into
  Resolve's Link Proxy Media to confirm auto-matching still behaves as
  expected for your specific naming pattern.
- Throughput is often **storage-bound** rather than CPU/GPU-bound,
  especially reading large uncompressed DPX off spinning disks — see the
  in-app performance notes/conversation history for details if fps drops
  over a long batch.
