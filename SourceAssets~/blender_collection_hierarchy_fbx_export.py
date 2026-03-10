import bpy
import sys
import re
import math
import mathutils
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ============================================================
# USER SETTINGS (edit only these 3 values)
# ============================================================
ROOT_COLLECTION_NAME = "Root"
EXPORT_DIR = "/Users/masanori/EscapeRoomTest/Assets/0/FBX"
TEXTURE_SIZE = 2048

# ============================================================
# CLI OVERRIDE  (blender --background file.blend --python script.py -- ColName /out/dir)
# ============================================================
def _apply_cli_args() -> None:
    global ROOT_COLLECTION_NAME, EXPORT_DIR
    if "--" not in sys.argv:
        return
    args = sys.argv[sys.argv.index("--") + 1:]
    if len(args) >= 1 and args[0].strip():
        ROOT_COLLECTION_NAME = args[0].strip()
    if len(args) >= 2 and args[1].strip():
        EXPORT_DIR = args[1].strip()

_apply_cli_args()


# ============================================================
# Script behavior:
# Run Script -> Bake -> Rewrite nodes -> Export FBX
# No UI. Single script only.
# ============================================================

TMP_COLLECTION_BASENAME = "__EXPORT_TMP_BAKE__"
INCLUDE_EXPORT_TYPES = {"MESH", "EMPTY", "LIGHT", "CAMERA", "ARMATURE", "CURVE"}
BAKE_SAMPLES = 16
CLEAN_OLD_LEFTOVERS = False


def log(msg: str) -> None:
    print(f"[AUTO_BAKE_EXPORT] {msg}")


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    return cleaned or "Unnamed"


def sanitize_name(name: str) -> str:
    name = name.replace(".", "_")
    name = name.replace(" ", "_")
    return name


