import os
import sys
import threading
import subprocess
import shutil
import json
import time
from pathlib import Path
import re
import tempfile
import tkinter as tk
import winsound
from tkinter import ttk, filedialog, messagebox

"""
Shotcut Auto-Cut GUI (Tkinter)
--------------------------------
This GUI lets you automatically cut out low-volume (silent) parts of videos.
It supports two backends:

1. Auto-Editor (recommended)
   - https://auto-editor.com
   - Detects and removes silence intelligently.
   - Allows margins and smart clip detection.

2. FFmpeg “silenceremove” (simple fallback)
   - Requires ffmpeg in PATH.
   - Removes low-volume parts directly from audio stream.

New in this version:
- Folder (batch) mode: process all videos in a folder (non-recursive)
- Skips files that already contain "_autocut" in the name
- After batch, builds <foldername>_ALL_autocut.mp4 by concatenating outputs in numeric order
"""

APP_TITLE = "Shotcut Auto-Cut GUI (Silence/Low-Volume)"

# Frame extraction: save 1 out of every N frames (N=30 by default)
FRAME_EXTRACT_INTERVAL = 30

DEFAULTS = {
    "backend": "Auto-Editor",
    "threshold_db": -30.0,
    "min_silence": 1.35,
    "margin": 0.5,
    "crossfade": 0.0,    # <--- NEW
    "audio_track": 0,
    "min_clip_len": 0.58,
    "ffmpeg_path": "",
    "extract_frames": False,
    "frames_folder": "",
}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".wmv", ".flv"}


def which(program: str):
    """Cross-platform which wrapper."""
    return shutil.which(program)


def run_subprocess(cmd_list, log_callback, cwd=None, *, stop_event=None, proc_setter=None):
    """
    Run a subprocess and stream stdout/stderr to log_callback.

    - stop_event: threading.Event (optional). If set, the process will be terminated.
    - proc_setter: callable(proc|None) (optional). Lets the GUI store the active Popen handle.
    """
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        # Prevent flashing console windows on Windows
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",  # prevent UnicodeDecodeError on Windows
            bufsize=1,
            universal_newlines=True,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        if proc_setter:
            try:
                proc_setter(proc)
            except Exception:
                pass

        # Stream output
        assert proc.stdout is not None
        for line in proc.stdout:
            if stop_event is not None and stop_event.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            log_callback(line.rstrip())

        proc.wait()
        return proc.returncode
    except FileNotFoundError as e:
        log_callback(f"ERROR: {e}")
        return 127
    except Exception as e:
        log_callback(f"ERROR running subprocess: {e}")
        return 1
    finally:
        if proc_setter:
            try:
                proc_setter(None)
            except Exception:
                pass


# --------------------------
# Auto-Editor backend
# --------------------------

class AutoEditorBackend:
    """Wrapper for Auto-Editor CLI (new v24+ syntax)"""

    def __init__(self, settings, log, stop_event=None, proc_setter=None):
        self.settings = settings
        self.log = log
        self.stop_event = stop_event
        self.proc_setter = proc_setter

    def ensure_installed(self) -> bool:
        exe = which("auto-editor")
        if exe:
            self.log(f"Found auto-editor at: {exe}")
            return True
        self.log("auto-editor not found. Attempting installation via pip...")
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "auto-editor"]
        rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        if rc == 0 and which("auto-editor"):
            self.log("auto-editor installed successfully.")
            return True
        self.log("ERROR: Unable to install or locate auto-editor.")
        return False

    def build_cmd(self, input_path: Path, output_path: Path):
        s = self.settings

        # Gather settings
        threshold_db = float(s["threshold_db"])
        margin = float(s["margin"])
        crossfade = float(s.get("crossfade", 0.0))

        edit_arg = f"audio:threshold={threshold_db}dB"

        cmd = [
            "auto-editor",
            str(input_path),
            "--edit", edit_arg,
            "--margin", f"{margin}sec",
            "--output", str(output_path),
            "--no_open"
        ]

        # Add crossfade if requested
        if crossfade > 0:
            cmd.extend(["--add-transition", f"fade:{crossfade}sec"])

        return cmd

    def run(self, input_path: Path, output_path: Path):
        if not self.ensure_installed():
            return 1

        cmd = self.build_cmd(input_path, output_path)
        self.log("Running: " + " ".join(cmd))
        return run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)

