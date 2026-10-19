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

    def run(self):
        try:
            results = []
            for index, job in enumerate(self.jobs):
                if self.cancel_event.is_set():
                    break
                v3_path = os.path.join(self.tempdir, f"job_{index}.v3")
                self.out_queue.put(("status", f"Analyzing {job.name}"))

                def progress(current, total, idx=index):
                    fraction = current / total if total else 0.0
                    self.out_queue.put(("progress", idx, fraction))

                run_auto_editor(
                    self.exe,
                    job.source,
                    v3_path,
                    self.options,
                    log=self._log,
                    progress=progress,
                    cancel_event=self.cancel_event,
                    on_proc=self._set_proc,
                )
                if self.cancel_event.is_set():
                    break
                timeline = v3.load(v3_path)
                v3.validate_supported(timeline)
                results.append((job.strip, timeline))

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
        has_strips = any(
            strip.type in {"MOVIE", "SOUND"} for strip in vse.selected_strips(context)
        )
        return has_strips and find_executable(_prefs(context)) is not None

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
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

        jobs = []
        for strip in selected:
            try:
                jobs.append(Job(strip, vse.source_path(strip), strip.name))
            except vse.BuildError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

        self._tempdir = tempfile.mkdtemp(prefix="auto_editor_")
        self._queue = queue.Queue()
        self._worker = AnalyzeWorker(jobs, options, exe, self._tempdir, self._queue)
        self._settings = settings
        self._job_count = len(jobs)
        self._cleaned = False
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        context.window_manager.progress_begin(0, 100)
        self._worker.start()
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC":
            return self.cancel(context)

        if event.type != "TIMER":
            return {"RUNNING_MODAL"}

        try:
            while True:
                message = self._queue.get_nowait()
                kind = message[0]
                if kind == "progress":
                    _, index, fraction = message
                    percent = ((index + fraction) / self._job_count) * 100.0
                    context.window_manager.progress_update(min(100.0, max(0.0, percent)))
                elif kind == "status":
                    self._status(context, message[1])
                elif kind == "error":
                    _, error, log = message
                    self._write_log(log)
                    self.report({"ERROR"}, error)
                    self._cleanup(context)
                    return {"CANCELLED"}
                elif kind == "cancelled":
                    self._cleanup(context)
                    return {"CANCELLED"}
                elif kind == "done":
                    try:
                        summary = vse.build_cuts(context, message[1], self._settings)
                    except Exception as exc:
                        self._write_log("")
                        self.report({"ERROR"}, str(exc))
                        self._cleanup(context)
                        return {"CANCELLED"}
                    self.report({"INFO"}, summary)
                    self._cleanup(context)
                    return {"FINISHED"}
        except queue.Empty:
            pass
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        if getattr(self, "_worker", None) is not None:
            self._worker.cancel()
        self._cleanup(context)
        return {"CANCELLED"}

    def _status(self, context, text):
        try:
            context.workspace.status_text_set(f"Auto-Editor: {text}")
        except Exception:
            pass

    def _write_log(self, text):
        log = bpy.data.texts.get("Auto-Editor Log")
        if log is None:
            log = bpy.data.texts.new("Auto-Editor Log")
        log.clear()
        log.write(text or "")

    def _cleanup(self, context):
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        try:
            context.window_manager.progress_end()
        except Exception:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        if getattr(self, "_timer", None) is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
        tempdir = getattr(self, "_tempdir", None)
        if tempdir:
            shutil.rmtree(tempdir, ignore_errors=True)
        try:
            for area in context.screen.areas:
                area.tag_redraw()
        except Exception:
            pass


classes = (
    AUTO_EDITOR_OT_detect_binary,
    AUTO_EDITOR_OT_test_binary,
    AUTO_EDITOR_OT_remove_gaps,
)
