bl_info = {
    "name": "Quick Cake",
    "author": "Quick Cake",
    "version": (1, 3, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Quick Cake",
    "description": "Автоматизация запекания текстур с High Poly на Low Poly",
    "category": "Object",
}

import bpy
import bmesh
import os
import math
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    StringProperty, EnumProperty, FloatProperty,
    BoolProperty, PointerProperty
)


# ─────────────────────────────────────────────
#  Property group
# ─────────────────────────────────────────────

class QuickCakeProps(PropertyGroup):
    high_poly: PointerProperty(
        name="High Poly",
        type=bpy.types.Object
    )
    low_poly: PointerProperty(
        name="Low Poly",
        type=bpy.types.Object
    )
    bake_type: EnumProperty(
        name="Bake Type",
        items=[
            ('BASE_COLOR',         "Base Color",        "Диффузный цвет (Diffuse, Color only)"),
            ('NORMAL',             "Normal",            "Карта нормалей (Tangent Space)"),
            ('AMBIENT_OCCLUSION',  "Ambient Occlusion", "Карта AO"),
        ],
        default='BASE_COLOR',
        update=lambda self, ctx: _update_texture_name_and_cancel(self, ctx)
    )
    texture_name: StringProperty(
        name="Имя текстуры",
        default="",
        update=lambda self, ctx: _on_texture_name_change(self, ctx)
    )
    texture_size: EnumProperty(
        name="Размер текстуры",
        items=[
            ('32',   "32x32",     ""),
            ('64',   "64x64",     ""),
            ('128',  "128x128",   ""),
            ('256',  "256x256",   ""),
            ('512',  "512x512",   ""),
            ('1024', "1024x1024", ""),
            ('2048', "2048x2048", ""),
            ('4096', "4096x4096", ""),
        ],
        default='1024',
        update=lambda self, ctx: _on_texture_size_change(self, ctx)
    )
    show_projection: BoolProperty(
        name="Отступы проецирования",
        default=False
    )
    extrusion: FloatProperty(
        name="Extrusion",
        default=0.01, min=0.0, max=1.0,
        precision=4,
        update=lambda self, ctx: _on_auto_cancel(self, ctx)
    )
    max_ray_distance: FloatProperty(
        name="Max Ray Distance",
        default=0.02, min=0.0, max=10.0,
        precision=4,
        update=lambda self, ctx: _on_auto_cancel(self, ctx)
    )
    # internal state
    bake_done: BoolProperty(default=False)
    show_result_used: BoolProperty(default=False)
    cancel_used: BoolProperty(default=False)
    saved_materials_json: StringProperty(default="")
    baked_image_name: StringProperty(default="")


def _on_texture_name_change(props, context):
    """Auto-cancel when user manually edits texture name."""
    if props.get("_refreshing"):
        return
    _on_auto_cancel(props, context)


def _update_texture_name_and_cancel(props, context):
    """When bake_type changes: refresh texture name suffix, then auto-cancel if result is shown."""
    props["_refreshing"] = True
    _refresh_texture_name(props)
    props["_refreshing"] = False
    _on_auto_cancel(props, context)


def _on_auto_cancel(props, context):
    """Auto-trigger cancel_result if result is currently shown."""
    if props.show_result_used and not props.cancel_used:
        bpy.ops.quickcake.cancel_result()


def _on_texture_size_change(props, context):
    """If result is currently shown, auto-trigger cancel_result."""
    if props.show_result_used and not props.cancel_used:
        bpy.ops.quickcake.cancel_result()


