"""Headless background-runner test.

Run with:
    AUTO_EDITOR_TEST_VIDEO=/path/to/video.mp4 blender -b --factory-startup \
        --python tests/blender_background.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="auto_editor_background_")
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
    video = str(Path(video).resolve())

    addon.register()
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    sequence_editor = scene.sequence_editor_create()
    collection = vse.top_strips(sequence_editor)
    movie = collection.new_movie("source", video, channel=1, frame_start=1)
    sound = collection.new_sound("source.001", video, channel=2, frame_start=1)
    movie.select = True
    sound.select = True
    settings = scene.auto_editor_settings
    settings.match_fps = True
    settings.ripple = True
    settings.keep_original = False
    settings.error = ""

    assert ops.find_executable(), "auto-editor not found"
    assert bpy.ops.auto_editor.remove_gaps() == {"FINISHED"}
    assert settings.running
    assert ops._ACTIVE is not None

    for _ in range(600):
        if ops._ACTIVE is None:
            break
        ops._poll()
        time.sleep(0.1)

    assert ops._ACTIVE is None
    assert not settings.running
    assert not settings.error
    assert settings.status.startswith("Replaced 2 strip(s)")

    strips = list(vse.top_strips(sequence_editor))
    movies = [strip for strip in strips if strip.type == "MOVIE"]
    sounds = [strip for strip in strips if strip.type == "SOUND"]
    assert len(movies) == 4
    assert len(sounds) == 4
    assert {strip.channel for strip in movies} == {1}
    assert {strip.channel for strip in sounds} == {2}
    print("BACKGROUND PASS", len(movies), len(sounds))

    addon.unregister()
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
