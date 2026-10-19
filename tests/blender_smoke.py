"""Headless smoke test.

Run with:
    AUTO_EDITOR_TEST_VIDEO=/path/to/video.mp4 blender -b --factory-startup \
        --python tests/blender_smoke.py
"""
import os
import queue
import shutil
import sys
import tempfile
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="auto_editor_smoke_")
PACKAGE = Path(TMP) / "auto_editor_blender"
shutil.copytree(
    ROOT, PACKAGE, ignore=shutil.ignore_patterns("__pycache__", ".git", "tests")
)
sys.path.insert(0, TMP)

import auto_editor_blender as addon  # noqa: E402
from auto_editor_blender import ops, vse  # noqa: E402


def main():
    video = os.environ.get("AUTO_EDITOR_TEST_VIDEO")
    if not video:
        print("SKIP: set AUTO_EDITOR_TEST_VIDEO to a short video file")
        return

    addon.register()
    scene = bpy.context.scene
    scene.render.fps = 24
    scene.render.fps_base = 1.0
    sequence_editor = scene.sequence_editor_create()
    source = sequence_editor.strips.new_movie(
        "source", video, channel=1, frame_start=1
    )
    original_start = source.frame_final_start
    original_name = source.name

    exe = ops.find_executable()
    assert exe, "auto-editor not found"
    options = ops.RunOptions(
        preset="SILENCE",
        audio_threshold=4.0,
        motion_threshold=2.0,
        margin=0.2,
        extra_args="",
    )
    tempdir = tempfile.mkdtemp(prefix="auto_editor_v3_")
    messages = queue.Queue()
    worker = ops.AnalyzeWorker(
        [ops.Job(source, vse.source_path(source), source.name)],
        options,
        exe,
        tempdir,
        messages,
    )
    worker.start()
    worker.join()

    done = []
    while not messages.empty():
        message = messages.get()
        if message[0] == "done":
            done.append(message)
        elif message[0] == "error":
            raise AssertionError(message[1] + "\n" + message[2])
    assert done, "analysis did not finish"

    settings = scene.auto_editor_settings
    settings.match_fps = True
    settings.ripple = True
    settings.keep_original = False
    summary = vse.build_cuts(bpy.context, done[-1][1], settings)
    print(summary)

    strips = list(sequence_editor.strips)
    movies = sorted(
        (strip for strip in strips if strip.type == "MOVIE"),
        key=lambda strip: strip.frame_final_start,
    )
    sounds = sorted(
        (strip for strip in strips if strip.type == "SOUND"),
        key=lambda strip: strip.frame_final_start,
    )
    assert original_name not in {strip.name for strip in strips}
    assert len(movies) > 1
    assert len(movies) == len(sounds)
    assert movies[0].frame_final_start == original_start
    for left, right in zip(movies, movies[1:]):
        assert right.frame_final_start == left.frame_final_start + left.frame_final_duration
    for movie, sound in zip(movies, sounds):
        assert movie.frame_final_start == sound.frame_final_start
        assert movie.frame_final_duration == sound.frame_final_duration
    assert abs(scene.render.fps / scene.render.fps_base - 30.0) < 1e-4
    print("PASS", len(movies), "movie strips,", len(sounds), "sound strips")

    addon.unregister()
    shutil.rmtree(tempdir, ignore_errors=True)


if __name__ == "__main__":
    main()