def ensure_export_dir(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        raise RuntimeError("EXPORT_DIR must be an absolute macOS path.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_root_collection(name: str) -> bpy.types.Collection:
    col = bpy.data.collections.get(name)
    if not col:
        raise RuntimeError(f"Collection not found: '{name}'")
    return col


def iter_collections_recursive(root_col: bpy.types.Collection):
    yield root_col
    for child in root_col.children:
        yield from iter_collections_recursive(child)


def collect_objects_recursive(root_col: bpy.types.Collection) -> List[bpy.types.Object]:
    seen: Set[bpy.types.Object] = set()
    result: List[bpy.types.Object] = []
    for col in iter_collections_recursive(root_col):
        for obj in col.objects:
            if obj in seen:
                continue
            seen.add(obj)
            result.append(obj)
    return result


def build_primary_owner_map(root_col: bpy.types.Collection) -> Dict[bpy.types.Object, bpy.types.Collection]:
    owner: Dict[bpy.types.Object, bpy.types.Collection] = {}
    warned: Set[bpy.types.Object] = set()
    for col in iter_collections_recursive(root_col):
        for obj in col.objects:
            if obj not in owner:
                owner[obj] = col
            elif obj not in warned:
                log(
                    f"WARNING multi-collection object: {obj.name} "
                    f"(PRIMARY -> {owner[obj].name}, ignored in {col.name})"
                )
                warned.add(obj)
    return owner


def build_collection_parent_map(root_col: bpy.types.Collection) -> Dict[bpy.types.Collection, Optional[bpy.types.Collection]]:
    parent_map: Dict[bpy.types.Collection, Optional[bpy.types.Collection]] = {root_col: None}

    def walk(col: bpy.types.Collection):
        for child in col.children:
            parent_map[child] = col
            walk(child)

    walk(root_col)
    return parent_map


def unique_collection_name(base_name: str) -> str:
    if base_name not in bpy.data.collections:
        return base_name
    i = 1
    while True:
        candidate = f"{base_name}_{i:03d}"
        if candidate not in bpy.data.collections:
            return candidate
        i += 1


def unique_object_name(base_name: str) -> str:
    if base_name not in bpy.data.objects:
        return base_name
    i = 1
    while True:
        candidate = f"{base_name}_{i:03d}"
        if candidate not in bpy.data.objects:
            return candidate
        i += 1


def duplicate_export_objects(
    scene: bpy.types.Scene,
    root_col: bpy.types.Collection,
) -> Tuple[
    bpy.types.Collection,
    Dict[bpy.types.Object, bpy.types.Object],
    Dict[bpy.types.Object, bpy.types.Object],
    List[bpy.types.Object],
    List[bpy.types.Material],
]:
    owner_map = build_primary_owner_map(root_col)
    parent_map = build_collection_parent_map(root_col)
    source_objects = collect_objects_recursive(root_col)

    tmp_col_name = unique_collection_name(TMP_COLLECTION_BASENAME)
    tmp_col = bpy.data.collections.new(tmp_col_name)
    scene.collection.children.link(tmp_col)
    tmp_col["created_by_bake_export_script"] = True

    # Preserve collection hierarchy through export-only empties.
    col_to_empty: Dict[bpy.types.Collection, bpy.types.Object] = {}
    for col in iter_collections_recursive(root_col):
        e = bpy.data.objects.new(unique_object_name(f"COL_{safe_name(col.name)}"), None)
        e.empty_display_type = "PLAIN_AXES"
        e.empty_display_size = 0.35
        tmp_col.objects.link(e)
        col_to_empty[col] = e

    for col in iter_collections_recursive(root_col):
        parent_col = parent_map.get(col)
        if parent_col and parent_col in col_to_empty:
            world = col_to_empty[col].matrix_world.copy()
            col_to_empty[col].parent = col_to_empty[parent_col]
            col_to_empty[col].matrix_world = world

    src_to_dup: Dict[bpy.types.Object, bpy.types.Object] = {}
    dup_to_src: Dict[bpy.types.Object, bpy.types.Object] = {}
    duplicates: List[bpy.types.Object] = []

    for src in source_objects:
        dup = src.copy()
        dup.name = unique_object_name(f"{safe_name(src.name)}__x")
        if src.type == "MESH" and src.data:
            dup.data = src.data.copy()
        tmp_col.objects.link(dup)
        dup.matrix_world = src.matrix_world.copy()
        src_to_dup[src] = dup
        dup_to_src[dup] = src
        duplicates.append(dup)

    # Keep original object-parent hierarchy where possible, otherwise parent to collection empty.
    for src, dup in src_to_dup.items():
        world = dup.matrix_world.copy()
        if src.parent and src.parent in src_to_dup:
            dup.parent = src_to_dup[src.parent]
        else:
            owner_col = owner_map.get(src)
            if owner_col in col_to_empty:
                dup.parent = col_to_empty[owner_col]
        dup.matrix_world = world

    # Duplicate materials per (mesh datablock, material) pair.
    mat_map: Dict[Tuple[int, int], bpy.types.Material] = {}
    created_materials: List[bpy.types.Material] = []
    for src, dup in src_to_dup.items():
        if dup.type != "MESH" or not dup.data:
            continue
        for i, slot in enumerate(dup.material_slots):
            src_mat = slot.material
            if not src_mat:
                continue
            mesh_ptr = src.data.as_pointer() if src.data else 0
            mat_ptr = src_mat.as_pointer()
            pair_key = (mesh_ptr, mat_ptr)
            if pair_key not in mat_map:
                mat_dup = src_mat.copy()
                mat_dup.name = safe_name(f"{src.data.name}__{src_mat.name}__x")
                mat_map[pair_key] = mat_dup
                created_materials.append(mat_dup)
            dup.material_slots[i].material = mat_map[pair_key]

    return tmp_col, src_to_dup, dup_to_src, list(col_to_empty.values()) + duplicates, created_materials


def ensure_uvs(obj: bpy.types.Object) -> None:
    if obj.type != "MESH" or not obj.data:
        return
    log(f"Creating/refreshing UVs for bake: {obj.name}")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=66)
    bpy.ops.object.mode_set(mode="OBJECT")

def texture_paths_for_pair(texture_dir: Path, mesh_name: str, mat_name: str) -> Dict[str, Path]:
    mesh_name = sanitize_name(mesh_name)
    mat_name = sanitize_name(mat_name)
    base = f"{mesh_name}__{mat_name}"
    return {
        "base": texture_dir / f"{base}_BaseMap.png",
        "bump": texture_dir / f"{base}_BumpMap.png",
        "mask": texture_dir / f"{base}_MaskMap.png",
        "emission": texture_dir / f"{base}_EmissionMap.png",
    }


def load_or_create_image_from_path(path: Path, colorspace: str) -> bpy.types.Image:
    if path.exists():
        img = bpy.data.images.load(str(path), check_existing=True)
    else:
        img = bpy.data.images.new(name=path.stem, width=TEXTURE_SIZE, height=TEXTURE_SIZE, alpha=True, float_buffer=False)
        img.generated_color = (0.0, 0.0, 0.0, 1.0)
        save_image_png(img, path)
    img.filepath = str(path)
    img.colorspace_settings.name = colorspace
    return img


def create_image(name: str, size: int, alpha: bool = True) -> bpy.types.Image:
    img = bpy.data.images.new(name=name, width=size, height=size, alpha=alpha, float_buffer=False)
    img.generated_color = (0.0, 0.0, 0.0, 1.0)
    return img


def save_image_png(img: bpy.types.Image, out_path: Path) -> None:
    img.filepath_raw = str(out_path)
    img.file_format = "PNG"
    img.save()


def set_only_object_selected(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def add_bake_target_node(mat: bpy.types.Material, img: bpy.types.Image) -> bpy.types.Node:
    nt = mat.node_tree
    node = nt.nodes.new("ShaderNodeTexImage")
    node.image = img
    for n in nt.nodes:
        n.select = False
    node.select = True
    nt.nodes.active = node
    return node


def remove_node_safe(nt: bpy.types.NodeTree, node: Optional[bpy.types.Node]) -> None:
    if node and node.name in nt.nodes:
        nt.nodes.remove(node)


def get_principled(mat: bpy.types.Material) -> Optional[bpy.types.Node]:
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


# ============================================================
# Trivial texture detection helpers
# ============================================================

def _socket_is_trivial(socket, threshold: float = 0.001) -> bool:
    """True if socket has no link and its value is effectively zero."""
    if socket is None:
        return True
    if socket.is_linked:
        return False
    val = socket.default_value
    if isinstance(val, float):
        return val < threshold
    try:
        return all(v < threshold for v in list(val)[:3])
    except Exception:
        return True


def _needs_emission_bake(principled: bpy.types.Node) -> bool:
    """True when the material has non-trivial emission."""
    strength = principled.inputs.get("Emission Strength")
    if strength and not strength.is_linked and strength.default_value < 0.001:
        return False
    color = principled.inputs.get("Emission Color")
    return not _socket_is_trivial(color)


def _needs_mask_bake(principled: bpy.types.Node) -> bool:
    """True when Metallic or Roughness has variation worth baking."""
    metallic = principled.inputs.get("Metallic")
    roughness = principled.inputs.get("Roughness")
    if metallic and metallic.is_linked:
        return True
    if roughness and roughness.is_linked:
        return True
    # Both unlinked: skip when metallic is effectively 0 (flat non-metal)
    if metallic and metallic.default_value < 0.001:
        return False
    return True


def _needs_bump_bake(principled: bpy.types.Node) -> bool:
    """True when the Normal socket is connected (normal/bump map in use)."""
    normal = principled.inputs.get("Normal")
    return normal is not None and normal.is_linked


def bake_base_color(obj: bpy.types.Object, mat: bpy.types.Material, img: bpy.types.Image) -> None:
    nt = mat.node_tree
    target = add_bake_target_node(mat, img)
    try:
        set_only_object_selected(obj)
        bpy.ops.object.bake(type="DIFFUSE", pass_filter={"COLOR"})
    finally:
        remove_node_safe(nt, target)


def bake_normal(obj: bpy.types.Object, mat: bpy.types.Material, img: bpy.types.Image) -> None:
    nt = mat.node_tree
    target = add_bake_target_node(mat, img)
    try:
        set_only_object_selected(obj)
        bpy.ops.object.bake(type="NORMAL", normal_space="TANGENT")
    finally:
        remove_node_safe(nt, target)


def bake_ao(obj: bpy.types.Object, mat: bpy.types.Material, img: bpy.types.Image) -> None:
    nt = mat.node_tree
    target = add_bake_target_node(mat, img)
    try:
        set_only_object_selected(obj)
        bpy.ops.object.bake(type="AO")
    finally:
        remove_node_safe(nt, target)


def _socket_to_grayscale_value(
    nt: bpy.types.NodeTree,
    socket: bpy.types.NodeSocket,
) -> bpy.types.NodeSocket:
    if socket.is_linked:
        src = socket.links[0].from_socket
        if src.type in {"VALUE", "INT"}:
            return src
        rgb_to_bw = nt.nodes.new("ShaderNodeRGBToBW")
        nt.links.new(src, rgb_to_bw.inputs["Color"])
        return rgb_to_bw.outputs["Val"]

    val = socket.default_value
    if isinstance(val, float):
        value_node = nt.nodes.new("ShaderNodeValue")
        value_node.outputs["Value"].default_value = val
        return value_node.outputs["Value"]
    if hasattr(val, "__len__") and len(val) >= 3:
        rgb_node = nt.nodes.new("ShaderNodeRGB")
        rgb_node.outputs["Color"].default_value = (val[0], val[1], val[2], 1.0)
        rgb_to_bw = nt.nodes.new("ShaderNodeRGBToBW")
        nt.links.new(rgb_node.outputs["Color"], rgb_to_bw.inputs["Color"])
        return rgb_to_bw.outputs["Val"]

    value_node = nt.nodes.new("ShaderNodeValue")
    value_node.outputs["Value"].default_value = 0.0
    return value_node.outputs["Value"]


def bake_principled_scalar_via_emit(
    obj: bpy.types.Object,
    mat: bpy.types.Material,
    principled: bpy.types.Node,
    principled_input_name: str,
    img: bpy.types.Image,
    invert: bool = False,
) -> None:
    nt = mat.node_tree
    created_nodes: List[bpy.types.Node] = []

    old_active_output = None
    for n in nt.nodes:
        if n.type == "OUTPUT_MATERIAL" and n.is_active_output:
            old_active_output = n
            break

    target = add_bake_target_node(mat, img)
    created_nodes.append(target)

    emit = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    combine = nt.nodes.new("ShaderNodeCombineColor")
    combine.mode = "RGB"
    created_nodes.extend([emit, out, combine])

    scalar_socket = _socket_to_grayscale_value(nt, principled.inputs[principled_input_name])
    if invert:
        inv = nt.nodes.new("ShaderNodeMath")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        nt.links.new(scalar_socket, inv.inputs[1])
        scalar_socket = inv.outputs["Value"]
        created_nodes.append(inv)

    nt.links.new(scalar_socket, combine.inputs["Red"])
    nt.links.new(scalar_socket, combine.inputs["Green"])
    nt.links.new(scalar_socket, combine.inputs["Blue"])
    nt.links.new(combine.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    out.is_active_output = True

    try:
        set_only_object_selected(obj)
        bpy.ops.object.bake(type="EMIT")
    finally:
        if old_active_output:
            old_active_output.is_active_output = True
        for n in created_nodes:
            remove_node_safe(nt, n)


def bake_emission(obj: bpy.types.Object, mat: bpy.types.Material, img: bpy.types.Image) -> None:
    nt = mat.node_tree
    target = add_bake_target_node(mat, img)
    try:
        set_only_object_selected(obj)
        bpy.ops.object.bake(type="EMIT")
    finally:
        remove_node_safe(nt, target)


def pack_mask_map(
    metallic_img: bpy.types.Image,
    ao_img: bpy.types.Image,
    smoothness_img: bpy.types.Image,
    out_name: str,
    size: int,
) -> bpy.types.Image:
    mask = bpy.data.images.new(name=out_name, width=size, height=size, alpha=True, float_buffer=False)
    px_count = size * size
    m = list(metallic_img.pixels)
    a = list(ao_img.pixels)
    s = list(smoothness_img.pixels)
    packed = [0.0] * (px_count * 4)

    for i in range(px_count):
        idx = i * 4
        packed[idx + 0] = m[idx + 0]    # R = Metallic
        packed[idx + 1] = a[idx + 0]    # G = AO
        packed[idx + 2] = 0.0           # B = 0
        packed[idx + 3] = s[idx + 0]    # A = Smoothness

    mask.pixels[:] = packed
    mask.update()
    return mask


def clear_and_rebuild_nodes(
    mat: bpy.types.Material,
    base_img: bpy.types.Image,
    bump_img: Optional[bpy.types.Image],
    mask_img: Optional[bpy.types.Image],
    emis_img: Optional[bpy.types.Image],
) -> None:
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)

    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (600, 0)

    tex_base = nt.nodes.new("ShaderNodeTexImage")
    tex_base.image = base_img
    tex_base.image.colorspace_settings.name = "sRGB"
    tex_base.location = (-350, 250)
    nt.links.new(tex_base.outputs["Color"], bsdf.inputs["Base Color"])

    if bump_img is not None:
        tex_bump = nt.nodes.new("ShaderNodeTexImage")
        tex_bump.image = bump_img
        tex_bump.image.colorspace_settings.name = "Non-Color"
        tex_bump.location = (-350, 40)
        normal_map = nt.nodes.new("ShaderNodeNormalMap")
        normal_map.location = (120, 40)
        nt.links.new(tex_bump.outputs["Color"], normal_map.inputs["Color"])
        nt.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])

    if mask_img is not None:
        tex_mask = nt.nodes.new("ShaderNodeTexImage")
        tex_mask.image = mask_img
        tex_mask.image.colorspace_settings.name = "Non-Color"
        tex_mask.location = (-350, -210)
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        sep.mode = "RGB"
        sep.location = (-80, -210)
        smooth_inv = nt.nodes.new("ShaderNodeMath")
        smooth_inv.operation = "SUBTRACT"
        smooth_inv.inputs[0].default_value = 1.0
        smooth_inv.location = (160, -320)
        nt.links.new(tex_mask.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs["Red"], bsdf.inputs["Metallic"])
        nt.links.new(tex_mask.outputs["Alpha"], smooth_inv.inputs[1])
        nt.links.new(smooth_inv.outputs[0], bsdf.inputs["Roughness"])

    if emis_img is not None:
        tex_emis = nt.nodes.new("ShaderNodeTexImage")
        tex_emis.image = emis_img
        tex_emis.image.colorspace_settings.name = "Non-Color"
        tex_emis.location = (-350, -470)
        nt.links.new(tex_emis.outputs["Color"], bsdf.inputs["Emission Color"])

    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])


