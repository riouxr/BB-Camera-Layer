import bpy
import os
import glob
import shutil
import tempfile
from bpy.props import StringProperty, PointerProperty, CollectionProperty, BoolProperty
from bpy.types import PropertyGroup, Panel, Operator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _camera_poll(self, obj):
    return obj.type == 'CAMERA'


def _is_multilayer_exr(scene):
    fmt = scene.render.image_settings
    if hasattr(fmt, 'media_type'):           # Blender 5.0+
        return fmt.media_type == 'MULTI_LAYER_IMAGE'
    return fmt.file_format == 'OPEN_EXR_MULTILAYER'  # pre-5.0


def _set_image_format(image_settings, file_format, codec=None):
    """Set format on ImageFormatSettings.

    Blender 5.0+ uses media_type to gate which file_format values are valid:
      'IMAGE'     – regular image formats (PNG, OPEN_EXR, TIFF …)
      'VIDEO'     – video formats (FFMPEG …)
      'MULTI_LAYER_IMAGE'– multilayer EXR (no separate file_format needed)

    Pre-5.0 uses a flat file_format enum that includes OPEN_EXR_MULTILAYER.
    """
    if hasattr(image_settings, 'media_type'):
        if file_format in ('OPEN_EXR_MULTILAYER', 'MULTI_LAYER_IMAGE'):
            image_settings.media_type = 'MULTI_LAYER_IMAGE'
            return  # media_type alone is sufficient in 5.0+
        else:
            image_settings.media_type = 'IMAGE'
            image_settings.file_format = file_format
    else:
        image_settings.file_format = file_format   # pre-5.0
    if codec is not None:
        image_settings.exr_codec = codec


def _filepath_with_layer(base, layer_name):
    """Insert /<LayerName> subfolder before the filename stub."""
    base = base.rstrip('/\\')
    head, tail = os.path.split(base)
    if not head:
        head, tail = base, ''
    return os.path.join(head, layer_name, tail) if tail else os.path.join(head, layer_name) + os.sep


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

class BB_LayerCameraItem(PropertyGroup):
    layer_name: StringProperty(name="Layer Name")
    camera: PointerProperty(name="Camera", type=bpy.types.Object, poll=_camera_poll)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def _sync(scene):
    layer_names = {vl.name for vl in scene.view_layers}
    existing    = {item.layer_name for item in scene.bb_layer_cameras}
    for name in layer_names - existing:
        item = scene.bb_layer_cameras.add()
        item.layer_name = name
    stale = [i for i, item in enumerate(scene.bb_layer_cameras)
             if item.layer_name not in layer_names]
    for i in reversed(stale):
        scene.bb_layer_cameras.remove(i)


def _get_camera(scene, layer_name):
    for item in scene.bb_layer_cameras:
        if item.layer_name == layer_name:
            return item.camera
    return None


# ---------------------------------------------------------------------------
# Keymap
# ---------------------------------------------------------------------------

_keymaps = []


def _register_keymap():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if not kc:
        return
    km  = kc.keymaps.new(name="Window", space_type='EMPTY')
    kmi = km.keymap_items.new("bb.render_all_layers", type='F12', value='PRESS')
    _keymaps.append((km, kmi))


def _unregister_keymap():
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()


def _on_intercept_toggle(self, context):
    if context.scene.bb_intercept_f12:
        _register_keymap()
    else:
        _unregister_keymap()


# ---------------------------------------------------------------------------
# Operator – separate EXRs (non-multilayer)
# ---------------------------------------------------------------------------

class BB_OT_SyncLayers(Operator):
    bl_idname  = "bb.sync_layer_cameras"
    bl_label   = "Sync Layers"
    bl_description = "Sync list with current View Layers"

    def execute(self, context):
        _sync(context.scene)
        return {'FINISHED'}


