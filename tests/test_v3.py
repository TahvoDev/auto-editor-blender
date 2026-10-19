import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v3", ROOT / "v3.py")
v3 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = v3
SPEC.loader.exec_module(v3)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.v3"


class V3Tests(unittest.TestCase):
    def test_load_fixture(self):
        timeline = v3.load(str(FIXTURE))
        self.assertAlmostEqual(timeline.fps, 30000 / 1001)
        self.assertEqual(len(v3.single_video_layer(timeline)), 3)
        self.assertEqual(len(v3.single_audio_layer(timeline)), 1)
        self.assertEqual(len(v3.kept(v3.single_video_layer(timeline))), 2)

    def test_speed_and_cut(self):
        timeline = v3.load(str(FIXTURE))
        clips = v3.single_video_layer(timeline)
        self.assertEqual(clips[1].speed, 2.0)
        self.assertTrue(clips[2].cut)
        self.assertFalse(clips[0].cut)

    def test_validate_rejects_speed(self):
        timeline = v3.load(str(FIXTURE))
        with self.assertRaises(v3.V3Error):
            v3.validate_supported(timeline)

    def test_validate_rejects_unknown_effect(self):
        raw = json.loads(FIXTURE.read_text())
        raw["v"][0][0]["effects"] = ["text:hello"]
        with tempfile.NamedTemporaryFile("w", suffix=".v3", delete=False) as handle:
            json.dump(raw, handle)
            path = handle.name
        try:
            timeline = v3.load(path)
            with self.assertRaises(v3.V3Error):
                v3.validate_supported(timeline)
        finally:
            Path(path).unlink()

    def test_rejects_transitions(self):
        raw = json.loads(FIXTURE.read_text())
        raw["transitions"] = {"v": [[{"kind": "dissolve", "at": 10, "dur": 4, "alignment": "center"}]], "a": []}
        with tempfile.NamedTemporaryFile("w", suffix=".v3", delete=False) as handle:
            json.dump(raw, handle)
            path = handle.name
        try:
            with self.assertRaises(v3.V3Error):
                v3.load(path)
        finally:
            Path(path).unlink()

    def test_rejects_multiple_video_layers(self):
        raw = json.loads(FIXTURE.read_text())
        raw["v"].append([{"src": "other.mp4", "start": 0, "dur": 10, "offset": 0, "stream": 0}])
        with tempfile.NamedTemporaryFile("w", suffix=".v3", delete=False) as handle:
            json.dump(raw, handle)
            path = handle.name
        try:
            timeline = v3.load(path)
            with self.assertRaises(v3.V3Error):
                v3.single_video_layer(timeline)
        finally:
            Path(path).unlink()


if __name__ == "__main__":
    unittest.main()
