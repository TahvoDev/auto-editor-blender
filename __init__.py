from __future__ import annotations

import bpy

from . import ops, vse


ADDON_ID = __package__

bl_info = {
    "name": "Auto-Editor Blender",
    "author": "Jenny",
    "version": (0, 1, 1),
    "blender": (4, 2, 0),
    "location": "Sequencer > Sidebar > Auto-Editor",
    "description": "Remove silence gaps from VSE strips without touching source files",
    "category": "Sequencer",
}


class AutoEditorSettings(bpy.types.PropertyGroup):
    preset: bpy.props.EnumProperty(
        name="Preset",
        items=[
            ("SILENCE", "Cut silence", "Use auto-editor's audio silence detection"),
            ("MOTION", "Cut motionless", "Use auto-editor's motion detection"),
        ],
        default="SILENCE",
    )
    audio_threshold: bpy.props.FloatProperty(
        name="Silence threshold (%)",
        description="Audio level below which a section is treated as silent",
        default=4.0,
        min=0.0,
        max=100.0,
    )
    motion_threshold: bpy.props.FloatProperty(
        name="Motion threshold (%)",
        description="Amount of motion below which a section is treated as motionless",
        default=2.0,
        min=0.0,
        max=100.0,
    )
    margin: bpy.props.FloatProperty(
        name="Margin (s)",
        description="Padding kept around active sections",
        default=0.2,
        min=0.0,
        max=10.0,
    )
    match_fps: bpy.props.BoolProperty(
        name="Match scene FPS",
        description="Set the scene FPS to the source FPS before building strips",
        default=True,
    )
    ripple: bpy.props.BoolProperty(
        name="Ripple selected strips",
        description=(
            "When several selected strips share a channel, place all kept clips "
            "back to back"
        ),
        default=True,
    )
    keep_original: bpy.props.BoolProperty(
        name="Keep originals (muted)",
        description="Keep the original strips on their channels, muted",
        default=False,
    )
    extra_args: bpy.props.StringProperty(
        name="Extra arguments",
        description="Extra arguments appended to the auto-editor command",
        default="",
    )


class AutoEditorPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    exe_path: bpy.props.StringProperty(
        name="auto-editor executable",
        subtype="FILE_PATH",
    )
    version: bpy.props.StringProperty(name="Version", default="")

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="auto-editor binary", icon="CONSOLE")
        row = box.row(align=True)
        row.prop(self, "exe_path", text="")
        row.operator("auto_editor.detect_binary", text="Detect", icon="VIEWZOOM")
        row.operator("auto_editor.test_binary", text="Test", icon="CHECKMARK")
        if self.version:
            box.label(text=self.version, icon="CHECKMARK")
        else:
            box.label(text="Not detected yet", icon="INFO")
        box.operator(
            "wm.url_open", text="Download auto-editor", icon="URL"
        ).url = "https://github.com/WyattBlue/auto-editor/releases"
        layout.label(
            text="The binary is not bundled. Install it separately and put it on PATH.",
            icon="INFO",
        )


class AUTO_EDITOR_PT_main(bpy.types.Panel):
    bl_label = "Auto-Editor"
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Auto-Editor"

    def draw(self, context):
        layout = self.layout
        prefs = ops._prefs(context)
        exe = ops.find_executable(prefs)
        selected = [
            strip
            for strip in vse.selected_strips(context)
            if strip.type in {"MOVIE", "SOUND"}
        ]
        movies = [strip for strip in selected if strip.type == "MOVIE"]
        sounds = [strip for strip in selected if strip.type == "SOUND"]

        box = layout.box()
        if exe:
            label = prefs.version if prefs is not None and prefs.version else exe
            box.label(text=label, icon="CHECKMARK")
        else:
            box.label(text="auto-editor not found", icon="ERROR")
            box.operator(
                "preferences.addon_show",
                text="Open Preferences",
                icon="PREFERENCES",
            ).module = ADDON_ID

        layout.label(text=f"Selected: {len(movies)} movie, {len(sounds)} sound")
        if not selected:
            layout.label(text="Select movie strip(s) in the VSE", icon="INFO")
        elif movies and sounds:
            layout.label(text="Select movie or sound strips, not both", icon="ERROR")

        row = layout.row()
        row.scale_y = 1.5
        row.enabled = bool(exe and selected and not (movies and sounds))
        row.operator("auto_editor.remove_gaps", text="Remove Gaps", icon="SEQUENCE")

        layout.separator()
        settings = context.scene.auto_editor_settings
        layout.prop(settings, "preset")
        if settings.preset == "SILENCE":
            layout.prop(settings, "audio_threshold")
        else:
            layout.prop(settings, "motion_threshold")
        layout.prop(settings, "margin")
        layout.prop(settings, "match_fps")
        layout.prop(settings, "ripple")
        layout.prop(settings, "keep_original")


class AUTO_EDITOR_PT_advanced(bpy.types.Panel):
    bl_label = "Advanced"
    bl_parent_id = "AUTO_EDITOR_PT_main"
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.auto_editor_settings
        layout.prop(settings, "extra_args")

        box = layout.box()
        box.label(text="Command preview", icon="CONSOLE")
        prefs = ops._prefs(context)
        exe = ops.find_executable(prefs)
        selected = [
            strip
            for strip in vse.selected_strips(context)
            if strip.type in {"MOVIE", "SOUND"}
        ]
        if exe and selected:
            try:
                source = vse.source_path(selected[0])
                options = ops.RunOptions(
                    preset=settings.preset,
                    audio_threshold=settings.audio_threshold,
                    motion_threshold=settings.motion_threshold,
                    margin=settings.margin,
                    extra_args=settings.extra_args,
                )
                args = ops.build_args(exe, source, "<temp>.v3", options)
                box.label(text=ops.command_preview(args))
            except Exception as exc:
                box.label(text=str(exc), icon="ERROR")
        else:
            box.label(text="-")


classes = (
    AutoEditorPreferences,
    AutoEditorSettings,
    AUTO_EDITOR_PT_main,
    AUTO_EDITOR_PT_advanced,
) + ops.classes


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.auto_editor_settings = bpy.props.PointerProperty(
        type=AutoEditorSettings
    )
    try:
        prefs = ops._prefs(bpy.context)
        if prefs is not None:
            path = ops.find_executable(prefs)
            if path:
                if not prefs.exe_path:
                    prefs.exe_path = path
                if not prefs.version:
                    try:
                        prefs.version = ops.run_version(path)
                    except Exception:
                        pass
    except Exception:
        pass


def unregister():
    if hasattr(bpy.types.Scene, "auto_editor_settings"):
        del bpy.types.Scene.auto_editor_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