class BB_OT_RenderAllLayers(Operator):
    bl_idname      = "bb.render_all_layers"
    bl_label       = "BB Render All Layers"
    bl_description = "Render each enabled View Layer with its assigned camera into a per-layer subfolder"

    @classmethod
    def poll(cls, context):
        return not _is_multilayer_exr(context.scene)

    def execute(self, context):
        global _bb_merging
        _bb_merging = True
        scene = context.scene
        _sync(scene)

        orig_camera   = scene.camera
        orig_single   = scene.render.use_single_layer
        orig_layer    = context.window.view_layer
        orig_filepath = scene.render.filepath
        errors = []

        scene.render.use_single_layer = True
        enabled_layers = [vl for vl in scene.view_layers if vl.use]

        for vl in enabled_layers:
            cam = _get_camera(scene, vl.name)
            if cam:
                scene.camera = cam
            context.window.view_layer = vl
            scene.render.filepath     = _filepath_with_layer(orig_filepath, vl.name)
            try:
                bpy.ops.render.render(write_still=True)
            except Exception as e:
                errors.append(f"{vl.name}: {e}")

        _bb_merging = False
        scene.camera                  = orig_camera
        scene.render.use_single_layer = orig_single
        scene.render.filepath         = orig_filepath
        context.window.view_layer     = orig_layer

        if errors:
            self.report({'WARNING'}, "Some layers failed: " + " | ".join(errors))
        else:
            self.report({'INFO'}, f"Rendered {len(enabled_layers)} layer(s).")

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Operator – render & merge into multilayer EXR
# ---------------------------------------------------------------------------

