# Shotcut Auto-Cut GUI (Silence/Low-Volume)

# A Tkinter-powered Silence Eradication Device for people who are *brave enough* to click buttons. 

Welcome, noble human. You have obtained a Python GUI that surgically removes awkward dead air from videos so you don’t have to. This tool can batch-process a whole folder of videos, carve out low-volume sections, optionally crossfade transitions (FFmpeg backend), optionally extract frames, and even play you a victory noise when it’s done—because you deserve positive reinforcement for letting software do what software is for. 

---

 What this does (in normal words, for your delicate mortal brain)

This app takes a video (or a folder of videos) and removes quiet / silent parts so the output is more “all action, no waiting around.”

It supports two backends:

1. Auto-Editor (recommended): smarter “remove silence” logic using the `auto-editor` CLI tool. 
2. FFmpeg (fallback / power-user mode): runs FFmpeg `silencedetect`, converts silence into “keep” intervals, cuts segments, then joins them with either hard cuts or true audio+video crossfades. 

---

 Features (a.k.a. “Yes, it really does all this”)

# Core abilities

* ✅ Single video mode (pick a file, get an `_autocut` output). 
* ✅ Batch folder mode (process every video in a folder, non-recursive). 
* ✅ Skips files that already contain `_autocut` (because it’s not a goldfish). 
* ✅ Natural numeric sorting in batch mode (`2.mp4` before `10.mp4`). 

# Silence cutting controls

* Threshold (dBFS): how quiet is “silence.” 
* Min silence (s): how long it must stay quiet to count. 
* Margin (s): expands silence intervals by ±margin so your cuts aren’t too tight. 
* Min kept clip (s): throws away tiny scraps that are too short to matter. 

# Transitions

* Crossfade (s):

  * Auto-Editor: uses `--add-transition fade:<sec>` if > 0. 
  * FFmpeg: true chained `xfade` + `acrossfade` (A/V crossfade). 
  * If FFmpeg produces more than 120 segments, it falls back to hard cuts (filter graphs can explode). 

# Extras (because you can’t be trusted with simplicity)

* 🖼 Frame extraction: “Extract 1 in every 30 frames” option, writes PNGs into per-video folders. 
* 🌑 Fade-to-black at end (2 seconds) applied in batch outputs via `_fade_output`. 
* 🎬 Optional open output in Shotcut when finished. 
* 🛠 One-click Install/Update Auto-Editor via pip. 
* 🔊 Plays a completion sound using `winsound` (Windows) with a hardcoded file path (more on that later, oh yes). 

---

 System Requirements (The Bare Minimum to Contain This Power)

# Required

* Python (Windows strongly implied; `winsound` is used). 
* Tkinter (ships with most Python installs). 
* FFmpeg available in PATH *or* provide a direct path in the GUI. 

# Optional (but realistically you’ll want it)

* Auto-Editor (the app can attempt to install it for you). 
* Shotcut if you want auto-open on completion. 

---

 Installation (Yes, you have to do *something*)

1. Install Python.
2. Save the script somewhere, like:
   `silencerGruntYay.py` 
3. (Recommended) Install FFmpeg:

   * Ensure `ffmpeg` (and ideally `ffprobe`) are accessible in your PATH or set the “FFmpeg path” field in the GUI. 
4. (Optional) Install Auto-Editor:

   * Use the GUI button Install/Update Auto-Editor
   * Or run: `python -m pip install --upgrade auto-editor` 

---

 Running the app (put on your “I can do this” helmet)

From a terminal:

```bash
python silencerGruntYay.py
```

A window opens. If it does not, re-read the previous sentence slowly. 

---

 How to Use (Single File Mode)

1. Click Browse File → choose a video. 
2. Output path auto-fills to `<name>_autocut.<ext>` (and may force `.mp4` depending on suffix). 
3. Pick a Backend:

   * Auto-Editor if you want easier life. 
   * FFmpeg if you want control and/or chaos. 
4. Set parameters:

   * Threshold (dBFS), Min silence (s), Margin (s), Crossfade (s), Min kept clip (s), Audio track. 
5. Optional:

   * Enable Extract 1 in every 30 frames and pick a frames folder. 
   * Enable Open output in Shotcut and set Shotcut path. 
6. Click Run.
7. Observe the log like it’s a sacred prophecy. 

---

 How to Use (Batch Folder Mode)

1. Click Browse Folder → select a folder with videos (non-recursive). 
2. The app finds files with extensions like `.mp4 .mov .mkv .avi .m4v .webm .wmv .flv`. 
3. It skips anything already containing `_autocut` in its name. 
4. It processes videos in natural numeric order (so “Episode 2” behaves like Episode 2). 
5. It outputs per-file `_autocut` videos and applies a fade-to-black at the end (safe mode). 
6. It does NOT build a giant “ALL_autocut” megafile in the current version (despite some older header text implying it might). It logs: “All videos processed individually (no megacut).” 