def _refresh_texture_name(props):
    suffix_map = {
        'BASE_COLOR':        "_BaseColor",
        'NORMAL':            "_Normal",
        'AMBIENT_OCCLUSION': "_AO",
    }
    base = props.low_poly.name if props.low_poly else ""
    suffix = suffix_map.get(props.bake_type, "")
    props.texture_name = base + suffix


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def count_tris(obj):
    """Return triangle count of obj (evaluating current state)."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    count = len(bm.faces)
    bm.free()
    return count


def ensure_visible(obj):
    """Make object visible in viewport and return original state."""
    state = {
        "hide_viewport": obj.hide_viewport,
        "hide_get":      obj.hide_get(),
    }
    obj.hide_viewport = False
    obj.hide_set(False)
    return state


def restore_visibility(obj, state):
    obj.hide_viewport = state["hide_viewport"]
    obj.hide_set(state["hide_get"])


def get_or_create_image(name, size):
    if name in bpy.data.images:
        img = bpy.data.images[name]
        if img.size[0] != size or img.size[1] != size:
            bpy.data.images.remove(img)
        else:
            return img
    img = bpy.data.images.new(name=name, width=size, height=size, alpha=False)
    return img


def ensure_image_node(obj, image):
    """Add / select an Image Texture node in every material of obj pointing to image."""
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        mat.use_nodes = True
        nt = mat.node_tree
        img_node = None
        for n in nt.nodes:
            if n.type == 'TEX_IMAGE' and n.name == "__QC_BAKE__":
                img_node = n
                break
        if img_node is None:
            img_node = nt.nodes.new('ShaderNodeTexImage')
            img_node.name = "__QC_BAKE__"
            img_node.label = "QC Bake Target"
        img_node.image = image
        nt.nodes.active = img_node


def cleanup_bake_nodes(obj):
    """Remove __QC_BAKE__ helper nodes from all materials of obj."""
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes:
            continue
        nt = mat.node_tree
        for n in list(nt.nodes):
            if n.name == "__QC_BAKE__":
                nt.nodes.remove(n)


def serialize_materials(obj):
    import json
    data = []
    for slot in obj.material_slots:
        data.append(slot.material.name if slot.material else None)
    return json.dumps(data)


def deserialize_materials(obj, json_str):
    import json
    if not json_str:
        return
    data = json.loads(json_str)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    while len(obj.material_slots) > 0:
        bpy.ops.object.material_slot_remove()
    for mat_name in data:
        obj.data.materials.append(None)
        idx = len(obj.material_slots) - 1
        if mat_name and mat_name in bpy.data.materials:
            obj.material_slots[idx].material = bpy.data.materials[mat_name]


def _ensure_default_material(obj):
    """Ensure obj has at least one material with a Principled BSDF."""
    if len(obj.material_slots) > 0 and obj.material_slots[0].material is not None:
        mat = obj.material_slots[0].material
        mat.use_nodes = True
        nt = mat.node_tree
        for n in nt.nodes:
            if n.type == 'OUTPUT_MATERIAL':
                for link in nt.links:
                    if link.to_node == n and link.to_socket.name == 'Surface':
                        return
        pbsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
        out = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if out is None:
            out = nt.nodes.new('ShaderNodeOutputMaterial')
        nt.links.new(pbsdf.outputs[0], out.inputs['Surface'])
        return

    mat = bpy.data.materials.new(name=obj.name + "_QC_Default")
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    pbsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    nt.links.new(pbsdf.outputs[0], out.inputs['Surface'])
    obj.data.materials.append(mat)


def _ensure_collection_visible(context, obj):
    """Make all collections containing obj visible in view layer. Returns state dict."""
    states = {}
    for col in obj.users_collection:
        vl_col = context.view_layer.layer_collection
        found = _find_layer_collection(vl_col, col.name)
        if found:
            states[col.name] = {
                "hide": found.hide_viewport,
                "exclude": found.exclude,
            }
            found.hide_viewport = False
            found.exclude = False
    return states


def _restore_collection_visible(context, obj, states):
    for col in obj.users_collection:
        vl_col = context.view_layer.layer_collection
        found = _find_layer_collection(vl_col, col.name)
        if found and col.name in states:
            found.hide_viewport = states[col.name]["hide"]
            found.exclude = states[col.name]["exclude"]


def _find_layer_collection(layer_col, name):
    if layer_col.name == name:
        return layer_col
    for child in layer_col.children:
        result = _find_layer_collection(child, name)
        if result:
            return result
    return None


def _exit_local_view(context):
    """Exit local view if active."""
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    if space.local_view:
                        with context.temp_override(area=area):
                            bpy.ops.view3d.localview()
                    break
            break


def _calculate_ao_distance(obj):
    """Calculate AO Distance as 3% of object bounding box."""
    bbox_coords = [obj.matrix_world @ obj.bound_box[i] for i in range(8)]
    min_x = min(c.x for c in bbox_coords)
    max_x = max(c.x for c in bbox_coords)
    min_y = min(c.y for c in bbox_coords)
    max_y = max(c.y for c in bbox_coords)
    min_z = min(c.z for c in bbox_coords)
    max_z = max(c.z for c in bbox_coords)
    
    size_x = max_x - min_x
    size_y = max_y - min_y
    size_z = max_z - min_z
    max_size = max(size_x, size_y, size_z)
    
    if max_size == 0:
        return 0.1
    
    # 3% от максимального размера
    ao_distance = max_size * 0.03
    return max(ao_distance, 0.01)


# ─────────────────────────────────────────────
#  Operators
# ─────────────────────────────────────────────

class QUICKCAKE_OT_pick_high_poly(Operator):
    bl_idname = "quickcake.pick_high_poly"
    bl_label = "Выбрать High Poly"
    bl_description = "Выбрать модель, с которой возьмем информацию → High Poly"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.quick_cake
        obj = context.active_object
        if obj is None:
            self.report({'WARNING'}, "Нет активного объекта")
            return {'CANCELLED'}
        if obj.type != 'MESH':
            self.report({'WARNING'}, "Выберите объект типа Mesh")
            return {'CANCELLED'}
        props.high_poly = obj
        return {'FINISHED'}


class QUICKCAKE_OT_pick_low_poly(Operator):
    bl_idname = "quickcake.pick_low_poly"
    bl_label = "Выбрать Low Poly"
    bl_description = "Выбрать модель, НА которую перенесем информацию → Low Poly"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.quick_cake
        obj = context.active_object
        if obj is None:
            self.report({'WARNING'}, "Нет активного объекта")
            return {'CANCELLED'}
        if obj.type != 'MESH':
            self.report({'WARNING'}, "Выберите объект типа Mesh")
            return {'CANCELLED'}
        tri_count = count_tris(obj)
        if tri_count > 2000:
            context.scene["__qc_pending_low__"] = obj.name
            context.scene["__qc_tri_warning__"] = True
        else:
            props.low_poly = obj
            _refresh_texture_name(props)
            context.scene["__qc_tri_warning__"] = False
            context.scene["__qc_pending_low__"] = ""
        return {'FINISHED'}


class QUICKCAKE_OT_confirm_low_poly(Operator):
    bl_idname = "quickcake.confirm_low_poly"
    bl_label = "Продолжить"
    bl_description = "Добавить модель несмотря на высокую плотность треугольников"

    def execute(self, context):
        props = context.scene.quick_cake
        pending = context.scene.get("__qc_pending_low__", "")
        if pending and pending in bpy.data.objects:
            props.low_poly = bpy.data.objects[pending]
            _refresh_texture_name(props)
        context.scene["__qc_tri_warning__"] = False
        context.scene["__qc_pending_low__"] = ""
        return {'FINISHED'}


class QUICKCAKE_OT_cancel_low_poly(Operator):
    bl_idname = "quickcake.cancel_low_poly"
    bl_label = "Отмена"
    bl_description = "Отменить добавление Low Poly модели"

    def execute(self, context):
        context.scene["__qc_tri_warning__"] = False
        context.scene["__qc_pending_low__"] = ""
        return {'FINISHED'}


class QUICKCAKE_OT_auto_uv(Operator):
    bl_idname = "quickcake.auto_uv"
    bl_label = "Auto UV"
    bl_description = "Создание авто развертки Smart UV для Low poly модели"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.quick_cake
        obj = props.low_poly
        if obj is None:
            self.report({'WARNING'}, "Low Poly не выбрана")
            return {'CANCELLED'}

        prev_active = context.view_layer.objects.active
        prev_selected = [o for o in context.selected_objects]

        vis = ensure_visible(obj)
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(island_margin=0.02)
        bpy.ops.uv.pack_islands(margin=0.02)
        bpy.ops.object.mode_set(mode='OBJECT')

        restore_visibility(obj, vis)

        bpy.ops.object.select_all(action='DESELECT')
        for o in prev_selected:
            try:
                o.select_set(True)
            except Exception:
                pass
        context.view_layer.objects.active = prev_active

        self.report({'INFO'}, f"Auto UV создана для {obj.name}")
        return {'FINISHED'}


class QUICKCAKE_OT_bake(Operator):
    bl_idname = "quickcake.bake"
    bl_label = "Запечь"
    bl_description = "Запечь текстуру с High Poly на Low Poly"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.quick_cake
        scene = context.scene

        high = props.high_poly
        low  = props.low_poly
        if high is None or low is None:
            self.report({'ERROR'}, "Выберите High Poly и Low Poly")
            return {'CANCELLED'}
        if not props.texture_name.strip():
            self.report({'ERROR'}, "Укажите имя текстуры")
            return {'CANCELLED'}

        size     = int(props.texture_size)
        tex_name = props.texture_name.strip()
        btype    = props.bake_type
        extrusion       = props.extrusion
        max_ray_distance = props.max_ray_distance

        # ── Exit Local View ─────────────────────────────────────────────────────────
        _exit_local_view(context)

        # ── Image ─────────────────────────────────────────────────────────────────────
        image = get_or_create_image(tex_name, size)
        
        # Установка colorspace ПЕРЕД запеканием
        if btype == 'NORMAL':
            image.colorspace_settings.name = 'Non-Color'
        elif btype == 'AMBIENT_OCCLUSION':
            image.colorspace_settings.name = 'Non-Color'
        else:
            image.colorspace_settings.name = 'sRGB'
        
        props.baked_image_name = tex_name

        # ── Engine ────────────────────────────────────────────────────────────────────
        prev_engine = scene.render.engine
        scene.render.engine = 'CYCLES'

        # ── Visibility ────────────────────────────────────────────────────────────────
        high_vis       = ensure_visible(high)
        low_vis        = ensure_visible(low)
        high_col_states = _ensure_collection_visible(context, high)
        low_col_states  = _ensure_collection_visible(context, low)

        # ── Ensure materials ───────────────────────────────────────────────────────────
        _ensure_default_material(high)
        _ensure_default_material(low)

        # ── Setup bake target ──────────────────────────────────────────────────────────
        ensure_image_node(low, image)

        # ── Object mode ────────────────────────────────────────────────────────────────
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # ── Setup selection: High SELECTED, Low ACTIVE (Selected to Active) ──────────
        prev_active = context.view_layer.objects.active
        prev_sel    = list(context.selected_objects)

        bpy.ops.object.select_all(action='DESELECT')
        high.select_set(True)
        low.select_set(True)
        context.view_layer.objects.active = low

        # ── Bake ─────────────────────────────────────────────────────────────────────
        bake_error = None

        try:
            if btype == 'BASE_COLOR':
                # Diffuse Color bake
                bpy.ops.object.bake(
                    type='DIFFUSE',
                    use_selected_to_active=True,
                    cage_extrusion=extrusion,
                    max_ray_distance=max_ray_distance,
                    margin=16,
                )

            elif btype == 'NORMAL':
                # Normal bake - Tangent Space
                bpy.ops.object.bake(
                    type='NORMAL',
                    normal_space='TANGENT',
                    use_selected_to_active=True,
                    cage_extrusion=extrusion,
                    max_ray_distance=max_ray_distance,
                    margin=16,
                )

            elif btype == 'AMBIENT_OCCLUSION':
                # AO bake с автоматическим расчетом
                ao_distance = _calculate_ao_distance(low)
                bpy.ops.object.bake(
                    type='AO',
                    use_selected_to_active=True,
                    cage_extrusion=extrusion,
                    max_ray_distance=ao_distance,
                    margin=16,
                )

        except Exception as e:
            bake_error = str(e)

        # ── Cleanup ──────────────────────────────────────────────────────────────────
        cleanup_bake_nodes(low)
        _restore_collection_visible(context, high, high_col_states)
        _restore_collection_visible(context, low,  low_col_states)
        self._restore(context, scene, prev_engine, high, low,
                      high_vis, low_vis, prev_active, prev_sel)

        if bake_error:
            self.report({'ERROR'}, f"Ошибка запекания: {bake_error}")
            return {'CANCELLED'}

        props.bake_done       = True
        props.show_result_used = False
        props.cancel_used      = False

        # Auto-show result
        bpy.ops.quickcake.show_result()

        self.report({'INFO'}, f"Запекание завершено: {tex_name}")
        return {'FINISHED'}

    def _restore(self, context, scene, prev_engine, high, low, high_vis, low_vis, prev_active, prev_sel):
        restore_visibility(high, high_vis)
        restore_visibility(low, low_vis)
        scene.render.engine = prev_engine
        bpy.ops.object.select_all(action='DESELECT')
        for o in prev_sel:
            try:
                o.select_set(True)
            except Exception:
                pass
        context.view_layer.objects.active = prev_active


class QUICKCAKE_OT_show_result(Operator):
    bl_idname = "quickcake.show_result"
    bl_label = "Показать результат"
    bl_description = "Показать результат запекания на Low Poly модели"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.quick_cake
        low = props.low_poly
        if low is None:
            self.report({'ERROR'}, "Low Poly не задана")
            return {'CANCELLED'}
        image_name = props.baked_image_name
        if not image_name or image_name not in bpy.data.images:
            self.report({'ERROR'}, "Текстура не найдена. Сначала запеките.")
            return {'CANCELLED'}
        image = bpy.data.images[image_name]

        # Save materials for Undo
        props.saved_materials_json = serialize_materials(low)

        # Exit any existing local view
        _exit_local_view(context)

        # Select low, isolate
        vis = ensure_visible(low)
        bpy.ops.object.select_all(action='DESELECT')
        low.select_set(True)
        context.view_layer.objects.active = low

        # Enter local view
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                with context.temp_override(area=area):
                    bpy.ops.view3d.localview()
                break

        # Clear slots and materials
        bpy.context.view_layer.objects.active = low
        while len(low.material_slots) > 0:
            bpy.ops.object.material_slot_remove()

        # Create material
        mat_name = low.name + "_QC_Preview"
        if mat_name in bpy.data.materials:
            mat = bpy.data.materials[mat_name]
            nt = mat.node_tree
            for n in list(nt.nodes):
                nt.nodes.remove(n)
        else:
            mat = bpy.data.materials.new(name=mat_name)
        
        mat.use_nodes = True
        nt = mat.node_tree

        out = nt.nodes.new('ShaderNodeOutputMaterial')
        pbsdf = nt.nodes.new('ShaderNodeBsdfPrincipled')
        out.location = (300, 0)
        pbsdf.location = (0, 0)
        nt.links.new(pbsdf.outputs[0], out.inputs[0])

        img_node = nt.nodes.new('ShaderNodeTexImage')
        img_node.image = image
        img_node.location = (-300, 0)

        btype = props.bake_type

        if btype == 'BASE_COLOR':
            nt.links.new(img_node.outputs[0], pbsdf.inputs['Base Color'])

        elif btype == 'NORMAL':
            normal_map = nt.nodes.new('ShaderNodeNormalMap')
            normal_map.space = 'TANGENT'
            normal_map.location = (-100, -150)
            nt.links.new(img_node.outputs[0], normal_map.inputs['Color'])
            nt.links.new(normal_map.outputs[0], pbsdf.inputs['Normal'])

        elif btype == 'AMBIENT_OCCLUSION':
            nt.links.new(img_node.outputs[0], pbsdf.inputs['Base Color'])

        low.data.materials.append(mat)

        restore_visibility(low, vis)

        props.show_result_used = True
        props.cancel_used = False

        self.report({'INFO'}, "Результат отображён")
        return {'FINISHED'}


class QUICKCAKE_OT_cancel_result(Operator):
    bl_idname = "quickcake.cancel_result"
    bl_label = "Вернуться"
    bl_description = "Вернуться: восстановить исходные материалы и выйти из изоляции"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.quick_cake
        low = props.low_poly
        if low is None:
            self.report({'ERROR'}, "Low Poly не задана")
            return {'CANCELLED'}

        _exit_local_view(context)

        vis = ensure_visible(low)
        bpy.ops.object.select_all(action='DESELECT')
        low.select_set(True)
        context.view_layer.objects.active = low

        mat_name = low.name + "_QC_Preview"
        if mat_name in bpy.data.materials:
            preview_mat = bpy.data.materials[mat_name]
            while len(low.material_slots) > 0:
                bpy.ops.object.material_slot_remove()
            bpy.data.materials.remove(preview_mat)

        deserialize_materials(low, props.saved_materials_json)
        restore_visibility(low, vis)

        props.show_result_used = False
        props.cancel_used = True

        self.report({'INFO'}, "Исходные материалы восстановлены")
        return {'FINISHED'}


class QUICKCAKE_OT_save_texture(Operator):
    bl_idname = "quickcake.save_texture"
    bl_label = "Сохранить"
    bl_description = "Сохранить созданную текстуру"
    bl_options = {'REGISTER'}

    filepath: StringProperty(subtype='FILE_PATH')
    filename: StringProperty()
    directory: StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        props = context.scene.quick_cake
        image_name = props.baked_image_name
        if not image_name or image_name not in bpy.data.images:
            self.report({'ERROR'}, "Нет текстуры для сохранения")
            return {'CANCELLED'}

        self.filename = image_name + ".png"
        blend_path = bpy.data.filepath
        if blend_path:
            self.directory = os.path.dirname(blend_path)
        else:
            self.directory = os.path.expanduser("~/Downloads")

        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        props = context.scene.quick_cake
        image_name = props.baked_image_name
        if not image_name or image_name not in bpy.data.images:
            self.report({'ERROR'}, "Нет текстуры для сохранения")
            return {'CANCELLED'}
        image = bpy.data.images[image_name]
        image.filepath_raw = self.filepath
        image.file_format = 'PNG'
        image.save()
        self.report({'INFO'}, f"Текстура сохранена: {self.filepath}")
        return {'FINISHED'}


class QUICKCAKE_OT_clear_all(Operator):
    bl_idname = "quickcake.clear_all"
    bl_label = "Очистить данные"
    bl_description = "Очистить данные"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.quick_cake
        props.high_poly = None
        props.low_poly = None
        props.texture_name = ""
        props.texture_size = '1024'
        props.bake_type = 'BASE_COLOR'
        props.extrusion = 0.01
        props.max_ray_distance = 0.02
        props.bake_done = False
        props.show_result_used = False
        props.cancel_used = False
        props.show_projection = False
        props.saved_materials_json = ""
        props.baked_image_name = ""
        context.scene["__qc_tri_warning__"] = False
        context.scene["__qc_pending_low__"] = ""
        self.report({'INFO'}, "Данные очищены")
        return {'FINISHED'}


class QUICKCAKE_OT_toggle_projection(Operator):
    bl_idname = "quickcake.toggle_projection"
    bl_label = "Отступы проецирования"
    bl_description = "Развернуть/свернуть отступы проецирования"
    bl_options = {'REGISTER'}

    def execute(self, context):
        context.scene.quick_cake.show_projection = not context.scene.quick_cake.show_projection
        return {'FINISHED'}


# ─────────────────────────────────────────────
#  Panel
# ─────────────────────────────────────────────

class QUICKCAKE_PT_main(Panel):
    bl_label = "Quick Cake"
    bl_idname = "QUICKCAKE_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Quick Cake"

    def draw(self, context):
        layout = self.layout
        props = context.scene.quick_cake
        scene = context.scene

        tri_warning = scene.get("__qc_tri_warning__", False)

        # ─── Step 1: High Poly ───
        self._draw_step(layout, "1", "Выбрать High Poly")
        box1 = layout.box()
        row1 = box1.row(align=True)
        if props.high_poly:
            name1 = row1.row(align=True)
            name1.alert = True
            name1.label(text=f"High Poly:  {props.high_poly.name}")
            row1.operator("quickcake.pick_high_poly", text="", icon='EYEDROPPER')
        else:
            row1.label(text="High Poly:")
            row1.operator("quickcake.pick_high_poly", text="", icon='EYEDROPPER')

        layout.separator()

        # ─── Step 2: Low Poly ───
        step2_active = props.high_poly is not None
        self._draw_step(layout, "2", "Выбрать Low Poly", enabled=step2_active)
        box2 = layout.box()
        box2.enabled = step2_active
        row2 = box2.row(align=True)
        if props.low_poly:
            name2 = row2.row(align=True)
            name2.alert = True
            name2.label(text=f"Low Poly:  {props.low_poly.name}")
            row2.operator("quickcake.pick_low_poly", text="", icon='EYEDROPPER')
        else:
            row2.label(text="Low Poly:")
            row2.operator("quickcake.pick_low_poly", text="", icon='EYEDROPPER')

        if tri_warning:
            warn_box = box2.box()
            warn_box.alert = True
            warn_box.label(text="Плотность треугольников > 2000.", icon='ERROR')
            warn_box.label(text="Это точно LOW POLY ?")
            warn_row = warn_box.row(align=True)
            warn_row.operator("quickcake.confirm_low_poly", text="Продолжить", icon='CHECKMARK')
            warn_row.operator("quickcake.cancel_low_poly", text="Отмена", icon='X')

        if props.low_poly:
            uv_row = box2.row()
            uv_row.operator("quickcake.auto_uv", text="Auto UV", icon='UV')

        layout.separator()

        # ─── Step 3: Bake Type ───
        step3_active = step2_active and props.low_poly is not None
        self._draw_step(layout, "3", "Что будем запекать?", enabled=step3_active)
        box3 = layout.box()
        box3.enabled = step3_active
        box3.prop(props, "bake_type", text="")

        layout.separator()

        # ─── Step 4: Texture Name ───
        step4_active = step3_active
        self._draw_step(layout, "4", "Имя текстуры", enabled=step4_active)
        box4 = layout.box()
        box4.enabled = step4_active
        box4.prop(props, "texture_name", text="")

        layout.separator()

        # ─── Step 5: Texture Size ───
        step5_active = step4_active and bool(props.texture_name.strip())
        self._draw_step(layout, "5", "Размер текстуры", enabled=step5_active)
        box5 = layout.box()
        box5.enabled = step5_active
        box5.prop(props, "texture_size", text="")

        layout.separator()

        # ─── Step 6: Projection margins ───
        step6_active = step5_active
        box6 = layout.box()
        box6.enabled = step6_active
        row6 = box6.row(align=True)
        icon6 = 'TRIA_DOWN' if props.show_projection else 'TRIA_RIGHT'
        row6.operator(
            "quickcake.toggle_projection",
            text="6.  Отступы проецирования",
            icon=icon6,
            emboss=False,
        )
        if props.show_projection:
            box6.prop(props, "extrusion", text="Extrusion")
            box6.prop(props, "max_ray_distance", text="Max Ray Distance")

        layout.separator()

        # ─── Step 7: Bake ───
        step7_active = step6_active
        row7 = layout.row(align=True)
        row7.enabled = step7_active
        lbl7 = row7.row()
        lbl7.ui_units_x = 1.5
        lbl7.label(text="7.")
        op7 = row7.row()
        op7.scale_x = 10.0
        op7.operator("quickcake.bake", text="Запечь", icon='FUND')

        layout.separator()

        # ─── Step 8: Cancel Result ───
        step8_active = props.show_result_used and not props.cancel_used
        row8 = layout.row(align=True)
        row8.enabled = step8_active
        lbl8 = row8.row()
        lbl8.ui_units_x = 1.5
        lbl8.label(text="8.")
        op8 = row8.row()
        op8.scale_x = 10.0
        op8.operator("quickcake.cancel_result", text="Вернуться", icon='LOOP_BACK')

        layout.separator()

        # ─── Step 9: Save + Trash ───
        step9_active = props.bake_done
        row9 = layout.row(align=True)
        lbl9 = row9.row()
        lbl9.ui_units_x = 1.5
        lbl9.enabled = step9_active
        lbl9.label(text="9.")
        op9 = row9.row()
        op9.enabled = step9_active
        op9.scale_x = 10.0
        op9.operator("quickcake.save_texture", text="Сохранить", icon='FILE_TICK')
        trash = row9.row()
        trash.active = True
        trash.enabled = True
        trash.operator("quickcake.clear_all", text="", icon='TRASH')

    def _draw_step(self, layout, number, label, enabled=True):
        row = layout.row()
        row.enabled = enabled
        row.label(text=f"  {number}. {label}")


# ─────────────────────────────────────────────
#  Registration
# ─────────────────────────────────────────────

classes = (
    QuickCakeProps,
    QUICKCAKE_OT_pick_high_poly,
    QUICKCAKE_OT_pick_low_poly,
    QUICKCAKE_OT_confirm_low_poly,
    QUICKCAKE_OT_cancel_low_poly,
    QUICKCAKE_OT_auto_uv,
    QUICKCAKE_OT_bake,
    QUICKCAKE_OT_show_result,
    QUICKCAKE_OT_cancel_result,
    QUICKCAKE_OT_save_texture,
    QUICKCAKE_OT_clear_all,
    QUICKCAKE_OT_toggle_projection,
    QUICKCAKE_PT_main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.quick_cake = PointerProperty(type=QuickCakeProps)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.quick_cake