class BB_OT_RenderMergeMultilayer(Operator):
    bl_idname      = "bb.render_merge_multilayer"
    bl_label       = "BB Render & Merge Multilayer EXR"
    bl_description = (
        "Render each View Layer separately with its assigned camera, "
        "then merge into a single multilayer EXR via compositor"
    )

    @classmethod
    def poll(cls, context):
        return _is_multilayer_exr(context.scene)

    def execute(self, context):
        global _bb_merging
        scene  = context.scene
        _sync(scene)

        orig_layer    = context.window.view_layer
        orig_filepath = scene.render.filepath
        tmp_dir       = tempfile.mkdtemp(prefix="bb_layer_cams_")
        layer_paths   = {}
        errors        = []
        out_path      = ""

        _bb_merging = True   # suppress _render_pre for all renders in this operator

        # ── Copy scene so we can freely change render settings ────────────
        # scene.copy() gives us independent image_settings not locked to
        # the original format, avoiding the enum restriction on the live scene.
        tmp_scene = scene.copy()
        _set_image_format(tmp_scene.render.image_settings, 'OPEN_EXR', codec='ZIP')
        tmp_scene.render.use_single_layer = True

        enabled_layers = [vl for vl in scene.view_layers if vl.use]

        try:
            # ── Phase 1: render each layer to temp single EXR ─────────────
            # Disable use_single_layer — instead we disable all layers in
            # tmp_scene except the target, which is unambiguous.
            tmp_scene.render.use_single_layer = False

            for vl in enabled_layers:
                cam = _get_camera(scene, vl.name)
                tmp_scene.camera = cam if cam else scene.camera

                # Enable only the target layer; disable all others
                for tvl in tmp_scene.view_layers:
                    tvl.use = (tvl.name == vl.name)

                safe = vl.name.replace(" ", "_").replace(".", "_")
                tmp_scene.render.filepath = os.path.join(tmp_dir, safe + "_")

                try:
                    with context.temp_override(scene=tmp_scene):
                        bpy.ops.render.render(write_still=True)
                    matches = sorted(glob.glob(os.path.join(tmp_dir, safe + "_*.exr")))
                    if matches:
                        layer_paths[vl.name] = matches[-1]
                    else:
                        errors.append(f"{vl.name}: rendered file not found")
                except Exception as e:
                    errors.append(f"{vl.name}: {e}")

            if not layer_paths:
                self.report({'ERROR'}, "No layers rendered. " + " | ".join(errors))
                return {'CANCELLED'}

            # ── Phase 2: compositor merge into multilayer EXR ─────────────
            merge_errors = self._merge(context, scene, layer_paths, orig_filepath)
            errors.extend(merge_errors)

        finally:
            _bb_merging = False
            bpy.data.scenes.remove(tmp_scene)
            context.window.view_layer = orig_layer

            for img in list(bpy.data.images):
                if bpy.path.abspath(img.filepath).startswith(tmp_dir):
                    bpy.data.images.remove(img)

            shutil.rmtree(tmp_dir, ignore_errors=True)

        # Filter out the INFO path message we stashed in errors
        info_msgs = [e for e in errors if e.startswith("INFO:")]
        real_errs = [e for e in errors if not e.startswith("INFO:")]

        if real_errs:
            self.report({'WARNING'}, " | ".join(real_errs))
        else:
            path_str = info_msgs[0].replace("INFO: ", "") if info_msgs else ""
            self.report({'INFO'}, f"Merged {len(layer_paths)} layer(s) → multilayer EXR  {path_str}")

        return {'FINISHED'}

    # ── Merge helper ──────────────────────────────────────────────────────

    def _merge(self, context, scene, layer_paths, orig_filepath):
        """Merge temp single-layer EXRs into one multilayer EXR via compositor."""
        errors      = []
        is_5x       = hasattr(scene, 'compositing_node_group')
        abs_orig    = bpy.path.abspath(orig_filepath)
        out_dir     = os.path.dirname(abs_orig) or tempfile.gettempdir()
        # Use a predictable output name. In Blender 5.x the File Output node
        # does not append a frame number for still renders → bb_multilayer.exr
        out_name = "bb_multilayer"
        out_path = os.path.join(out_dir, f"{out_name}.exr")
        errors.append(f"INFO: output → {out_path}")

        # ── Save compositor state ─────────────────────────────────────────
        orig_use_compositing = scene.render.use_compositing
        orig_use_sequencer   = scene.render.use_sequencer
        scene.render.use_compositing = True
        scene.render.use_sequencer   = False

        if is_5x:
            orig_comp_group = scene.compositing_node_group
            tree = bpy.data.node_groups.new("BB_Merge_Tmp", "CompositorNodeTree")
            scene.compositing_node_group = tree
            grp_out = tree.nodes.new('NodeGroupOutput')
            grp_out.location = (800, 0)
            tree.interface.new_socket(
                name="Image", in_out="OUTPUT", socket_type="NodeSocketColor"
            )
        else:
            orig_comp_group  = None
            orig_use_nodes   = getattr(scene, 'use_nodes', False)
            scene.use_nodes  = True
            tree             = scene.node_tree
            for n in list(tree.nodes):
                tree.nodes.remove(n)

        # ── File Output node ──────────────────────────────────────────────
        fo           = tree.nodes.new('CompositorNodeOutputFile')
        fo.location  = (600, 0)

        if is_5x:
            # Blender 5.0+ API
            fo.directory = out_dir
            fo.file_name = out_name
            if hasattr(fo.format, 'media_type'):
                fo.format.media_type = 'MULTI_LAYER_IMAGE'
        else:
            # Pre-5.0 API
            fo.base_path = out_dir
            fo.format.file_format = 'OPEN_EXR_MULTILAYER'

        fo.format.color_depth = '32'

        # ── Clear default slots ───────────────────────────────────────────
        slots = fo.file_output_items if is_5x else fo.layer_slots                 if hasattr(fo, 'layer_slots') else fo.file_slots
        if hasattr(slots, 'clear'):
            slots.clear()
        else:
            while slots:
                slots.remove(slots[0])

        # ── Add one slot per layer, wire Image node → File Output ─────────
        last_img_out = None
        y = 0
        for layer_name, exr_path in layer_paths.items():
            try:
                img = bpy.data.images.load(exr_path, check_existing=False)
            except Exception as e:
                errors.append(f"Load {layer_name}: {e}")
                continue

            img_node          = tree.nodes.new('CompositorNodeImage')
            img_node.image    = img
            img_node.location = (0, y)
            y -= 280

            slots.new('RGBA', layer_name)

            # Socket is named after the layer; fall back to last input
            sock = fo.inputs.get(layer_name) or (fo.inputs[-1] if fo.inputs else None)
            if sock is not None:
                try:
                    tree.links.new(img_node.outputs['Image'], sock)
                except Exception as e:
                    errors.append(f"Link error {layer_name}: {e}")
            else:
                errors.append(f"No socket found for {layer_name}")

            last_img_out = img_node.outputs['Image']

        if last_img_out is None:
            errors.append("No layers loaded — merge aborted")
        else:
            if is_5x and last_img_out:
                tree.links.new(last_img_out, grp_out.inputs[0])
            try:
                global _bb_merging
                _bb_merging = True
                bpy.ops.render.render(write_still=False)
            except Exception as e:
                errors.append(f"Compositor pass: {e}")
            finally:
                _bb_merging = False

        # ── Restore ───────────────────────────────────────────────────────
        scene.render.use_compositing = orig_use_compositing
        scene.render.use_sequencer   = orig_use_sequencer

        if is_5x:
            scene.compositing_node_group = orig_comp_group
            bpy.data.node_groups.remove(tree)
        else:
            for n in list(tree.nodes):
                tree.nodes.remove(n)
            scene.use_nodes = orig_use_nodes

        return errors


# ---------------------------------------------------------------------------
# Render handlers (active-layer swap for standard F12)
# ---------------------------------------------------------------------------

_saved_camera = {}
_bb_merging  = False   # skip _render_pre during merge compositor pass


