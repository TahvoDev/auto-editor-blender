"""Headless movie/sound pairing test.

Run with:
    AUTO_EDITOR_TEST_VIDEO=/path/to/video.mp4 blender -b --factory-startup \
        --python tests/blender_pair.py
"""
import math
import os
import queue
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.mkdtemp(prefix="auto_editor_pair_")
PACKAGE = Path(TMP) / "auto_editor_blender"
shutil.copytree(
    ROOT, PACKAGE, ignore=shutil.ignore_patterns("__pycache__", ".git", "tests")
)
sys.path.insert(0, TMP)

import auto_editor_blender as addon  # noqa: E402
from auto_editor_blender import ops, vse  # noqa: E402


def make_wav(path: Path, seconds: int = 10, rate: int = 48000) -> None:
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = []
        for index in range(rate * seconds):
            time = index / rate
            amplitude = 0 if int(time) % 2 else 12000
            frames.append(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * time))))
        handle.writeframes(b"".join(frames))


def analyze(jobs, temp_root: Path):
    tempdir = tempfile.mkdtemp(prefix="v3_", dir=temp_root)
    messages = queue.Queue()
    worker = ops.AnalyzeWorker(
        jobs,
        ops.RunOptions("SILENCE", 4.0, 2.0, 0.2, ""),
        ops.find_executable(),
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
    return done[-1][1]


def clear(sequence_editor) -> None:
    collection = vse.top_strips(sequence_editor)
    for strip in list(collection):
        collection.remove(strip)


def split(sequence_editor):
    strips = list(vse.top_strips(sequence_editor))
    movies = sorted(
        (strip for strip in strips if strip.type == "MOVIE"),
        key=lambda strip: strip.frame_final_start,
    )
    sounds = sorted(
        (strip for strip in strips if strip.type == "SOUND"),
        key=lambda strip: strip.frame_final_start,
    )
    return movies, sounds


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
    settings = scene.auto_editor_settings
    settings.match_fps = True
    settings.ripple = True
    settings.keep_original = False
    assert ops.find_executable(), "auto-editor not found"

    temp_root = Path(tempfile.mkdtemp(prefix="auto_editor_pair_runs_"))
    audio_path = temp_root / "gapped.wav"
    make_wav(audio_path)
    collection = vse.top_strips(sequence_editor)

    movie = collection.new_movie("source", video, channel=1, frame_start=1)
    sound = collection.new_sound("source.001", video, channel=2, frame_start=1)
    jobs = ops.build_jobs([movie, sound])
    assert len(jobs) == 1
    assert jobs[0].audio_strip is sound
    results = analyze(jobs, temp_root)
    print(vse.build_cuts(bpy.context, results, settings))
    movies, sounds = split(sequence_editor)
    assert len(movies) > 1
    assert len(movies) == len(sounds)
    assert {strip.name for strip in movies} != {"source"}
    assert all(
        Path(bpy.path.abspath(strip.sound.filepath)).resolve() == Path(video)
        for strip in sounds
    )
    for movie, sound in zip(movies, sounds):
        assert movie.frame_final_start == sound.frame_final_start
        assert movie.frame_final_duration == sound.frame_final_duration
    print("PASS same-source", len(movies), "movie,", len(sounds), "sound")
    clear(sequence_editor)

    movie = collection.new_movie("paired", video, channel=1, frame_start=1)
    sound = collection.new_sound("audio", str(audio_path), channel=2, frame_start=1)
    jobs = ops.build_jobs([movie, sound])
    assert len(jobs) == 1
    assert jobs[0].audio_strip is sound
    results = analyze(jobs, temp_root)
    print(vse.build_cuts(bpy.context, results, settings))
    movies, sounds = split(sequence_editor)
    assert len(movies) > 1
    assert 0 < len(sounds) <= len(movies)
    assert all(
        Path(bpy.path.abspath(strip.sound.filepath)).resolve() == audio_path.resolve()
        for strip in sounds
    )
    assert movies[0].frame_final_start == sounds[0].frame_final_start
    assert movies[0].frame_final_duration == sounds[0].frame_final_duration
    print("PASS separate-audio", len(movies), "movie,", len(sounds), "sound")
    clear(sequence_editor)

    sound = collection.new_sound("audio-only", str(audio_path), channel=1, frame_start=1)
    jobs = ops.build_jobs([sound])
    assert len(jobs) == 1
    assert jobs[0].audio_strip is None
    results = analyze(jobs, temp_root)
    print(vse.build_cuts(bpy.context, results, settings))
    movies, sounds = split(sequence_editor)
    assert not movies
    assert len(sounds) > 1
    print("PASS sound-only", len(sounds), "sound")
    print("PAIR PASS")

    addon.unregister()
    shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