# --------------------------
# FFmpeg backend
# --------------------------

class FFmpegBackend:
    """
    FFmpeg backend with real crossfade support.

    Pipeline:
      1) Detect silence with silencedetect
      2) Convert silence intervals -> kept intervals (non-silent), applying margin and min_clip_len
      3) Cut kept intervals into temp segment files (re-encoded for accurate cuts)
      4) Join:
          - crossfade == 0: concat demuxer (hard cuts)
          - crossfade  > 0: xfade + acrossfade chain (true A/V crossfades)
    """

    def __init__(self, settings, log, stop_event=None, proc_setter=None):
        self.settings = settings
        self.log = log
        self.stop_event = stop_event
        self.proc_setter = proc_setter

    def get_ffmpeg(self) -> str:
        user_path = (self.settings.get("ffmpeg_path", "") or "").strip()
        if user_path:
            return user_path
        exe = which("ffmpeg")
        if exe:
            return exe
        return "ffmpeg"

    def get_ffprobe(self) -> str:
        ffmpeg = self.get_ffmpeg()
        try:
            p = Path(ffmpeg)
            # If user provided an absolute path, prefer sibling ffprobe(.exe)
            if p.suffix.lower() == ".exe" or p.name.lower().startswith("ffmpeg"):
                cand = p.with_name("ffprobe.exe" if p.suffix.lower() == ".exe" else "ffprobe")
                if cand.exists():
                    return str(cand)
        except Exception:
            pass
        exe = which("ffprobe")
        if exe:
            return exe
        return "ffprobe"

    def _check_cancelled(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise RuntimeError("Cancelled by user.")

    def _probe_duration(self, video: Path) -> float:
        ffprobe = self.get_ffprobe()
        cmd = [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video)
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               creationflags=creationflags)
            return float((r.stdout or "").strip() or 0.0)
        except Exception:
            return 0.0

    def _detect_silence(self, input_path: Path, threshold_db: float, min_silence: float):
        """
        Returns a list of (silence_start, silence_end) in seconds.
        silence_end may be None if silence runs to EOF (we'll fix up using duration).
        """
        ffmpeg = self.get_ffmpeg()
        # silencedetect prints to stderr normally; we redirect to stdout in run_subprocess,
        # so we just parse lines in the log sink.
        lines = []

        def _cap(line: str):
            lines.append(line)
            # also forward to GUI
            self.log(line)

        cmd = [
            ffmpeg, "-hide_banner", "-nostats", "-i", str(input_path),
            "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
            "-f", "null", "-"
        ]
        self.log("Detecting silence via FFmpeg silencedetect...")
        rc = run_subprocess(cmd, _cap, stop_event=self.stop_event, proc_setter=self.proc_setter)
        if rc != 0:
            raise RuntimeError(f"FFmpeg silencedetect failed (exit {rc}).")

        silences = []
        start = None
        for line in lines:
            m1 = re.search(r"silence_start:\s*([0-9.]+)", line)
            if m1:
                start = float(m1.group(1))
                continue
            m2 = re.search(r"silence_end:\s*([0-9.]+)", line)
            if m2 and start is not None:
                end = float(m2.group(1))
                silences.append((start, end))
                start = None

        if start is not None:
            silences.append((start, None))

        return silences

    def _invert_to_kept(self, duration: float, silences, margin: float, min_clip_len: float):
        """
        Convert silence intervals -> kept (non-silent) intervals.
        Apply margin by expanding each silence interval by +/- margin.
        """
        if duration <= 0:
            # best-effort
            return [(0.0, None)]

        # Expand silence intervals by margin, then clamp
        expanded = []
        for s0, s1 in silences:
            a = max(0.0, float(s0) - margin)
            b = None if s1 is None else min(duration, float(s1) + margin)
            expanded.append((a, b))

        # Merge overlapping expanded silences
        expanded.sort(key=lambda x: x[0])
        merged = []
        for a, b in expanded:
            if not merged:
                merged.append([a, b])
                continue
            pa, pb = merged[-1]
            pb_val = duration if pb is None else pb
            b_val = duration if b is None else b
            if a <= pb_val:
                # overlap/adjacent
                merged[-1][1] = None if (pb is None or b is None) else max(pb, b)
            else:
                merged.append([a, b])

        # Invert
        kept = []
        cur = 0.0
        for a, b in merged:
            end_a = a
            if end_a - cur >= min_clip_len:
                kept.append((cur, end_a))
            cur = duration if b is None else b
        if duration - cur >= min_clip_len:
            kept.append((cur, duration))

        return kept

    def _cut_segment(self, input_path: Path, out_path: Path, start: float, end: float):
        self._check_cancelled()
        ffmpeg = self.get_ffmpeg()

        # Accurate cuts: re-encode. Keep it reasonably fast + compatible.
        cmd = [
            ffmpeg, "-hide_banner", "-y",
            "-ss", f"{start:.6f}", "-to", f"{end:.6f}",
            "-i", str(input_path),
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            str(out_path)
        ]
        self.log(f"Cut segment: {start:.2f}s → {end:.2f}s")
        rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        if rc != 0:
            raise RuntimeError(f"FFmpeg segment cut failed (exit {rc}).")

    def _concat_hard(self, segments, output_path: Path):
        self._check_cancelled()
        ffmpeg = self.get_ffmpeg()
        list_file = output_path.with_suffix(".concat.txt")
        with list_file.open("w", encoding="utf-8") as f:
            for seg in segments:
                # concat demuxer needs: file 'path'
                path_str = str(seg).replace("'", "'\\''")
                f.write("file '" + path_str + "'\n")

        cmd = [
            ffmpeg, "-hide_banner", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(output_path)
        ]
        self.log("Concatenating (hard cuts)...")
        rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        if rc != 0:
            # Fallback: re-encode concat (more compatible)
            self.log("Hard concat with -c copy failed; retrying with re-encode...")
            cmd = [
                ffmpeg, "-hide_banner", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                str(output_path)
            ]
            rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        try:
            list_file.unlink(missing_ok=True)
        except Exception:
            pass
        return rc

    def _concat_crossfade(self, segments, durations, crossfade: float, output_path: Path):
        """
        Build a chained xfade/acrossfade graph.
        """
        self._check_cancelled()
        ffmpeg = self.get_ffmpeg()
        n = len(segments)
        if n == 1:
            shutil.copy2(segments[0], output_path)
            return 0

        # Build filter_complex
        fc_lines = []
        # Labels for each input stream
        # [0:v][0:a], [1:v][1:a], etc.

        # Chain video xfade
        cum = durations[0]
        v_label = "[0:v]"
        a_label = "[0:a]"
        for i in range(1, n):
            in_v = f"[{i}:v]"
            in_a = f"[{i}:a]"
            out_v = f"[v{i}]"
            out_a = f"[a{i}]"
            offset = max(0.0, cum - crossfade)
            fc_lines.append(f"{v_label}{in_v}xfade=transition=fade:duration={crossfade}:offset={offset}{out_v}")
            fc_lines.append(f"{a_label}{in_a}acrossfade=d={crossfade}{out_a}")
            cum = cum + durations[i] - crossfade
            v_label = out_v
            a_label = out_a

        filter_complex = ";".join(fc_lines)

        cmd = [ffmpeg, "-hide_banner", "-y"]
        for seg in segments:
            cmd.extend(["-i", str(seg)])

        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", v_label,
            "-map", a_label,
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path)
        ])

        self.log("Concatenating with true crossfade...")
        rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        return rc

    def run(self, input_path: Path, output_path: Path):
        s = self.settings
        threshold_db = float(s["threshold_db"])
        min_silence = float(s["min_silence"])
        margin = float(s.get("margin", 0.0))
        crossfade = float(s.get("crossfade", 0.0))
        min_clip_len = float(s.get("min_clip_len", 0.0))

        # If user asks for crossfade but margin/min_clip_len are zero-ish, still do it.
        duration = self._probe_duration(input_path)
        self.log(f"Duration: {duration:.2f}s")

        silences = self._detect_silence(input_path, threshold_db=threshold_db, min_silence=min_silence)
        self.log(f"Detected {len(silences)} silence interval(s).")

        kept = self._invert_to_kept(duration, silences, margin=margin, min_clip_len=min_clip_len)
        self.log(f"Keeping {len(kept)} non-silent interval(s).")
        if not kept:
            self.log("Nothing to keep (everything considered silence).")
            return 1

        # Build temp segments
        tmpdir = Path(tempfile.mkdtemp(prefix="silencer_segments_"))
        segments = []
        durations = []
        try:
            for i, (a, b) in enumerate(kept):
                self._check_cancelled()
                if b is None:
                    b = duration
                if b <= a:
                    continue
                seg = tmpdir / f"seg_{i:04d}.mp4"
                self._cut_segment(input_path, seg, a, b)
                segments.append(seg)
                durations.append(b - a)

            if not segments:
                self.log("No segments produced after filtering.")
                return 1

            if crossfade <= 0:
                rc = self._concat_hard(segments, output_path)
            else:
                # Prevent enormous graphs from blowing up on extreme segment counts.
                if len(segments) > 120:
                    self.log(f"Too many segments ({len(segments)}). Falling back to hard cuts.")
                    rc = self._concat_hard(segments, output_path)
                else:
                    rc = self._concat_crossfade(segments, durations, crossfade, output_path)

            return rc
        finally:
            # Best-effort cleanup
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