@bpy.app.handlers.persistent
def _render_pre(scene, depsgraph=None):
    if _bb_merging or scene.bb_intercept_f12:
        return
    try:
        active_layer = bpy.context.view_layer
    except Exception:
        return
    if not active_layer:
        return
    cam = _get_camera(scene, active_layer.name)
    if cam:
        _saved_camera[scene.name] = scene.camera
        scene.camera = cam


@bpy.app.handlers.persistent
def _render_post(scene, depsgraph=None):
    orig = _saved_camera.pop(scene.name, None)
    if orig:
        scene.camera = orig


@bpy.app.handlers.persistent
def _load_post(*args):
    for scene in bpy.data.scenes:
        _sync(scene)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class BB_PT_LayerCameras(Panel):
    bl_label       = "BB Layer Cameras"
    bl_idname      = "BB_PT_layer_cameras"
    bl_space_type  = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context     = "view_layer"

    def draw(self, context):
        layout   = self.layout
        scene    = context.scene
        is_mlexr = _is_multilayer_exr(scene)

        # Top row: sync + F12 intercept (disabled for multilayer EXR)
        row = layout.row(align=True)
        row.operator("bb.sync_layer_cameras", text="", icon='FILE_REFRESH')
        sub = row.row(align=True)
        sub.enabled = not is_mlexr
        sub.prop(scene, "bb_intercept_f12", text="Intercept F12", toggle=True,
                 icon='RENDER_STILL')

        if not scene.bb_layer_cameras:
            layout.label(text="Click ↺ to sync View Layers", icon='INFO')
            return

        # Layer / camera list
        col = layout.column(align=True)
        for vl in scene.view_layers:
            item = next(
                (i for i in scene.bb_layer_cameras if i.layer_name == vl.name),
                None
            )
            row  = col.row(align=True)
            icon = 'RENDERLAYERS' if vl.use else 'LAYER_USED'
            row.label(text=vl.name, icon=icon)
            if item:
                sub = row.row(align=True)
                sub.enabled = vl.use
                sub.prop(item, "camera", text="")
            else:
                row.label(text="(click ↺)", icon='ERROR')

        layout.separator()

        if is_mlexr:
            layout.operator("bb.render_merge_multilayer", icon='RENDER_STILL')
            box = layout.box()
            box.scale_y = 0.7
            box.label(text="Renders layers separately, merges via compositor.", icon='INFO')
            box.label(text="Output → same folder as Output Path.")
        else:
            layout.operator("bb.render_all_layers", icon='RENDER_STILL')
            box = layout.box()
            box.scale_y = 0.7
            box.label(text="Output → <filepath>/<LayerName>/", icon='FILE_FOLDER')


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

classes = [
    BB_LayerCameraItem,
    BB_OT_SyncLayers,
    BB_OT_RenderAllLayers,
    BB_OT_RenderMergeMultilayer,
    BB_PT_LayerCameras,
]


def register():
    # Purge any stale handlers from previous text-editor runs by name.
    # Simple unregister() can't do this because function references differ.
    for hlist in (bpy.app.handlers.render_pre,
                  bpy.app.handlers.render_post,
                  bpy.app.handlers.render_cancel,
                  bpy.app.handlers.load_post):
        for h in list(hlist):
            if getattr(h, '__name__', '') in ('_render_pre', '_render_post', '_load_post'):
                hlist.remove(h)
    try:
        unregister()
    except Exception:
        pass

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.bb_layer_cameras = CollectionProperty(type=BB_LayerCameraItem)
    bpy.types.Scene.bb_intercept_f12 = BoolProperty(
        name="Intercept F12",
        description="F12 renders all layers with their assigned cameras",
        default=False,
        update=_on_intercept_toggle,
    )

    bpy.app.handlers.render_pre.append(_render_pre)
    bpy.app.handlers.render_post.append(_render_post)
    bpy.app.handlers.render_cancel.append(_render_post)
    bpy.app.handlers.load_post.append(_load_post)

    if hasattr(bpy.data, 'scenes'):
        for scene in bpy.data.scenes:
            _sync(scene)


def unregister():
    _unregister_keymap()

    for h, hlist in [
        (_render_pre,  bpy.app.handlers.render_pre),
        (_render_post, bpy.app.handlers.render_post),
        (_render_post, bpy.app.handlers.render_cancel),
        (_load_post,   bpy.app.handlers.load_post),
    ]:
        if h in hlist:
            hlist.remove(h)

    del bpy.types.Scene.bb_layer_cameras
    del bpy.types.Scene.bb_intercept_f12

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
