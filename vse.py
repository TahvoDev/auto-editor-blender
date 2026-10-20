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
    collection = getattr(sequence_editor, "strips", None)
    if collection is None:
        collection = getattr(sequence_editor, "sequences", None)
    return collection if collection is not None else ()


def all_strips(sequence_editor):
    collection = getattr(sequence_editor, "strips_all", None)
    if collection is None:
        collection = getattr(sequence_editor, "sequences_all", None)
    return collection if collection is not None else top_strips(sequence_editor)


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


def _strip_key(strip) -> int:
    try:
        return strip.as_pointer()
    except (AttributeError, ReferenceError):
        return id(strip)


def _channel_conflict(sequence_editor, channel, start, end, ignored_keys) -> bool:
    for strip in top_strips(sequence_editor):
        if strip.channel != channel or _strip_key(strip) in ignored_keys:
            continue
        strip_start = strip.frame_final_start
        strip_end = strip_start + strip.frame_final_duration
        if strip_start < end and strip_end > start:
            return True
    return False


def _unique_channel(strips):
    channels = {strip.channel for strip in strips}
    return next(iter(channels)) if len(channels) == 1 else None


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


def _unpack(job):
    if len(job) == 2:
        strip, timeline = job
        return strip, timeline, None
    if len(job) == 3:
        strip, timeline, audio_strip = job
        return strip, timeline, audio_strip
    strip, timeline, _audio_timeline, audio_strip = job
    return strip, timeline, audio_strip


