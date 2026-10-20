from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import bpy

from . import v3, vse


ADDON_ID = __package__
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class RunOptions:
    preset: str
    audio_threshold: float
    motion_threshold: float
    margin: float
    extra_args: str


@dataclass(frozen=True)
class Job:
    strip: object
    source: str
    name: str
    audio_strip: object | None = None


def _same_source(a, b) -> bool:
    try:
        return Path(vse.source_path(a)).resolve() == Path(vse.source_path(b)).resolve()
    except vse.BuildError:
        return False


def _overlap_ratio(a, b) -> float:
    a_start = a.frame_final_start
    a_end = a_start + a.frame_final_duration
    b_start = b.frame_final_start
    b_end = b_start + b.frame_final_duration
    overlap = max(0.0, min(a_end, b_end) - max(a_start, b_start))
    shorter = min(a.frame_final_duration, b.frame_final_duration)
    return overlap / shorter if shorter > 0 else 0.0


def build_jobs(selected) -> list[Job]:
    movies = [strip for strip in selected if strip.type == "MOVIE"]
    sounds = [strip for strip in selected if strip.type == "SOUND"]
    paired: set[int] = set()
    jobs: list[Job] = []

    for movie in movies:
        audio_strip = None
        best_score = -1.0
        for sound in sounds:
            if id(sound) in paired:
                continue
            ratio = _overlap_ratio(movie, sound)
            if ratio <= 0:
                continue
            same = _same_source(movie, sound)
            if not same and ratio < 0.8:
                continue
            score = ratio + (2.0 if same else 0.0)
            if score > best_score:
                audio_strip = sound
                best_score = score
        if audio_strip is not None:
            paired.add(id(audio_strip))
        jobs.append(
            Job(
                strip=movie,
                source=vse.source_path(movie),
                name=movie.name,
                audio_strip=audio_strip,
            )
        )

    for sound in sounds:
        if id(sound) in paired:
            continue
        jobs.append(Job(strip=sound, source=vse.source_path(sound), name=sound.name))
    return jobs