---

 Backend guide (choose your weapon)

# Auto-Editor backend (Recommended for normal humans)

* If `auto-editor` isn’t found, the app tries to install it via pip. 
* It runs something like:

  * `--edit audio:threshold=<threshold>dB`
  * `--margin <margin>sec`
  * optional: `--add-transition fade:<crossfade>sec` if crossfade > 0
  * outputs to your chosen file


# FFmpeg backend (Recommended for control freaks and wizards)

Pipeline:

1. Uses FFmpeg `silencedetect` to detect silence intervals. 
2. Expands those silences by ±margin, merges overlaps, then inverts into “kept” intervals. 
3. Cuts kept intervals into temp segments (re-encodes with libx264 + aac for accurate cuts). 
4. Joins:

   * Crossfade = 0 → concat demuxer “hard cuts” (with re-encode fallback if `-c copy` fails). 
   * Crossfade > 0 → chained `xfade` + `acrossfade` (true A/V crossfade), unless segments > 120 then fallback to hard cuts. 

---

 Frame extraction (for your inner evidence-hoarder)

If enabled, the app extracts frames from the output video:

* Default interval: 1 out of every 30 frames. 
* Uses FFmpeg `select=not(mod(n\,interval))` and writes PNGs to:
  `frames_root/<video_stem>/frame_000001.png ...` 
* If you don’t choose a frames folder:

  * Batch mode: `<input_folder>/<input_folder_name>_frames/` 
  * Single mode: `<output_parent>/<output_stem>_frames/` 

---

 Fade-to-black (the “don’t end like a psychopath” button you didn’t know you needed)

The app can apply a 2-second fade-out at the end via FFmpeg:

* `fade=t=out:st=<duration-2>:d=2`
* Keeps audio as-is (`-c:a copy`) in that fade step. 

In batch mode it runs this after each successful per-file output. 

---

 The Stop button (aka: “Placebo, with ambition”)

There are two `on_stop` methods in the script, and one overwrites the other. The effective behavior is:

* Clicking Stop shows: “Close the app to stop the process (safe stop coming soon).” 

So yes: your most reliable Stop mechanism is… exiting the program like it’s 1998. 

---

 The completion sound (WARNING: this is hilariously specific)

The app tries to play a sound using `winsound.PlaySound()` pointing at a hardcoded path:

`C:\Users\provo\Desktop\Folder of Folders\streamstuff\Utilities\Python\VolumeThresholdVideo\gruntYay.mp4` 

Notes:

* This is Windows-only (`winsound`). 
* The file path is probably not your file path, unless you are literally that person. 
* If it fails, it logs a warning and continues. 

---

 Common mistakes (you are not the first)

# “It says FFmpeg failed / ffmpeg not found”

* Install FFmpeg and make sure `ffmpeg` is in PATH, or set the FFmpeg path in the GUI. 

# “Auto-Editor isn’t working”

* Click Install/Update Auto-Editor in the GUI, or install via pip. 
* If the CLI isn’t discoverable, `which("auto-editor")` won’t find it. 

# “Crossfade did nothing”

* If using FFmpeg backend and you produced too many segments (>120), it falls back to hard cuts. 
* If crossfade is set to `0`, it will hard cut by design. 

# “Frame extraction didn’t happen”

* You must enable it and pick a frames folder (or accept defaults). 
* Extraction runs on output video, not input. 

# “It won’t open Shotcut”

* You must enable “Open output in Shotcut” and set a path, or have `shotcut` in PATH. 

---

 Troubleshooting (dramatic, but effective)

* Read the Log box. It tells you exactly what command ran and what exploded. 
* Try Auto-Editor backend first if you don’t want to deal with FFmpeg quirks. 
* If FFmpeg concat with `-c copy` fails, the app retries concat with re-encode (more compatible). 
* If fade rename fails after successful fade, it keeps the temp file instead. 

---

 Warnings (the part you will ignore, proudly)

* This tool re-encodes segments in FFmpeg mode for accurate cuts (quality is good, but it’s still a re-encode). 
* Crossfades build a filter graph that can become unwieldy; therefore, the >120 segment fallback exists. 
* Stop button is currently “please close the app” in practice. 
* Completion sound path is extremely specific and likely incorrect for you. 

---

 Final blessing

Go forth. Remove silence. Destroy dead air. Extract frames like a paranoid archivist. Crossfade like a cinematic genius. And when the victory grunt plays (or doesn’t), know this:

You have harnessed a GUI that does not judge you.

(Okay it judges you a little.) 