def apply_transform_to_duplicate(obj: bpy.types.Object) -> None:
    if obj.type != "MESH":
        return
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)


def process_mesh_material_pair(
    mesh_obj: bpy.types.Object,
    mat: bpy.types.Material,
    texture_dir: Path,
    pair_key: Tuple[int, int],
    mesh_name_for_key: str,
    material_name_for_key: str,
    baked_pairs: Dict[Tuple[int, int], Dict[str, bpy.types.Image]],
) -> Dict[str, bpy.types.Image]:
    if pair_key in baked_pairs:
        log(f"Bake cache hit: {mesh_name_for_key} / {material_name_for_key}")
        return baked_pairs[pair_key]

    log(f"Processing material: {material_name_for_key} on mesh: {mesh_name_for_key}")
    if not mat.use_nodes or not mat.node_tree:
        log(f"Skipping material (no nodes): {mat.name}")
        empty = {}
        baked_pairs[pair_key] = empty
        return empty

    principled = get_principled(mat)
    if not principled:
        log(f"Skipping material (no Principled BSDF): {mat.name}")
        empty = {}
        baked_pairs[pair_key] = empty
        return empty

    paths = texture_paths_for_pair(texture_dir, mesh_name_for_key, material_name_for_key)
    log(f"Texture path (BaseMap): {paths['base']}")

    # Bake skip rule: skip when BaseMap already exists on disk.
    if paths["base"].exists():
        log(f"Skipping baked material: {mesh_name_for_key} / {material_name_for_key}")
        # UV はベイク時と同じ状態にしてからエクスポートしないとテクスチャがずれる
        apply_transform_to_duplicate(mesh_obj)
        ensure_uvs(mesh_obj)
        base_img = load_or_create_image_from_path(paths["base"], "sRGB")
        bump_img = load_or_create_image_from_path(paths["bump"], "Non-Color") if paths["bump"].exists() else None
        mask_img = load_or_create_image_from_path(paths["mask"], "Non-Color") if paths["mask"].exists() else None
        emis_img = load_or_create_image_from_path(paths["emission"], "Non-Color") if paths["emission"].exists() else None
        clear_and_rebuild_nodes(mat, base_img, bump_img, mask_img, emis_img)
        images = {"base": base_img, "bump": bump_img, "mask": mask_img, "emission": emis_img}
        baked_pairs[pair_key] = images
        return images

    needs_bump     = _needs_bump_bake(principled)
    needs_emission = _needs_emission_bake(principled)
    needs_mask     = _needs_mask_bake(principled)
    log(f"Baking material: {mesh_name_for_key} / {material_name_for_key} "
        f"(bump={needs_bump}, emission={needs_emission}, mask={needs_mask})")
    apply_transform_to_duplicate(mesh_obj)
    ensure_uvs(mesh_obj)

    pfx = f"{safe_name(mesh_name_for_key)}__{safe_name(material_name_for_key)}"
    base_img = create_image(f"{pfx}_Base_TMP", TEXTURE_SIZE, alpha=True)
    bake_base_color(mesh_obj, mat, base_img)

    bump_img: Optional[bpy.types.Image] = None
    if needs_bump:
        bump_img = create_image(f"{pfx}_Bump_TMP", TEXTURE_SIZE, alpha=True)
        bake_normal(mesh_obj, mat, bump_img)

    emis_img: Optional[bpy.types.Image] = None
    if needs_emission:
        emis_img = create_image(f"{pfx}_Emission_TMP", TEXTURE_SIZE, alpha=True)
        bake_emission(mesh_obj, mat, emis_img)

    mask_img: Optional[bpy.types.Image] = None
    ao_tmp = metallic_tmp = smoothness_tmp = None
    if needs_mask:
        ao_tmp        = create_image(f"{pfx}_AO_TMP",        TEXTURE_SIZE, alpha=True)
        metallic_tmp  = create_image(f"{pfx}_Metallic_TMP",  TEXTURE_SIZE, alpha=True)
        smoothness_tmp = create_image(f"{pfx}_Smoothness_TMP", TEXTURE_SIZE, alpha=True)
        bake_ao(mesh_obj, mat, ao_tmp)
        bake_principled_scalar_via_emit(mesh_obj, mat, principled, "Metallic",  metallic_tmp,   invert=False)
        bake_principled_scalar_via_emit(mesh_obj, mat, principled, "Roughness", smoothness_tmp, invert=True)
        mask_img = pack_mask_map(
            metallic_img=metallic_tmp,
            ao_img=ao_tmp,
            smoothness_img=smoothness_tmp,
            out_name=f"{pfx}_Mask_TMP",
            size=TEXTURE_SIZE,
        )

    log(f"Saving textures: {mesh_name_for_key} / {material_name_for_key}")
    save_image_png(base_img, paths["base"])
    base_img.filepath = str(paths["base"])
    if bump_img is not None:
        save_image_png(bump_img, paths["bump"])
        bump_img.filepath = str(paths["bump"])
    if mask_img is not None:
        save_image_png(mask_img, paths["mask"])
        mask_img.filepath = str(paths["mask"])
    if emis_img is not None:
        save_image_png(emis_img, paths["emission"])
        emis_img.filepath = str(paths["emission"])

    clear_and_rebuild_nodes(mat, base_img, bump_img, mask_img, emis_img)

    # Cleanup temporary helper images not referenced by final node graph.
    for tmp in [ao_tmp, metallic_tmp, smoothness_tmp]:
        if tmp and tmp.name in bpy.data.images:
            bpy.data.images.remove(tmp, do_unlink=True)

    images = {"base": base_img, "bump": bump_img, "mask": mask_img, "emission": emis_img}
    baked_pairs[pair_key] = images
    return images