def _plan_group(sequence_editor, group, fps: float, channels):
    """Return (new_strips, originals, cursor_end)."""
    group = sorted(group, key=lambda job: job[0].frame_final_start)
    shared_cursor = len(group) > 1
    cursor = int(round(group[0][0].frame_final_start))
    movie_channel, sound_channel = channels
    planned: list[NewStrip] = []
    originals = []

    for strip, timeline, audio_strip in group:
        originals.append(strip)
        if audio_strip is not None and audio_strip is not strip:
            originals.append(audio_strip)
        if has_speed_control(sequence_editor, strip):
            raise BuildError(
                f"{strip.name!r} has a Speed Control effect; flatten it first"
            )
        if (
            audio_strip is not None
            and audio_strip is not strip
            and has_speed_control(sequence_editor, audio_strip)
        ):
            raise BuildError(
                f"{audio_strip.name!r} has a Speed Control effect; flatten it first"
            )

        if not shared_cursor:
            cursor = int(round(strip.frame_final_start))

        if strip.type == "MOVIE":
            video_clips = v3.kept(v3.single_video_layer(timeline))
            if not video_clips:
                continue
            embedded_audio = bool(v3.kept(v3.single_audio_layer(timeline)))
            audio_owner = (
                audio_strip
                if audio_strip is not None
                else (strip if embedded_audio else None)
            )
            video_total = int(round(strip.frame_duration))
            video_source = source_path(strip)
            origin_start = int(round(strip.frame_final_start))
            origin_offset = int(round(strip.frame_offset_start))

            if audio_owner is not None:
                audio_source = source_path(audio_owner)
                audio_lo = int(round(audio_owner.frame_offset_start))
                audio_hi = audio_lo + int(round(audio_owner.frame_final_duration))
                audio_total = int(round(audio_owner.frame_duration))
                audio_owner_start = int(round(audio_owner.frame_final_start))

            for index, clip in enumerate(video_clips):
                window = _window(clip, strip, video_total, timeline.fps, fps)
                if window is None:
                    continue
                planned.append(
                    NewStrip(
                        kind="MOVIE",
                        source=video_source,
                        channel=movie_channel,
                        new_start=cursor,
                        src_offset=window.src_offset,
                        duration=window.duration,
                        offset_end=video_total - window.src_offset - window.duration,
                        name=f"{strip.name} cut {index + 1}",
                    )
                )

                if audio_owner is not None:
                    source_start = origin_start + (window.src_offset - origin_offset)
                    if audio_owner is strip:
                        audio_offset = window.src_offset
                    else:
                        audio_offset = audio_lo + (source_start - audio_owner_start)
                    start = max(audio_offset, audio_lo, 0)
                    end = min(audio_offset + window.duration, audio_hi, audio_total)
                    if end > start:
                        planned.append(
                            NewStrip(
                                kind="SOUND",
                                source=audio_source,
                                channel=sound_channel,
                                new_start=cursor + (start - audio_offset),
                                src_offset=start,
                                duration=end - start,
                                offset_end=audio_total - start - (end - start),
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
                window = _window(clip, strip, source_total, timeline.fps, fps)
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
    """Jobs are (strip, timeline[, audio_strip])."""
    if not jobs:
        raise BuildError("Select at least one movie or sound strip")

    jobs = [_unpack(job) for job in jobs]
    scene = context.scene
    sequence_editor = scene.sequence_editor
    if sequence_editor is None:
        raise BuildError("Open the Video Sequencer first")

    fps_before = scene_fps(scene)
    old_fps = scene.render.fps
    old_fps_base = scene.render.fps_base

    base_timeline = None
    movie_fps = None
    for strip, timeline, _ in jobs:
        v3.validate_supported(timeline)
        if strip.type == "MOVIE":
            if movie_fps is None:
                movie_fps = timeline.fps
                base_timeline = timeline
            elif abs(timeline.fps - movie_fps) > 1e-4:
                raise BuildError(
                    "Selected movie sources have different frame rates; "
                    "process them separately"
                )
    if base_timeline is None:
        base_timeline = jobs[0][1]
    timebase = base_timeline.fps

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
            set_scene_fps(scene, base_timeline.timebase_num, base_timeline.timebase_den)
        fps = scene_fps(scene)

        selected = [strip for strip, _, _ in jobs]
        ripple = (
            settings.ripple
            and len(jobs) > 1
            and len({strip.channel for strip in selected}) == 1
        )
        groups = [jobs] if ripple else [[job] for job in jobs]
        groups.sort(key=lambda group: min(strip.channel for strip, _, _ in group))

        group_originals = []
        for group in groups:
            originals = []
            for strip, _, audio_strip in group:
                originals.append(strip)
                if audio_strip is not None and audio_strip is not strip:
                    originals.append(audio_strip)
            group_originals.append(originals)
        selected_keys = {
            _strip_key(strip) for originals in group_originals for strip in originals
        }

        next_channel = find_free_channel(sequence_editor)
        reserved = set()
        group_channels = []

        def allocate():
            nonlocal next_channel
            while next_channel in reserved:
                next_channel += 1
            channel = next_channel
            next_channel += 1
            reserved.add(channel)
            return channel

        for group, originals in zip(groups, group_originals):
            start = min(strip.frame_final_start for strip in originals)
            end = max(
                strip.frame_final_start + strip.frame_final_duration
                for strip in originals
            )
            movie_strips = [strip for strip in originals if strip.type == "MOVIE"]
            sound_strips = [strip for strip in originals if strip.type == "SOUND"]
            has_movie = bool(movie_strips)
            has_audio = bool(sound_strips) or any(
                audio_strip is not None
                or bool(v3.kept(v3.single_audio_layer(timeline)))
                for strip, timeline, audio_strip in group
                if strip.type == "MOVIE"
            )

            movie_channel = None
            if has_movie:
                original = (
                    None if settings.keep_original else _unique_channel(movie_strips)
                )
                if (
                    original is not None
                    and original not in reserved
                    and not _channel_conflict(
                        sequence_editor, original, start, end, selected_keys
                    )
                ):
                    movie_channel = original
                    reserved.add(original)
                else:
                    movie_channel = allocate()

            sound_channel = None
            if has_audio:
                original = (
                    None if settings.keep_original else _unique_channel(sound_strips)
                )
                if (
                    original is not None
                    and original != movie_channel
                    and original not in reserved
                    and not _channel_conflict(
                        sequence_editor, original, start, end, selected_keys
                    )
                ):
                    sound_channel = original
                    reserved.add(original)
                else:
                    sound_channel = allocate()
            group_channels.append((movie_channel, sound_channel))

        if max(reserved, default=0) > MAX_CHANNEL:
            raise BuildError(f"Not enough free VSE channels (max {MAX_CHANNEL})")

        plan = []
        originals = []
        seen_originals = set()
        for group, channels in zip(groups, group_channels):
            movie_channel, sound_channel = channels
            if movie_channel is None and sound_channel is None:
                continue
            group_plan, group_originals, _ = _plan_group(
                sequence_editor, group, fps, channels
            )
            plan.extend(group_plan)
            for strip in group_originals:
                key = _strip_key(strip)
                if key not in seen_originals:
                    seen_originals.add(key)
                    originals.append(strip)

        if not plan:
            raise BuildError("auto-editor kept nothing for the selected strip(s)")

        staging_base = max(
            (
                strip.frame_final_start + strip.frame_final_duration
                for strip in strip_collection
            ),
            default=0,
        )
        staging_base += (
            max((item.src_offset + item.duration for item in plan), default=0) + 1000
        )
        staging_next = {}
        for item in plan:
            start = staging_next.get(item.channel, staging_base)
            if item.kind == "MOVIE":
                strip = strip_collection.new_movie(
                    item.name, item.source, item.channel, frame_start=start
                )
            else:
                strip = strip_collection.new_sound(
                    item.name, item.source, item.channel, frame_start=start
                )
            created.append(strip)
            _trim(strip, start, item.src_offset, item.duration)
            staging_next[item.channel] = start + item.duration + 1

        if settings.keep_original:
            for strip in originals:
                strip.mute = True
        else:
            for strip in originals:
                try:
                    strip_collection.remove(strip)
                except (ReferenceError, RuntimeError):
                    pass

        for strip, item in zip(created, plan):
            strip.frame_start = item.new_start - item.src_offset

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
