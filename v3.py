from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class V3Error(Exception):
    pass


@dataclass(frozen=True)
class Clip:
    src: str
    start: int
    dur: int
    offset: int
    stream: int
    effects: tuple[str, ...]

    @property
    def cut(self) -> bool:
        return "cut" in self.effects

    @property
    def speed(self) -> float:
        value = 1.0
        for effect in self.effects:
            if effect == "cut":
                continue
            if effect.startswith("speed:") or effect.startswith("varispeed:"):
                try:
                    value *= float(effect.split(":", 1)[1])
                except (IndexError, ValueError) as exc:
                    raise V3Error(f"Invalid speed effect: {effect!r}") from exc
        return value

    @property
    def unsupported_effects(self) -> tuple[str, ...]:
        return tuple(
            effect
            for effect in self.effects
            if effect != "cut"
            and not effect.startswith("speed:")
            and not effect.startswith("varispeed:")
        )


@dataclass(frozen=True)
class Timeline:
    path: str
    timebase_num: int
    timebase_den: int
    video: tuple[tuple[Clip, ...], ...]
    audio: tuple[tuple[Clip, ...], ...]

    @property
    def fps(self) -> float:
        return self.timebase_num / self.timebase_den


def _natural(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise V3Error(f"{field} must be a non-negative integer")
    return value


def _parse_timebase(value: Any) -> tuple[int, int]:
    if not isinstance(value, str) or "/" not in value:
        raise V3Error("timebase must look like '30/1'")
    num_text, den_text = value.split("/", 1)
    try:
        num = int(num_text)
        den = int(den_text)
    except ValueError as exc:
        raise V3Error(f"Invalid timebase: {value!r}") from exc
    if num <= 0 or den <= 0:
        raise V3Error(f"Invalid timebase: {value!r}")
    return num, den


def _resolve_src(src: str, base_dir: Path) -> str:
    path = Path(src)
    if path.is_absolute():
        return str(path)
    relative_to_v3 = (base_dir / path).resolve()
    if relative_to_v3.exists():
        return str(relative_to_v3)
    return str(path.resolve())


def _parse_clip(value: Any, base_dir: Path) -> Clip:
    if not isinstance(value, dict):
        raise V3Error("clip must be an object")
    src = value.get("src")
    if not isinstance(src, str) or not src:
        raise V3Error("clip.src must be a non-empty path")
    effects = value.get("effects", [])
    if not isinstance(effects, list) or not all(isinstance(e, str) for e in effects):
        raise V3Error("clip.effects must be a list of strings")
    return Clip(
        src=_resolve_src(src, base_dir),
        start=_natural(value.get("start"), "clip.start"),
        dur=_natural(value.get("dur"), "clip.dur"),
        offset=_natural(value.get("offset"), "clip.offset"),
        stream=_natural(value.get("stream", 0), "clip.stream"),
        effects=tuple(effects),
    )


def _parse_layers(value: Any, base_dir: Path, name: str) -> tuple[tuple[Clip, ...], ...]:
    if not isinstance(value, list):
        raise V3Error(f"{name} must be a list of layers")
    layers = []
    for layer in value:
        if not isinstance(layer, list):
            raise V3Error(f"{name} layer must be a list")
        layers.append(tuple(_parse_clip(clip, base_dir) for clip in layer))
    return tuple(layers)


def load(path: str) -> Timeline:
    file_path = Path(path)
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V3Error(f"Could not read v3 timeline: {exc}") from exc
    if not isinstance(raw, dict):
        raise V3Error("v3 timeline must be a JSON object")
    if raw.get("version") != "3":
        raise V3Error(f"Unsupported v3 version: {raw.get('version')!r}")

    transitions = raw.get("transitions")
    if transitions:
        if not isinstance(transitions, dict):
            raise V3Error("v3 transitions are not supported yet")
        if any(value for value in transitions.values()):
            raise V3Error("v3 transitions are not supported yet")

    num, den = _parse_timebase(raw.get("timebase"))
    return Timeline(
        path=str(file_path),
        timebase_num=num,
        timebase_den=den,
        video=_parse_layers(raw.get("v"), file_path.parent, "v"),
        audio=_parse_layers(raw.get("a"), file_path.parent, "a"),
    )


def kept(layer: tuple[Clip, ...]) -> tuple[Clip, ...]:
    return tuple(clip for clip in layer if not clip.cut)


def single_video_layer(timeline: Timeline) -> tuple[Clip, ...]:
    layers = [layer for layer in timeline.video if layer]
    if len(layers) != 1:
        raise V3Error(
            f"Expected one video layer, found {len(layers)}. "
            "Multiple video layers are not supported yet."
        )
    return layers[0]


def single_audio_layer(timeline: Timeline) -> tuple[Clip, ...]:
    layers = [layer for layer in timeline.audio if layer]
    if len(layers) > 1:
        raise V3Error(
            f"Expected at most one audio layer, found {len(layers)}. "
            "Multi-track audio is not supported yet."
        )
    return layers[0] if layers else ()


def validate_supported(timeline: Timeline) -> None:
    for layer in timeline.video + timeline.audio:
        for clip in layer:
            if clip.unsupported_effects:
                raise V3Error(
                    "Unsupported v3 effect(s): " + ", ".join(clip.unsupported_effects)
                )
            if clip.speed != 1.0:
                raise V3Error("Speed changes are not supported in 0.1")