def process_all_mesh_material_pairs(
    dup_mesh_objects: List[bpy.types.Object],
    dup_to_src: Dict[bpy.types.Object, bpy.types.Object],
    texture_dir: Path,
) -> None:
    baked_pairs: Dict[Tuple[int, int], Dict[str, bpy.types.Image]] = {}

    for obj in dup_mesh_objects:
        src_obj = dup_to_src.get(obj)
        if not src_obj or src_obj.type != "MESH" or not src_obj.data:
            continue
        log(f"Processing object: {obj.name}")
        mesh_name_for_key = src_obj.data.name

        for idx, slot in enumerate(obj.material_slots):
            mat = slot.material
            if not mat:
                continue
            src_mat = None
            if idx < len(src_obj.material_slots):
                src_mat = src_obj.material_slots[idx].material
            material_name_for_key = src_mat.name if src_mat else mat.name
            # Bake unit key: (Mesh datablock, Material datablock)
            mesh_ptr = src_obj.data.as_pointer()
            mat_ptr = src_mat.as_pointer() if src_mat else mat.as_pointer()
            pair_key = (mesh_ptr, mat_ptr)
            process_mesh_material_pair(
                mesh_obj=obj,
                mat=mat,
                texture_dir=texture_dir,
                pair_key=pair_key,
                mesh_name_for_key=mesh_name_for_key,
                material_name_for_key=material_name_for_key,
                baked_pairs=baked_pairs,
            )