# --------------------------
# Helpers
# --------------------------

_number_re = re.compile(r"(\d+)")

def natural_key(p: Path):
    """
    Numeric-aware sort: "2.mp4" comes before "10.mp4".
    Works on the file's stem.
    """
    s = p.stem
    parts = _number_re.split(s)
    return [int(t) if t.isdigit() else t.lower() for t in parts]


def add_autocut_suffix(p: Path) -> Path:
    out = p.with_stem(p.stem + "_autocut")
    if out.suffix.lower() not in (".mp4", ".mov", ".mkv", ".m4v"):
        out = out.with_suffix(".mp4")
    return out


def is_video_file(p: Path) -> bool:
    return p.suffix.lower() in VIDEO_EXTS


# --------------------------
# GUI
# --------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("940x680")
        self.minsize(860, 620)

        self.settings = DEFAULTS.copy()

        # Inputs
        self.input_path = tk.StringVar()     # file or folder
        self.output_path = tk.StringVar()    # single-file mode target
        self.backend = tk.StringVar(value=self.settings["backend"])
        self.threshold_db = tk.DoubleVar(value=self.settings["threshold_db"])
        self.min_silence = tk.DoubleVar(value=self.settings["min_silence"])
        self.margin = tk.DoubleVar(value=self.settings["margin"])
        self.audio_track = tk.IntVar(value=self.settings["audio_track"])
        self.min_clip_len = tk.DoubleVar(value=self.settings["min_clip_len"])
        self.ffmpeg_path = tk.StringVar(value=self.settings["ffmpeg_path"])
        self.open_in_shotcut = tk.BooleanVar(value=False)
        self.shotcut_path = tk.StringVar(value="")
        self.extract_frames = tk.BooleanVar(value=False)
        self.frames_folder = tk.StringVar(value="")

        self.threshold_db = tk.DoubleVar(value=self.settings["threshold_db"])
        self.min_silence = tk.DoubleVar(value=self.settings["min_silence"])
        self.margin = tk.DoubleVar(value=self.settings["margin"])
        self.crossfade = tk.DoubleVar(value=self.settings["crossfade"]) # <--- NEW
        self.audio_track = tk.IntVar(value=self.settings["audio_track"])
        self.min_clip_len = tk.DoubleVar(value=self.settings["min_clip_len"])

        self.running = False
        self.worker_thread = None

        self.stop_event = threading.Event()
        self._active_proc = None

        self._build_ui()
        self._toggle_frames_controls()

    def log(self, msg: str):
        self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)
        self.update_idletasks()


    def _set_active_proc(self, proc):
        """Store currently running subprocess for Stop button to terminate."""
        self._active_proc = proc

    def on_stop(self):
        """Stop current operation (best-effort)."""
        try:
            self.stop_event.set()
            proc = self._active_proc
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self.log("⛔ Stop requested.")
        except Exception:
            pass

    # ---------- Browsers ----------
    def browse_input_file(self):
        path = filedialog.askopenfilename(
            title="Choose input video",
            filetypes=[("Video files", ".mp4 .mov .mkv .avi .m4v .webm .wmv .flv"), ("All files", "*.*")]
        )
        if path:
            self.input_path.set(path)
            in_p = Path(path)
            out = add_autocut_suffix(in_p)
            self.output_path.set(str(out))

    def browse_input_folder(self):
        path = filedialog.askdirectory(title="Choose input folder (batch mode)")
        if path:
            self.input_path.set(path)
            # In folder mode, we don't set output_path; each file gets its own, plus a final combined output.

    def browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save output as", defaultextension=".mp4",
            filetypes=[("MP4 video", ".mp4"), ("All files", "*.*")]
        )
        if path:
            self.output_path.set(path)

    def browse_ffmpeg(self):
        path = filedialog.askopenfilename(title="Path to ffmpeg executable")
        if path:
            self.ffmpeg_path.set(path)

    def browse_shotcut(self):
        path = filedialog.askopenfilename(title="Path to Shotcut executable")
        if path:
            self.shotcut_path.set(path)

    def browse_frames_folder(self):
        path = filedialog.askdirectory(title="Choose folder for extracted frames")
        if path:
            self.frames_folder.set(path)

    def _toggle_frames_controls(self):
        enabled = self.extract_frames.get()
        state = "normal" if enabled else "disabled"
        try:
            self.ent_frames_folder.configure(state=state)
            self.btn_frames_browse.configure(state=state)
        except Exception:
            pass

    def _toggle_shotcut_path(self):
        enabled = self.open_in_shotcut.get()
        state = "normal" if enabled else "disabled"
        frm_open = self.winfo_children()[2]
        entries = frm_open.winfo_children()
        entries[1].configure(state=state)
        entries[2].configure(state=state)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        frm_top = ttk.LabelFrame(self, text="Input/Output")
        frm_top.pack(fill=tk.X, **pad)
        ttk.Label(frm_top, text="Input (file OR folder):").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(frm_top, textvariable=self.input_path, width=72).grid(row=0, column=1, columnspan=2, sticky=tk.W)
        ttk.Button(frm_top, text="Browse File", command=self.browse_input_file).grid(row=0, column=3)
        ttk.Button(frm_top, text="Browse Folder", command=self.browse_input_folder).grid(row=0, column=4)

        ttk.Label(frm_top, text="Output file (single-file mode):").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(frm_top, textvariable=self.output_path, width=72).grid(row=1, column=1, columnspan=2, sticky=tk.W)
        ttk.Button(frm_top, text="Browse", command=self.browse_output).grid(row=1, column=3)

        # Frame extraction (optional)
        ttk.Label(frm_top, text="Extract 1 in every 30 frames:").grid(row=2, column=0, sticky=tk.W)
        ttk.Checkbutton(
            frm_top,
            text="Enable",
            variable=self.extract_frames,
            command=self._toggle_frames_controls
        ).grid(row=2, column=1, sticky=tk.W)
        self.ent_frames_folder = ttk.Entry(frm_top, textvariable=self.frames_folder, width=56, state="disabled")
        self.ent_frames_folder.grid(row=2, column=2, columnspan=2, sticky=tk.W)
        self.btn_frames_browse = ttk.Button(frm_top, text="Frames Folder", command=self.browse_frames_folder, state="disabled")
        self.btn_frames_browse.grid(row=2, column=4)

        ttk.Label(frm_top, text="Backend:").grid(row=3, column=0, sticky=tk.W)
        ttk.Combobox(frm_top, textvariable=self.backend, values=["Auto-Editor", "FFmpeg"],
                     state="readonly", width=18).grid(row=3, column=1, sticky=tk.W)

        frm_params = ttk.LabelFrame(self, text="Parameters")
        frm_params.pack(fill=tk.X, **pad)

        # Row 0
        ttk.Label(frm_params, text="Threshold (dBFS)").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(frm_params, textvariable=self.threshold_db, width=10).grid(row=0, column=1)
        ttk.Label(frm_params, text="Min silence (s)").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(frm_params, textvariable=self.min_silence, width=10).grid(row=0, column=3)

        # Row 1
        ttk.Label(frm_params, text="Margin (s)").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(frm_params, textvariable=self.margin, width=10).grid(row=1, column=1)
        ttk.Label(frm_params, text="Crossfade (s)").grid(row=1, column=2, sticky=tk.W) # <--- NEW
        ttk.Entry(frm_params, textvariable=self.crossfade, width=10).grid(row=1, column=3)

        # Row 2
        ttk.Label(frm_params, text="Min kept clip (s)").grid(row=2, column=0, sticky=tk.W)
        ttk.Entry(frm_params, textvariable=self.min_clip_len, width=10).grid(row=2, column=1)
        ttk.Label(frm_params, text="Audio track").grid(row=2, column=2, sticky=tk.W)
        ttk.Entry(frm_params, textvariable=self.audio_track, width=10).grid(row=2, column=3)

        # Row 3
        ttk.Label(frm_params, text="FFmpeg path (opt)").grid(row=3, column=0, sticky=tk.W)
        ttk.Entry(frm_params, textvariable=self.ffmpeg_path, width=38).grid(row=3, column=1, columnspan=3, sticky=tk.W)
        ttk.Button(frm_params, text="Browse", command=self.browse_ffmpeg).grid(row=3, column=4, padx=4)

        frm_open = ttk.Frame(self)
        frm_open.pack(fill=tk.X, **pad)
        ttk.Checkbutton(frm_open, text="Open output in Shotcut on finish",
                        variable=self.open_in_shotcut,
                        command=self._toggle_shotcut_path).pack(side=tk.LEFT)
        ttk.Entry(frm_open, textvariable=self.shotcut_path, width=50, state="disabled").pack(side=tk.LEFT, padx=6)
        ttk.Button(frm_open, text="Browse", command=self.browse_shotcut, state="disabled").pack(side=tk.LEFT)

        frm_btns = ttk.Frame(self)
        frm_btns.pack(fill=tk.X, **pad)
        ttk.Button(frm_btns, text="Run", command=self.on_run).pack(side=tk.LEFT)
        ttk.Button(frm_btns, text="Stop", command=self.on_stop).pack(side=tk.LEFT, padx=6)
        ttk.Button(frm_btns, text="Install/Update Auto-Editor", command=self.install_autoeditor).pack(side=tk.LEFT)

        frm_log = ttk.LabelFrame(self, text="Log")
        frm_log.pack(fill=tk.BOTH, expand=True, **pad)
        self.txt_log = tk.Text(frm_log, height=18, wrap="word")
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    # ---------- Run / Workers ----------
    def on_run(self):
        if self.running:
            messagebox.showinfo("Busy", "Already running.")
            return

        in_path = Path(self.input_path.get().strip())
        if not in_path.exists():
            messagebox.showerror("Missing input", "Please choose a valid input file or folder.")
            return

        # Validate single-file output path only in file mode
        if in_path.is_file():
            out_path = Path(self.output_path.get().strip()) if self.output_path.get().strip() else add_autocut_suffix(in_path)
            if not out_path.parent.exists():
                messagebox.showerror("Invalid output", f"Output folder doesn't exist: {out_path.parent}")
                return

        self.settings.update({
            "backend": self.backend.get(),
            "threshold_db": float(self.threshold_db.get()),
            "min_silence": float(self.min_silence.get()),
            "margin": float(self.margin.get()),
            "crossfade": float(self.crossfade.get()),  # <--- NEW
            "audio_track": int(self.audio_track.get()),
            "min_clip_len": float(self.min_clip_len.get()),
            "ffmpeg_path": self.ffmpeg_path.get().strip(),
            "extract_frames": bool(self.extract_frames.get()),
            "frames_folder": self.frames_folder.get().strip(),
        })

        self.running = True

        self.stop_event.clear()
        self.running = True
        self.txt_log.delete("1.0", tk.END)
        self.log("Starting...")

        if in_path.is_dir():
            self.worker_thread = threading.Thread(target=self._batch_worker, args=(in_path,), daemon=True)
            self.worker_thread.start()
        else:
            self.worker_thread = threading.Thread(target=self._single_worker, args=(in_path,), daemon=True)
            self.worker_thread.start()

    def _make_backend(self):
        if self.settings["backend"] == "Auto-Editor":
            return AutoEditorBackend(self.settings, self.log, stop_event=self.stop_event, proc_setter=self._set_active_proc)
        return FFmpegBackend(self.settings, self.log, stop_event=self.stop_event, proc_setter=self._set_active_proc)

    def _single_worker(self, in_path: Path):
        try:
            start = time.time()
            out_path = Path(self.output_path.get().strip()) if self.output_path.get().strip() else add_autocut_suffix(in_path)
            backend = self._make_backend()
            rc = backend.run(in_path, out_path)
            dur = time.time() - start
            if rc == 0:
                self.log(f"\n✅ Done in {dur:.1f}s → {out_path}")
                self.play_done_sound()
                if self.settings.get("extract_frames"):
                    frames_root = self._resolve_frames_root(in_path, out_path)
                    self._extract_sampled_frames(out_path, frames_root, interval=FRAME_EXTRACT_INTERVAL)
                self.open_in_shotcut_if_requested(out_path)
            else:
                self.log(f"\n❌ FAILED with exit code {rc}")
        finally:
            self.running = False

    def _batch_worker(self, folder: Path):
        start = time.time()
        try:
            self.log(f"Batch mode: scanning folder → {folder}")
            files = [p for p in folder.iterdir() if p.is_file() and is_video_file(p)]
            files = [p for p in files if "_autocut" not in p.stem.lower()]

            if not files:
                self.log("No input videos found (or they all look already processed).")
                return

            files.sort(key=natural_key)
            total = len(files)
            self.log(f"Found {total} video(s) to process.")

            backend = self._make_backend()

            for idx, src in enumerate(files, 1):
                out_path = add_autocut_suffix(src)
                self.log(f"\n[{idx}/{total}] Processing: {src.name}")
                rc = backend.run(src, out_path)
                if rc == 0 and out_path.exists():
                    self.log(f"✅ Wrote {out_path.name}")
                    # Always apply fade to each output file
                    final_vid = self._fade_output(out_path)
                    if self.settings.get("extract_frames") and Path(final_vid).exists():
                        frames_root = self._resolve_frames_root(folder, Path(final_vid))
                        self._extract_sampled_frames(Path(final_vid), frames_root, interval=FRAME_EXTRACT_INTERVAL)
                else:
                    self.log(f"❌ FAILED on {src.name} (exit {rc}) — continuing")

            self.log("\n🎬 All videos processed individually (no megacut).")
            dur = time.time() - start
            self.log(f"\nBatch complete in {dur:.1f}s.")
            self.play_done_sound()

        finally:
            self.running = False

    def _resolve_frames_root(self, in_path: Path, out_video: Path) -> Path:
        """Pick the root folder where per-video frame folders will be created."""
        root = (self.settings.get("frames_folder") or "").strip()
        if root:
            return Path(root)
        # Default roots:
        if in_path.is_dir():
            # batch mode default: <input_folder>/<input_folder_name>_frames
            return in_path / f"{in_path.name}_frames"
        # single mode default: <output_parent>/<output_stem>_frames
        return out_video.parent / f"{out_video.stem}_frames"

    def _extract_sampled_frames(self, video: Path, frames_root: Path, interval: int = FRAME_EXTRACT_INTERVAL) -> int:
        """Extract 1 out of every `interval` frames to frames_root/<video_stem>/frame_XXXXXX.png."""
        frames_root.mkdir(parents=True, exist_ok=True)
        out_dir = frames_root / video.stem
        out_dir.mkdir(parents=True, exist_ok=True)

        ff = FFmpegBackend(self.settings, self.log).get_ffmpeg()

        # FFmpeg filter: select frames where n % interval == 0
        # Comma must be escaped for FFmpeg expression parsing.
        vf = f"select=not(mod(n\\,{interval}))"

        out_pattern = str(out_dir / "frame_%06d.png")
        cmd = [
            ff, "-hide_banner", "-y",
            "-i", str(video),
            "-vf", vf,
            "-vsync", "vfr",
            out_pattern,
        ]
        self.log(f"🖼 Extracting frames (1/{interval}) → {out_dir}")
        rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        if rc == 0:
            self.log(f"✅ Frames written to: {out_dir}")
        else:
            self.log(f"⚠ Frame extraction failed (exit {rc}) for: {video.name}")
        return rc

    def _get_duration(self, video: Path) -> float:
        """Return duration (seconds) using ffprobe (no window)."""
        backend = FFmpegBackend(self.settings, self.log, stop_event=self.stop_event, proc_setter=self._set_active_proc)
        ffprobe = backend.get_ffprobe()
        cmd = [ffprobe, "-v", "error", "-show_entries",
               "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=creationflags)
            return float((result.stdout or "").strip() or 0.0)
        except Exception:
            return 0.0

    def _fade_output(self, video: Path):
        """Apply fade-to-black to the end of a video safely."""
        ff = FFmpegBackend(self.settings, self.log).get_ffmpeg()
        dur = self._get_duration(video)
        if dur <= 0:
            self.log(f"⚠ Unable to determine duration for fade: {video}")
            return video

        # Create a temp output next to original
        temp_out = video.with_name(video.stem + "_fade.mp4")
        fade_filter = f"fade=t=out:st={max(dur-2,0):.2f}:d=2"

        cmd = [
            ff, "-hide_banner", "-y",
            "-i", str(video),
            "-vf", fade_filter,
            "-c:a", "copy",
            str(temp_out)
        ]
        self.log("Applying fade (safe mode): " + " ".join(cmd))
        rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        if rc == 0 and temp_out.exists():
            try:
                video.unlink(missing_ok=True)
                temp_out.rename(video)
                self.log(f"✅ Fade applied to {video.name}")
                return video
            except Exception as e:
                self.log(f"⚠ Fade succeeded but rename failed: {e}")
                return temp_out
        else:
            self.log("⚠ Fade failed, keeping original.")
            return video

    def _concat_outputs(self, outputs: list[Path], final_path: Path) -> int:
        """
        Concatenate, apply fade to black to each clip before concat, then
        split output if > 8 hours (28800s)
        """
        # ✅ No per-clip fades anymore
        faded = outputs[:]  # just pass them along unchanged

        ff = FFmpegBackend(self.settings, self.log).get_ffmpeg()
        with tempfile.TemporaryDirectory() as td:
            list_file = Path(td) / "concat.txt"
            with list_file.open("w", encoding="utf-8") as f:
                for p in faded:
                    safe_p = str(p).replace("'", "\\'")
                    f.write(f"file '{safe_p}'\n")

            cmd = [
                ff, "-hide_banner", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(final_path)
            ]

            self.log("Combining with: " + " ".join(cmd))
            rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
            if rc != 0:
                return rc
            # ✅ Apply single fade at the very end
            self._fade_output(final_path)

        # Check final duration
        dur = self._get_duration(final_path)
        self.log(f"Combined duration: {dur/3600:.2f} hours")

        if dur > 28800:  # 8 hours
            self.log("Splitting into PART 1 and PART 2")

            part1 = final_path.with_name(final_path.stem + "_PART1.mp4")
            part2 = final_path.with_name(final_path.stem + "_PART2.mp4")

            # Split evenly
            half = dur / 2
            split_cmds = [
                [ff, "-y", "-i", str(final_path), "-t", str(half), "-c", "copy", str(part1)],
                [ff, "-y", "-i", str(final_path), "-ss", str(half), "-c", "copy", str(part2)]
            ]
            for cmd in split_cmds:
                self.log("Splitting: " + " ".join(cmd))
                run_subprocess(cmd, self.log)

            final_path.unlink()  # remove the giant original
            self.log(f"✅ Created split files:\n{part1}\n{part2}")

        return 0

    # ---------- Misc ----------
    def on_stop(self):
        messagebox.showinfo("Stop", "Close the app to stop the process (safe stop coming soon).")

    def open_in_shotcut_if_requested(self, output_path: Path):
        if not self.open_in_shotcut.get():
            return
        exe = self.shotcut_path.get().strip() or which("shotcut") or which("shotcut.exe")
        if not exe:
            self.log("Shotcut not found. Set its path to auto-open.")
            return
        subprocess.Popen([exe, str(output_path)])
        self.log("Opened in Shotcut.")

    def install_autoeditor(self):
        self.log("Installing Auto-Editor via pip...")
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "auto-editor"]
        rc = run_subprocess(cmd, self.log, stop_event=self.stop_event, proc_setter=self.proc_setter)
        if rc == 0:
            self.log("Auto-Editor installed or updated successfully.")
        else:
            self.log("Failed to install Auto-Editor.")
            
    def play_done_sound(self):
        try:
            winsound.PlaySound(
                r"C:\Users\provo\Desktop\Folder of Folders\streamstuff\Utilities\Python\VolumeThresholdVideo\gruntYay.mp4",
                winsound.SND_FILENAME | winsound.SND_ASYNC
            )
        except Exception as e:
            self.log(f"⚠ Could not play sound: {e}")



def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
