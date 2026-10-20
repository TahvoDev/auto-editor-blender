# Auto-Editor Blender

A small Blender VSE add-on that removes silence gaps from selected movie and
sound strips.

Select strips in the Video Sequencer, click **Cut Silence** (or **Cut
Motionless**), and the add-on:

1. runs the official [`auto-editor`](https://github.com/WyattBlue/auto-editor) binary
   on each source file,
2. reads auto-editor's `.v3` timeline,
3. replaces the selected strips with trimmed movie/sound strips, keeping the
   original channels where possible and placing them back to back,
4. leaves the source files untouched.

The edit is a normal Blender undo step. `Ctrl+Z` restores the original strips.

## Requirements

- Blender **4.2+** (tested with 5.0.1).
- The official **auto-editor** binary, version **30.1.0** or newer
  (tested with 30.1.0 and 31.7.2).
  - Download: <https://github.com/WyattBlue/auto-editor/releases>
  - Put it on `PATH` as `auto-editor`, or set its path in
    **Edit > Preferences > Add-ons > Auto-Editor Blender**.

The binary is **not** bundled with the add-on.

## Install

Build an extension zip and install it from disk:

```bash
blender --command extension build
```

Then **Edit > Preferences > Get Extensions > Install from Disk**.

For development, add the parent directory of this folder to
**Preferences > File Paths > Script Directories**, or copy/symlink this folder into
a Blender extension/add-on directory.

## Use

1. In the Video Sequencer, select one or more **movie** strips. You may also
   select their overlapping/linked sound strips; the pair is handled as one job.
2. Open the sidebar and choose the **Auto-Editor** tab.
3. Pick **Cut silence** or **Cut motionless**, adjust the threshold if needed.
4. Click **Cut Silence** / **Cut Motionless**.

If a selected sound strip overlaps a selected movie strip (Blender's linked
`*.001` sound strip is the common case), the pair is joined: video comes from the
movie, audio from the sound. Sound strips with no overlapping movie are processed
on their own. The **Advanced** subpanel has extra CLI arguments and a command
preview.

If several selected strips are on the same channel and **Ripple selected strips**
is enabled, their kept clips are placed back to back as one continuous sequence.
Otherwise each strip is processed in place.

The original strips are removed by default. Enable **Keep originals (muted)** to
keep them on their channels, muted.

Analysis runs in the background: the sidebar shows progress and a **Cancel**
button, and the editor stays responsive. Rebuilt video strips stay on their
original channel when it is free over the edited range. Paired sound stays on its
own channel; embedded movie audio goes to a free channel. With **Keep originals**
enabled, new strips use free channels so they do not overlap the muted originals.

## Limits in 0.1

- One video layer and at most one audio layer per source.
- No speed/`varispeed`, transitions, overlay/`pos`, or other v3 effects.
- No multi-stream audio selection.
- The add-on matches the scene FPS to the source FPS by default. If two selected
  sources have different frame rates, process them separately.
- Movie strips that already have a Speed Control effect are not supported.
- Movie/sound pairing is heuristic: same source file, or at least 80% overlap.

## Development

The v3 parser is plain Python and can be tested without Blender:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

The Blender smoke test runs a real analysis and rebuild on a video you provide:

```bash
AUTO_EDITOR_TEST_VIDEO=/path/to/video.mp4 \
  blender -b --factory-startup --python tests/blender_smoke.py
```

The pairing test covers movie+sound pairing, a separate audio file, and
sound-only processing:

```bash
AUTO_EDITOR_TEST_VIDEO=/path/to/video.mp4 \
  blender -b --factory-startup --python tests/blender_pair.py
```

The background test exercises the non-modal operator, progress polling, channel
preservation, and cleanup:

```bash
AUTO_EDITOR_TEST_VIDEO=/path/to/video.mp4 \
  blender -b --factory-startup --python tests/blender_background.py
```

All three tests are skipped when `AUTO_EDITOR_TEST_VIDEO` is not set.

## License

GPL-3.0-or-later. auto-editor is Unlicense and is not included.