def bake_blender_to_unity_transform(export_nodes: List[bpy.types.Object]) -> None:
    """
    全メッシュの頂点を Blender Z-up → Unity Y-up 座標系に変換してから
    全オブジェクトの Transform を identity にリセットする。

    FBX エクスポート後に Unity でインポートした際、全ノードが identity transform となり
    Transform Reset (Position=0,Rotation=0,Scale=1) のまま正しく表示される。

    変換行列: X 軸 -90° 回転 + ×100 拡大 (Blender Z-up → Unity Y-up, Unity globalScale=1 に合わせた頂点スケール)
      (0,0,1)[Blender Z-up] → -90°X → (0,1,0)[Unity Y-up] ✓
      ×100: Unity PostProcessor の importer.globalScale=1.0f と組み合わせて正しい表示サイズにする。
      (頂点×100 × Scale=1 = 頂点×1 × Scale=100 と等価の表示サイズ)
    処理順序 (行列は右から左に適用): まず -90°X 回転、次に ×100 拡大。
    """
    M_conv = mathutils.Matrix.Scale(100, 4) @ mathutils.Matrix.Rotation(math.radians(-90), 4, 'X')

    for obj in export_nodes:
        if obj.type != "MESH":
            continue
        # ワールド行列に座標変換を乗算してから頂点データに焼き込む
        obj.matrix_world = M_conv @ obj.matrix_world
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        log(f"Applied unity transform to: {obj.name}")

    # Empty はコレクション階層の構造保持のみ目的なので identity にリセット
    for obj in export_nodes:
        if obj.type == "EMPTY":
            obj.location = mathutils.Vector((0.0, 0.0, 0.0))
            obj.rotation_euler = mathutils.Euler((0.0, 0.0, 0.0))
            obj.scale = mathutils.Vector((1.0, 1.0, 1.0))