def _prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def find_executable(prefs=None) -> str | None:
    candidates = []
    if prefs is not None and getattr(prefs, "exe_path", ""):
        candidates.append(prefs.exe_path)
    for name in ("auto-editor", "auto-editor.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    home = Path.home()
    candidates.extend(
        [
            home / ".local" / "bin" / "auto-editor",
            home / "bin" / "auto-editor",
            "/usr/local/bin/auto-editor",
            "/usr/bin/auto-editor",
            "/opt/homebrew/bin/auto-editor",
        ]
    )
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Programs" / "auto-editor" / "auto-editor.exe")
        candidates.append(home / "auto-editor.exe")

    for candidate in candidates:
        try:
            path = Path(bpy.path.abspath(str(candidate))).expanduser()
        except Exception:
            continue
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return str(path.resolve())
    return None


def run_version(exe: str) -> str:
    completed = subprocess.run(
        [exe, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=NO_WINDOW,
    )
    output = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or f"auto-editor exited with {completed.returncode}")
    return output


def build_edit_expr(options) -> str:
    if options.preset == "MOTION":
        return f"motion:threshold={options.motion_threshold / 100.0:g}"
    return f"audio:threshold={options.audio_threshold / 100.0:g},stream=all"


def build_args(exe: str, source: str, v3_path: str, options) -> list[str]:
    args = [
        exe,
        source,
        "--edit",
        build_edit_expr(options),
        "--margin",
        f"{options.margin:g}s",
        "--export",
        "v3",
        "-o",
        v3_path,
        "--progress",
        "machine",
        "--no-open",
    ]
    try:
        extra = shlex.split(options.extra_args, posix=(os.name != "nt"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid extra arguments: {exc}") from exc
    args.extend(extra)
    return args


def command_preview(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def run_auto_editor(
    exe: str,
    source: str,
    v3_path: str,
    options,
    log=None,
    progress=None,
    cancel_event=None,
    on_proc=None,
) -> None:
    args = build_args(exe, source, v3_path, options)
    if log:
        log(command_preview(args))
    proc = subprocess.Popen(
        args,
        cwd=str(Path(source).parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        creationflags=NO_WINDOW,
    )
    if on_proc:
        on_proc(proc)

    buffer = b""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        buffer += chunk
        lines = re.split(rb"[\r\n]+", buffer)
        buffer = lines.pop()
        for raw in lines:
            text = ANSI_RE.sub("", raw.decode("utf-8", "replace")).strip()
            if not text:
                continue
            if log:
                log(text)
            if progress and "~" in text:
                parts = text.rsplit("~", 3)
                if len(parts) == 4:
                    try:
                        progress(float(parts[1]), float(parts[2]))
                    except ValueError:
                        pass
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()

    if buffer:
        text = ANSI_RE.sub("", buffer.decode("utf-8", "replace")).strip()
        if text and log:
            log(text)

    returncode = proc.wait()
    proc.stdout.close()
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()
    if returncode != 0:
        raise RuntimeError(f"auto-editor exited with code {returncode}")


class AnalyzeWorker(threading.Thread):
    def __init__(self, jobs, options, exe, tempdir, out_queue):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.options = options
        self.exe = exe
        self.tempdir = tempdir
        self.out_queue = out_queue
        self.cancel_event = threading.Event()
        self.proc = None
        self.log_lines = deque(maxlen=40)
        self.total_runs = len(jobs)

    def cancel(self):
        self.cancel_event.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def _log(self, line):
        self.log_lines.append(line)

    def _set_proc(self, proc):
        self.proc = proc

    def _analyze(self, source, output, run_index, label):
        self.out_queue.put(("status", f"Analyzing {label}"))

        def progress(current, total, idx=run_index):
            fraction = current / total if total else 0.0
            self.out_queue.put(("progress", idx, fraction))

        run_auto_editor(
            self.exe,
            source,
            output,
            self.options,
            log=self._log,
            progress=progress,
            cancel_event=self.cancel_event,
            on_proc=self._set_proc,
        )

    def run(self):
        try:
            results = []
            for index, job in enumerate(self.jobs):
                if self.cancel_event.is_set():
                    break
                v3_path = os.path.join(self.tempdir, f"job_{index}.v3")
                self._analyze(job.source, v3_path, index, job.name)
                if self.cancel_event.is_set():
                    break
                timeline = v3.load(v3_path)
                v3.validate_supported(timeline)
                results.append((job.strip, timeline, job.audio_strip))

            if self.cancel_event.is_set():
                self.out_queue.put(("cancelled",))
            else:
                self.out_queue.put(("done", results))
        except Cancelled:
            self.out_queue.put(("cancelled",))
        except Exception as exc:
            self.out_queue.put(("error", str(exc), "\n".join(self.log_lines)))


class AUTO_EDITOR_OT_detect_binary(bpy.types.Operator):
    bl_idname = "auto_editor.detect_binary"
    bl_label = "Detect auto-editor"
    bl_description = "Find the auto-editor executable on PATH or in common locations"

    def execute(self, context):
        prefs = _prefs(context)
        path = find_executable(prefs)
        if not path:
            if prefs is not None:
                prefs.version = ""
            self.report(
                {"ERROR"},
                "auto-editor not found. Install the official binary or set its path.",
            )
            return {"CANCELLED"}
        if prefs is not None:
            prefs.exe_path = path
        try:
            version = run_version(path)
        except Exception as exc:
            if prefs is not None:
                prefs.version = ""
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if prefs is not None:
            prefs.version = version
        self.report({"INFO"}, f"Found {version}")
        return {"FINISHED"}


class AUTO_EDITOR_OT_test_binary(bpy.types.Operator):
    bl_idname = "auto_editor.test_binary"
    bl_label = "Test auto-editor"
    bl_description = "Run auto-editor --version"

    def execute(self, context):
        prefs = _prefs(context)
        path = find_executable(prefs)
        if not path:
            if prefs is not None:
                prefs.version = ""
            self.report(
                {"ERROR"},
                "auto-editor not found. Install the official binary or set its path.",
            )
            return {"CANCELLED"}
        try:
            version = run_version(path)
        except Exception as exc:
            if prefs is not None:
                prefs.version = ""
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if prefs is not None:
            prefs.exe_path = path
            prefs.version = version
        self.report({"INFO"}, f"{version} ({path})")
        return {"FINISHED"}


@dataclass
class ActiveJob:
    worker: AnalyzeWorker
    queue: queue.Queue
    tempdir: str
    settings: object
    scene: object
    total_runs: int
    label: str = ""
    percent: int = -1


_ACTIVE: ActiveJob | None = None


def _tag_redraw():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


def _write_log(text):
    try:
        log = bpy.data.texts.get("Auto-Editor Log")
        if log is None:
            log = bpy.data.texts.new("Auto-Editor Log")
        log.clear()
        log.write(text or "")
    except Exception:
        pass


def _set_status(job, text):
    try:
        job.scene.auto_editor_settings.status = text
    except Exception:
        pass
    _tag_redraw()


def _finish_job(job, summary="", error="", cancelled=False):
    global _ACTIVE
    try:
        settings = job.scene.auto_editor_settings
        settings.running = False
        settings.error = error
        settings.status = "Cancelled" if cancelled else (error or summary)
    except Exception:
        pass
    shutil.rmtree(job.tempdir, ignore_errors=True)
    _ACTIVE = None
    _tag_redraw()


def _poll():
    job = _ACTIVE
    if job is None:
        return None
    try:
        while True:
            message = job.queue.get_nowait()
            kind = message[0]
            if kind == "progress":
                _, index, fraction = message
                percent = int(
                    min(100.0, max(0.0, ((index + fraction) / job.total_runs) * 100.0))
                )
                if percent != job.percent:
                    job.percent = percent
                    _set_status(job, f"{job.label or 'Working'} — {percent}%")
            elif kind == "status":
                job.label = message[1]
                job.percent = -1
                _set_status(job, f"{job.label}…")
            elif kind == "error":
                _write_log(message[2])
                _finish_job(job, error=message[1])
                return None
            elif kind == "cancelled":
                _finish_job(job, cancelled=True)
                return None
            elif kind == "done":
                try:
                    summary = vse.build_cuts(
                        SimpleNamespace(scene=job.scene), message[1], job.settings
                    )
                except Exception as exc:
                    _write_log("")
                    _finish_job(job, error=str(exc))
                else:
                    _finish_job(job, summary=summary)
                return None
    except queue.Empty:
        pass
    return 0.1


def stop():
    global _ACTIVE
    job = _ACTIVE
    if job is None:
        return
    job.worker.cancel()
    try:
        job.worker.join(timeout=2)
    except Exception:
        pass
    shutil.rmtree(job.tempdir, ignore_errors=True)
    try:
        job.scene.auto_editor_settings.running = False
        job.scene.auto_editor_settings.status = "Cancelled"
    except Exception:
        pass
    _ACTIVE = None
    try:
        if bpy.app.timers.is_registered(_poll):
            bpy.app.timers.unregister(_poll)
    except Exception:
        pass


class AUTO_EDITOR_OT_remove_gaps(bpy.types.Operator):
    bl_idname = "auto_editor.remove_gaps"
    bl_label = "Remove Gaps"
    bl_description = (
        "Analyze selected strips with auto-editor and replace them with trimmed, "
        "back-to-back strips. Source files are not modified"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        settings = context.scene.auto_editor_settings
        has_strips = any(
            strip.type in {"MOVIE", "SOUND"} for strip in vse.selected_strips(context)
        )
        return (
            not settings.running
            and has_strips
            and find_executable(_prefs(context)) is not None
        )

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        global _ACTIVE
        if _ACTIVE is not None:
            self.report({"ERROR"}, "An Auto-Editor job is already running.")
            return {"CANCELLED"}

        prefs = _prefs(context)
        exe = find_executable(prefs)
        if not exe:
            self.report({"ERROR"}, "auto-editor not found. Set its path in preferences.")
            return {"CANCELLED"}

        selected = [
            strip
            for strip in vse.selected_strips(context)
            if strip.type in {"MOVIE", "SOUND"}
        ]
        if not selected:
            self.report({"ERROR"}, "Select one or more movie strips in the VSE.")
            return {"CANCELLED"}

        settings = context.scene.auto_editor_settings
        options = RunOptions(
            preset=settings.preset,
            audio_threshold=settings.audio_threshold,
            motion_threshold=settings.motion_threshold,
            margin=settings.margin,
            extra_args=settings.extra_args,
        )

        try:
            jobs = build_jobs(selected)
        except vse.BuildError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        messages = queue.Queue()
        worker = AnalyzeWorker(
            jobs,
            options,
            exe,
            tempfile.mkdtemp(prefix="auto_editor_"),
            messages,
        )
        job = ActiveJob(
            worker=worker,
            queue=messages,
            tempdir=worker.tempdir,
            settings=SimpleNamespace(
                match_fps=settings.match_fps,
                ripple=settings.ripple,
                keep_original=settings.keep_original,
            ),
            scene=context.scene,
            total_runs=worker.total_runs,
            label=jobs[0].name,
        )
        settings.error = ""
        settings.status = "Starting auto-editor…"
        settings.running = True
        _ACTIVE = job
        worker.start()
        try:
            if not bpy.app.timers.is_registered(_poll):
                bpy.app.timers.register(_poll, first_interval=0.1)
        except Exception as exc:
            worker.cancel()
            _finish_job(job, error=f"Could not start progress timer: {exc}")
            return {"CANCELLED"}
        _tag_redraw()
        return {"FINISHED"}


class AUTO_EDITOR_OT_cancel(bpy.types.Operator):
    bl_idname = "auto_editor.cancel"
    bl_label = "Cancel auto-editor"
    bl_description = "Stop the running auto-editor analysis"

    @classmethod
    def poll(cls, context):
        return context.scene.auto_editor_settings.running

    def execute(self, context):
        job = _ACTIVE
        if job is not None:
            job.worker.cancel()
        context.scene.auto_editor_settings.status = "Cancelling…"
        _tag_redraw()
        return {"FINISHED"}


classes = (
    AUTO_EDITOR_OT_detect_binary,
    AUTO_EDITOR_OT_test_binary,
    AUTO_EDITOR_OT_remove_gaps,
    AUTO_EDITOR_OT_cancel,
)
