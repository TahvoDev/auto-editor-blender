from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import bpy

from . import v3


MAX_CHANNEL = 128


class BuildError(Exception):
    pass


@dataclass
class ClipWindow:
    src_offset: int
    duration: int


@dataclass
class NewStrip:
    kind: str
    source: str
    channel: int
    new_start: int
    src_offset: int
    duration: int
    offset_end: int
    name: str


def top_strips(sequence_editor):
    collection = getattr(sequence_editor, "strips", None) or getattr(
        sequence_editor, "sequences", None
    )
    return collection if collection is not None else ()


def all_strips(sequence_editor):
    return (
        getattr(sequence_editor, "strips_all", None)
        or getattr(sequence_editor, "sequences_all", None)
        or top_strips(sequence_editor)
    )


def selected_strips(context) -> list:
    for attribute in (
        "selected_editable_strips",
        "selected_editable_sequences",
        "selected_strips",
        "selected_sequences",
    ):
        value = getattr(context, attribute, None)
        if value is not None:
            return list(value)
    sequence_editor = context.scene.sequence_editor
    if sequence_editor is None:
        return []
    return [strip for strip in top_strips(sequence_editor) if strip.select]


def source_path(strip) -> str:
    if strip.type == "MOVIE":
        path = bpy.path.abspath(strip.filepath)
    elif strip.type == "SOUND":
        path = bpy.path.abspath(strip.sound.filepath)
    else:
        raise BuildError("Select movie or sound strips only")
    if not path or not Path(path).is_file():
        raise BuildError(f"Source file not found: {path!r}")
    return path


def scene_fps(scene) -> float:
    return scene.render.fps / scene.render.fps_base


def set_scene_fps(scene, num: int, den: int) -> None:
    scene.render.fps = num
    scene.render.fps_base = float(den)


def find_free_channel(sequence_editor) -> int:
    strips = list(top_strips(sequence_editor))
    return max((strip.channel for strip in strips), default=0) + 1


def has_speed_control(sequence_editor, strip) -> bool:
    for candidate in top_strips(sequence_editor):
        if getattr(candidate, "type", None) == "SPEED" and getattr(
            candidate, "input_1", None
        ) == strip:
            return True
    return False


def _frames(clip: v3.Clip, tb_fps: float, fps: float) -> tuple[int, int, int]:
    return (
        round(clip.start / tb_fps * fps),
        round(clip.dur / tb_fps * fps),
        round(clip.offset / tb_fps * fps),
    )


def _window(
    clip: v3.Clip,
    strip,
    source_total: int,
    tb_fps: float,
    fps: float,
    max_duration: int | None = None,
) -> ClipWindow | None:
    _, duration, offset = _frames(clip, tb_fps, fps)
    visible_start = max(0, int(round(strip.frame_offset_start)))
    visible_end = visible_start + int(round(strip.frame_final_duration))

    start = max(offset, visible_start)
    end = min(offset + duration, visible_end)
    if max_duration is not None:
        end = min(end, start + max_duration)
    if end <= start:
        return None
    length = end - start
    if start + length > source_total:
        raise BuildError(
            f"v3 clip runs past the end of {Path(source_path(strip)).name!r}"
        )
    return ClipWindow(start, length)


def _trim(strip, new_start: int, src_offset: int, duration: int) -> None:
    total = int(round(strip.frame_duration))
    if src_offset + duration > total:
        raise BuildError(
            f"{strip.name!r}: source range {src_offset}+{duration} exceeds {total} frames"
        )
    strip.frame_offset_start = src_offset
    strip.frame_offset_end = max(0, total - src_offset - duration)
    strip.frame_start = new_start - src_offset
    if int(round(strip.frame_final_duration)) != duration:
        strip.frame_final_duration = duration


def _plan_group(sequence_editor, group, tb_fps: float, fps: float, channels):
    """Return (new_strips, originals, cursor_end)."""
    group = sorted(group, key=lambda job: job[0].frame_final_start)
    shared_cursor = len(group) > 1
    cursor = int(round(group[0][0].frame_final_start))
    movie_channel, sound_channel = channels
    planned: list[NewStrip] = []
    originals = []

    for strip, timeline in group:
        originals.append(strip)
        if has_speed_control(sequence_editor, strip):
            raise BuildError(
                f"{strip.name!r} has a Speed Control effect; flatten it first"
            )

        if not shared_cursor:
            cursor = int(round(strip.frame_final_start))

        if strip.type == "MOVIE":
            video_clips = v3.kept(v3.single_video_layer(timeline))
            if not video_clips:
                continue
            audio_clips = v3.kept(v3.single_audio_layer(timeline))
            source_total = int(round(strip.frame_duration))
            if any(clip.stream for clip in audio_clips):
                raise BuildError("Multi-stream audio is not supported yet")
            source = source_path(strip)

            for index, clip in enumerate(video_clips):
                window = _window(clip, strip, source_total, tb_fps, fps)
                if window is None:
                    continue
                planned.append(
                    NewStrip(
                        kind="MOVIE",
                        source=source,
                        channel=movie_channel,
                        new_start=cursor,
                        src_offset=window.src_offset,
                        duration=window.duration,
                        offset_end=source_total - window.src_offset - window.duration,
                        name=f"{strip.name} cut {index + 1}",
                    )
                )
                if index < len(audio_clips):
                    audio_window = _window(
                        audio_clips[index],
                        strip,
                        source_total,
                        tb_fps,
                        fps,
                        max_duration=window.duration,
                    )
                    if audio_window is not None:
                        planned.append(
                            NewStrip(
                                kind="SOUND",
                                source=source,
                                channel=sound_channel,
                                new_start=cursor,
                                src_offset=audio_window.src_offset,
                                duration=audio_window.duration,
                                offset_end=source_total
                                - audio_window.src_offset
                                - audio_window.duration,
                                name=f"{strip.name} audio {index + 1}",
                            )
                        )
                cursor += window.duration

        elif strip.type == "SOUND":
            audio_clips = v3.kept(v3.single_audio_layer(timeline))
            if not audio_clips:
                raise BuildError(f"No kept audio found for {strip.name!r}")
            if any(clip.stream for clip in audio_clips):
                raise BuildError("Multi-stream audio is not supported yet")
            source_total = int(round(strip.frame_duration))
            source = source_path(strip)
            for index, clip in enumerate(audio_clips):
                window = _window(clip, strip, source_total, tb_fps, fps)
                if window is None:
                    continue
                planned.append(
                    NewStrip(
                        kind="SOUND",
                        source=source,
                        channel=sound_channel,
                        new_start=cursor,
                        src_offset=window.src_offset,
                        duration=window.duration,
                        offset_end=source_total - window.src_offset - window.duration,
                        name=f"{strip.name} cut {index + 1}",
                    )
                )
                cursor += window.duration
        else:
            raise BuildError(f"Unsupported strip type: {strip.type}")

    return planned, originals, cursor