def export_fbx(objects_to_export: List[bpy.types.Object], fbx_path: Path) -> None:
    log(f"Exporting FBX: {fbx_path}")
    bpy.ops.object.select_all(action="DESELECT")
    export_candidates = [o for o in objects_to_export if o.type in INCLUDE_EXPORT_TYPES]
    for obj in export_candidates:
        obj.select_set(True)
    if export_candidates:
        bpy.context.view_layer.objects.active = export_candidates[0]

    bpy.ops.export_scene.fbx(
        filepath=str(fbx_path),
        check_existing=False,
        use_selection=True,
        path_mode="AUTO",
        embed_textures=False,
        apply_unit_scale=False,      # 頂点スケールは M_conv の ×100 で制御済み。二重スケールを防ぐ
        global_scale=1.0,
        axis_forward='-Z',
        axis_up='Y',                 # FBXヘッダーに Y-up を宣言
        use_space_transform=False,   # ノードへの座標変換追加を防ぐ (Rotation=0 維持)
        bake_space_transform=False,  # 頂点変換は bake_blender_to_unity_transform() で実施済み
    )


def main() -> None:
    export_dir = Path(EXPORT_DIR)
    export_dir = ensure_export_dir(str(export_dir))
    texture_dir = export_dir / "Textures"
    texture_dir.mkdir(parents=True, exist_ok=True)
    root_col = find_root_collection(ROOT_COLLECTION_NAME)

    log(f"Root collection: {root_col.name}")
    log(f"Export dir: {export_dir}")
    log(f"Texture dir: {texture_dir}")

    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = BAKE_SAMPLES

    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    prev_selected = list(bpy.context.selected_objects)
    prev_active = bpy.context.view_layer.objects.active

    tmp_col = None
    created_collections: List[bpy.types.Collection] = []
    src_to_dup: Dict[bpy.types.Object, bpy.types.Object] = {}
    dup_to_src: Dict[bpy.types.Object, bpy.types.Object] = {}
    export_nodes: List[bpy.types.Object] = []
    created_materials: List[bpy.types.Material] = []

    try:
        tmp_col, src_to_dup, dup_to_src, export_nodes, created_materials = duplicate_export_objects(scene, root_col)
        created_collections.append(tmp_col)
        dup_mesh_objects = [o for o in export_nodes if o.type == "MESH"]

        process_all_mesh_material_pairs(dup_mesh_objects, dup_to_src, texture_dir)

        # 全メッシュ頂点を Unity 座標系に変換し、全 Transform を identity にリセット
        bake_blender_to_unity_transform(export_nodes)

        fbx_path = export_dir / f"{ROOT_COLLECTION_NAME}.fbx"
        export_fbx(export_nodes, fbx_path)
        log("Done")

    finally:
        # Restore selection state.
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in prev_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if prev_active and prev_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = prev_active
        except Exception as ex:
            log(f"WARNING selection restore failed: {ex}")

        # Remove duplicated materials tracked in this run.
        for mat in created_materials:
            if mat and mat.name in bpy.data.materials:
                bpy.data.materials.remove(mat, do_unlink=True)

        # Fallback cleanup for stray export materials from previous failed runs.
        for mat in list(bpy.data.materials):
            if mat.name.endswith("__x"):
                try:
                    bpy.data.materials.remove(mat, do_unlink=True)
                except Exception as ex:
                    log(f"WARNING fallback material cleanup failed: {mat.name} / {ex}")

        # Cleanup only collections created by THIS run.
        for col in created_collections:
            if not col:
                continue
            if col.name not in bpy.data.collections:
                continue
            try:
                scene_col = bpy.context.scene.collection
                if col in scene_col.children:
                    scene_col.children.unlink(col)
                for parent in list(col.users_collection):
                    try:
                        parent.children.unlink(col)
                    except Exception:
                        pass
                for obj in list(col.objects):
                    if obj and obj.name in bpy.data.objects:
                        bpy.data.objects.remove(obj, do_unlink=True)
                bpy.data.collections.remove(col)
            except Exception as ex:
                log(f"WARNING temp collection cleanup failed: {col.name} / {ex}")

        # Optional cleanup for old leftovers, tagged only.
        if CLEAN_OLD_LEFTOVERS:
            for col in list(bpy.data.collections):
                if not col.name.startswith(TMP_COLLECTION_BASENAME):
                    continue
                if col.get("created_by_bake_export_script") is not True:
                    continue
                try:
                    scene_col = bpy.context.scene.collection
                    if col in scene_col.children:
                        scene_col.children.unlink(col)
                    for parent in list(col.users_collection):
                        try:
                            parent.children.unlink(col)
                        except Exception:
                            pass
                    for obj in list(col.objects):
                        if obj and obj.name in bpy.data.objects:
                            bpy.data.objects.remove(obj, do_unlink=True)
                    bpy.data.collections.remove(col)
                except Exception as ex:
                    log(f"WARNING old leftover cleanup failed: {col.name} / {ex}")


main()