def build_cuts(context, jobs, settings) -> str:
    """jobs is [(original_strip, v3.Timeline), ...]."""
    if not jobs:
        raise BuildError("Select at least one movie strip")

    scene = context.scene
    sequence_editor = scene.sequence_editor
    if sequence_editor is None:
        raise BuildError("Open the Video Sequencer first")

    fps_before = scene_fps(scene)
    old_fps = scene.render.fps
    old_fps_base = scene.render.fps_base

    timebase = jobs[0][1].fps
    for _, timeline in jobs:
        if abs(timeline.fps - timebase) > 1e-4:
            raise BuildError(
                "Selected sources have different frame rates; process them separately"
            )
        v3.validate_supported(timeline)

    fps_changed = abs(fps_before - timebase) > 1e-4
    if fps_changed and not settings.match_fps:
        raise BuildError(
            f"Scene is {fps_before:.3f} fps but the source is {timebase:.3f} fps. "
            "Enable 'Match scene FPS' or change the scene frame rate."
        )

    strip_collection = top_strips(sequence_editor)
    created = []
    try:
        if fps_changed:
            set_scene_fps(scene, jobs[0][1].timebase_num, jobs[0][1].timebase_den)
        fps = scene_fps(scene)

        selected = [strip for strip, _ in jobs]
        ripple = (
            settings.ripple
            and len(jobs) > 1
            and len({strip.channel for strip in selected}) == 1
        )
        groups = [jobs] if ripple else [[job] for job in jobs]
        groups.sort(key=lambda group: min(strip.channel for strip, _ in group))

        next_channel = find_free_channel(sequence_editor)
        group_channels = []
        for group in groups:
            has_movie = any(strip.type == "MOVIE" for strip, _ in group)
            has_audio = any(strip.type == "SOUND" for strip, _ in group) or any(
                v3.kept(v3.single_audio_layer(timeline))
                for strip, timeline in group
                if strip.type == "MOVIE"
            )
            movie_channel = next_channel if has_movie else None
            if has_movie:
                next_channel += 1
            sound_channel = next_channel if has_audio else None
            if has_audio:
                next_channel += 1
            group_channels.append((movie_channel, sound_channel))

        if next_channel - 1 > MAX_CHANNEL:
            raise BuildError(f"Not enough free VSE channels (max {MAX_CHANNEL})")

        plan = []
        originals = []
        for group, channels in zip(groups, group_channels):
            movie_channel, sound_channel = channels
            if movie_channel is None and sound_channel is None:
                continue
            group_plan, group_originals, _ = _plan_group(
                sequence_editor, group, timebase, fps, channels
            )
            plan.extend(group_plan)
            originals.extend(group_originals)

        if not plan:
            raise BuildError("auto-editor kept nothing for the selected strip(s)")

        used_channels = {strip.channel for strip in strip_collection}
        used_channels.update(item.channel for item in plan)
        temp_channel = next(
            (
                channel
                for channel in range(1, MAX_CHANNEL + 1)
                if channel not in used_channels
            ),
            None,
        )
        if temp_channel is None:
            raise BuildError("Not enough free VSE channels to stage the edit")

        for item in plan:
            if item.kind == "MOVIE":
                strip = strip_collection.new_movie(
                    item.name, item.source, temp_channel, frame_start=0
                )
            else:
                strip = strip_collection.new_sound(
                    item.name, item.source, temp_channel, frame_start=0
                )
            created.append(strip)
            _trim(strip, item.new_start, item.src_offset, item.duration)
            strip.channel = item.channel

        if settings.keep_original:
            for strip in originals:
                strip.mute = True
        else:
            for strip in originals:
                try:
                    strip_collection.remove(strip)
                except (ReferenceError, RuntimeError):
                    pass

        try:
            bpy.ops.ed.undo_push(message="Auto-Editor: remove gaps")
        except RuntimeError:
            pass
    except Exception:
        for strip in created:
            try:
                strip_collection.remove(strip)
            except (ReferenceError, RuntimeError):
                pass
        if fps_changed:
            scene.render.fps = old_fps
            scene.render.fps_base = old_fps_base
        raise

    return f"Replaced {len(originals)} strip(s) with {len(created)} trimmed strip(s)."
