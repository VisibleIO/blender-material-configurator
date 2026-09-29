# SPDX-License-Identifier: GPL-3.0-or-later
"""VisibleIO Local — pick objects, combine their materials, render with one camera.

No network, no account, no updater. The camera is not part of the combination.
"""

import json
import os
import re

import bpy
from bpy.app.translations import pgettext_iface

_I18N = "VisibleIO"


def iface_(text):
	return pgettext_iface(text, _I18N)

bl_info = {
	"name": "VisibleIO Local",
	"author": "VisibleIO Dev Team",
	"version": (1, 0, 1),
	"blender": (4, 2, 0),
	"location": "View3D > Sidebar (N) > VisibleIO > VisibleIO Local",
	"description": "VisibleIO material configurator. PNGs stay on this computer.",
	"doc_url": "https://github.com/VisibleIO/blender-material-configurator",
	"category": "Material",
}

_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RENDERING = False
_RENDER_EVENT = ""
_PREVIEW_DIRTY = False
_SLOT_SYNC = False
_SUPPRESS_TASK_PREVIEW = False
_MAX_COMBINATIONS = 10000
_EMPTY_MATERIAL = "__EMPTY__"
_FILTER_ALL = "__ALL__"


def _on_render_complete(*_args):
	global _RENDER_EVENT
	_RENDER_EVENT = "complete"


def _on_render_cancel(*_args):
	global _RENDER_EVENT
	_RENDER_EVENT = "cancel"


def _render_handlers_on():
	if _on_render_complete not in bpy.app.handlers.render_complete:
		bpy.app.handlers.render_complete.append(_on_render_complete)
	if _on_render_cancel not in bpy.app.handlers.render_cancel:
		bpy.app.handlers.render_cancel.append(_on_render_cancel)


def _render_handlers_off():
	global _RENDER_EVENT
	for bucket, fn in (
		(bpy.app.handlers.render_complete, _on_render_complete),
		(bpy.app.handlers.render_cancel, _on_render_cancel),
	):
		if fn in bucket:
			bucket.remove(fn)
	_RENDER_EVENT = ""


def _has_materials(obj):
	return obj is not None and getattr(obj, "material_slots", None) is not None and len(obj.material_slots) > 0


def _safe_name(value, fallback):
	cleaned = _INVALID_FILENAME.sub("_", value or "").strip(" .")
	return cleaned or fallback


def _output_filename(task_name, camera_name, used, output_dir):
	"""Pick a PNG name that is free in this batch and not already on disk."""
	stem = f"{_safe_name(task_name, 'Task')}_{_safe_name(camera_name, 'Camera')}"
	candidate = stem
	suffix = 2
	while True:
		filename = candidate + ".png"
		taken = candidate.lower() in used or os.path.exists(os.path.join(output_dir, filename))
		if not taken:
			used.add(candidate.lower())
			return filename
		candidate = f"{stem}_{suffix}"
		suffix += 1


def _default_output_dir():
	if not bpy.data.filepath:
		return ""
	return bpy.path.abspath("//visibleio_local")


def _resolve_output_dir(settings):
	raw = (settings.output_dir or "").strip()
	if not raw:
		if not bpy.data.filepath:
			return None
		return _default_output_dir()
	resolved = bpy.path.abspath(raw)
	if not os.path.isabs(resolved):
		return None
	return resolved


def _display_output_dir(settings):
	if (settings.output_dir or "").strip():
		return settings.output_dir
	return "//visibleio_local/"


def _find_object(scene, name):
	obj = scene.objects.get(name) if scene else None
	if obj is None:
		obj = bpy.data.objects.get(name)
	return obj


def _find_material(material_name):
	"""Match a material by exact name, then by trimmed name."""
	if not material_name or material_name == _EMPTY_MATERIAL:
		return None
	material = bpy.data.materials.get(material_name)
	if material is not None:
		return material
	stripped = material_name.strip()
	if stripped and stripped != material_name:
		material = bpy.data.materials.get(stripped)
		if material is not None:
			return material
	target = stripped or material_name
	for material in bpy.data.materials:
		if material.name == material_name or material.name.strip() == target:
			return material
	return None


def _material_is_present(material_name):
	if material_name in ("", _EMPTY_MATERIAL):
		return True
	return _find_material(material_name) is not None


def _assign_slot(obj, slot_index, material_name):
	"""Write the mesh slot, then the object slot."""
	if obj is None or slot_index < 0:
		return False
	data = getattr(obj, "data", None)
	if data is None or not hasattr(data, "materials"):
		return False
	if material_name in ("", _EMPTY_MATERIAL):
		material = None
	else:
		material = _find_material(material_name)
		if material is None:
			return False
	try:
		while len(data.materials) <= slot_index:
			data.materials.append(None)
		data.materials[slot_index] = None
		data.materials[slot_index] = material
		if slot_index < len(obj.material_slots):
			obj.material_slots[slot_index].material = material
		obj.update_tag(refresh={"DATA"})
	except Exception:
		return False
	return True


def _row_label(object_name, material_name):
	shown = "" if material_name in ("", _EMPTY_MATERIAL) else material_name
	return f"{object_name} / {shown or iface_('(empty)')}"


def _apply_rows(scene, rows):
	"""Apply every slot of an object, or leave that object untouched when one slot is missing."""
	buckets = {}
	order = []
	for row in rows:
		name = row["object"]
		if name not in buckets:
			buckets[name] = []
			order.append(name)
		buckets[name].append(row)
	missing = []
	for name in order:
		obj_rows = buckets[name]
		obj = _find_object(scene, name)
		if not _has_materials(obj):
			missing.extend(_row_label(name, row["material"]) for row in obj_rows)
			continue
		bad = [row for row in obj_rows if not _material_is_present(row["material"])]
		if bad:
			missing.extend(_row_label(name, row["material"]) for row in bad)
			continue
		for row in obj_rows:
			if not _assign_slot(obj, row["slot"], row["material"]):
				missing.append(_row_label(name, row["material"]))
	return missing


def _snapshot_materials(scene, jobs):
	names = []
	seen = set()
	for job in jobs:
		for row in job["materials"]:
			name = row["object"]
			if name not in seen:
				seen.add(name)
				names.append(name)
	snapshot = {}
	for name in names:
		obj = _find_object(scene, name)
		if not _has_materials(obj):
			snapshot[name] = None
			continue
		snapshot[name] = [
			slot.material.name if slot.material else ""
			for slot in obj.material_slots
		]
	return snapshot


def _restore_materials(scene, snapshot):
	for name, slots in snapshot.items():
		if slots is None:
			continue
		obj = _find_object(scene, name)
		if not _has_materials(obj):
			continue
		for index, material_name in enumerate(slots):
			_assign_slot(obj, index, material_name)


def _load_baseline(settings):
	raw = settings.baseline_materials or ""
	if not raw:
		return {}
	try:
		data = json.loads(raw)
	except json.JSONDecodeError:
		return {}
	return data if isinstance(data, dict) else {}


def _store_baseline(settings, data):
	settings.baseline_materials = json.dumps(data, ensure_ascii=False)


def _slot_names(obj):
	return [slot.material.name if slot.material else "" for slot in obj.material_slots]


def _preview_active(settings):
	return _PREVIEW_DIRTY or bool(getattr(settings, "previewing", False))


def _set_previewing(settings, active):
	global _PREVIEW_DIRTY
	_PREVIEW_DIRTY = bool(active)
	if settings is not None:
		settings.previewing = bool(active)


def _clear_task_highlight(settings):
	"""A material preview is not a task. Drop the highlight so the same task can be selected again."""
	global _SUPPRESS_TASK_PREVIEW
	if settings is None or settings.active_task_index < 0:
		return
	_SUPPRESS_TASK_PREVIEW = True
	try:
		settings.active_task_index = -1
	finally:
		_SUPPRESS_TASK_PREVIEW = False


def _settle_saved_preview():
	"""A saved preview must not become the new baseline on the next session."""
	global _PREVIEW_DIRTY
	scenes = getattr(bpy.data, "scenes", None)
	if scenes is None:
		tries = getattr(_settle_saved_preview, "_tries", 0) + 1
		_settle_saved_preview._tries = tries
		if tries > 20:
			_settle_saved_preview._tries = 0
			return None
		return 0.1
	_settle_saved_preview._tries = 0
	for scene in scenes:
		settings = getattr(scene, "visibleio_local", None)
		if settings is None or not getattr(settings, "previewing", False):
			continue
		_restore_baseline(scene, settings)
		settings.previewing = False
	_PREVIEW_DIRTY = False
	return None


def _remember_objects(scene, settings, objects):
	"""Keep the pre-preview materials. Do not overwrite them once a preview is showing."""
	data = _load_baseline(settings)
	for obj in objects:
		if not _has_materials(obj):
			continue
		if obj.name in data and _preview_active(settings):
			continue
		data[obj.name] = _slot_names(obj)
	_store_baseline(settings, data)
	if not settings.baseline_camera and scene.camera is not None and scene.camera.type == "CAMERA":
		settings.baseline_camera = scene.camera.name


def _refresh_baseline(scene, settings):
	data = {}
	for setup in settings.objects:
		obj = _find_object(scene, setup.object_name)
		if _has_materials(obj):
			data[obj.name] = _slot_names(obj)
	_store_baseline(settings, data)
	settings.baseline_camera = scene.camera.name if scene.camera is not None and scene.camera.type == "CAMERA" else ""


def _restore_baseline(scene, settings):
	for name, slots in _load_baseline(settings).items():
		if not isinstance(slots, list):
			continue
		obj = _find_object(scene, name)
		if not _has_materials(obj):
			continue
		for index, material_name in enumerate(slots):
			_assign_slot(obj, index, material_name)
	camera_name = settings.baseline_camera
	if camera_name:
		camera = scene.objects.get(camera_name)
		if camera is not None and camera.type == "CAMERA":
			scene.camera = camera


def _restore_named(scene, settings, object_name):
	slots = _load_baseline(settings).get(object_name)
	obj = _find_object(scene, object_name)
	if not isinstance(slots, list) or not _has_materials(obj):
		return
	for index, material_name in enumerate(slots):
		_assign_slot(obj, index, material_name)


def _forget_object(settings, object_name):
	data = _load_baseline(settings)
	if object_name in data:
		del data[object_name]
		_store_baseline(settings, data)


def _original_slot_material(scene, settings, object_name, slot_index):
	slots = _load_baseline(settings).get(object_name)
	if isinstance(slots, list) and 0 <= slot_index < len(slots):
		return slots[slot_index]
	obj = _find_object(scene, object_name)
	if _has_materials(obj) and slot_index < len(obj.material_slots):
		material = obj.material_slots[slot_index].material
		return material.name if material else ""
	return ""


def _set_choice_material(item, material_name):
	value = material_name or _EMPTY_MATERIAL
	try:
		item.material_name = value
	except TypeError:
		item.material_name = _EMPTY_MATERIAL


def _sync_slot_overrides(scene, settings, setup):
	"""Other slots follow the selected combination material. They do not multiply the count."""
	global _SLOT_SYNC
	_SLOT_SYNC = True
	try:
		_sync_slot_overrides_now(scene, settings, setup)
	finally:
		_SLOT_SYNC = False


def _sync_slot_overrides_now(scene, settings, setup):
	primary = int(setup.slot_index or 0)
	obj = _find_object(scene, setup.object_name)
	slot_count = len(obj.material_slots) if _has_materials(obj) else 0
	for choice in setup.materials:
		kept = {}
		for item in choice.slots:
			if item.slot_index != primary and 0 <= item.slot_index < slot_count:
				kept[item.slot_index] = item.material_name
		choice.slots.clear()
		for slot_index in range(slot_count):
			if slot_index == primary:
				continue
			item = choice.slots.add()
			item.slot_index = slot_index
			if slot_index in kept:
				_set_choice_material(item, kept[slot_index])
			else:
				_set_choice_material(item, _original_slot_material(scene, settings, setup.object_name, slot_index))


def _stored_material_name(material_name):
	if material_name in ("", _EMPTY_MATERIAL):
		return ""
	return material_name


def _expand_rows(settings, primary_rows):
	"""Primary slot varies. Other slots follow that material."""
	rows = []
	for row in primary_rows:
		rows.append(row)
		setup = next((item for item in settings.objects if item.object_name == row["object"]), None)
		if setup is None:
			continue
		choice = next((item for item in setup.materials if item.material_name == row["material"]), None)
		if choice is None:
			continue
		for slot in choice.slots:
			if slot.slot_index == row["slot"]:
				continue
			rows.append({
				"object": row["object"],
				"slot": slot.slot_index,
				"material": _stored_material_name(slot.material_name),
			})
	return rows


def _jobs_from_tasks(tasks):
	jobs = []
	for task in tasks:
		jobs.append({
			"name": task.name,
			"camera": task.camera_name,
			"materials": [
				{
					"object": row.object_name,
					"slot": row.slot_index,
					"material": row.material_name,
				}
				for row in task.materials
			],
		})
	return jobs


def _set_status(context, text):
	context.window_manager.visibleio_local_status = text or ""


def _redraw_view3d(context):
	window_manager = getattr(context, "window_manager", None)
	if window_manager is None:
		return
	for window in window_manager.windows:
		screen = window.screen
		if screen is None:
			continue
		for area in screen.areas:
			if area.type == "VIEW_3D":
				area.tag_redraw()


def _preview_selected_task(settings, context):
	if _SUPPRESS_TASK_PREVIEW or _RENDERING or context is None or getattr(context, "scene", None) is None:
		return
	scene = context.scene
	if not _preview_active(settings):
		_refresh_baseline(scene, settings)
	_restore_baseline(scene, settings)
	index = settings.active_task_index
	if not (0 <= index < len(settings.tasks)):
		_set_previewing(settings, False)
		return
	task = settings.tasks[index]
	missing = _apply_rows(scene, _jobs_from_tasks([task])[0]["materials"])
	_set_previewing(settings, True)
	camera = scene.objects.get(task.camera_name) if task.camera_name else None
	if camera is not None and camera.type == "CAMERA":
		scene.camera = camera
	view_layer = getattr(context, "view_layer", None)
	if view_layer is not None:
		view_layer.update()
	_redraw_view3d(context)
	if missing:
		_set_status(context, iface_("Missing: %s") % ", ".join(missing[:6]))
	else:
		_set_status(context, iface_("Preview %s") % task.name)


def _preview_object_material(setup, context):
	"""Show the selected combination material on this object. Other objects return to their saved materials."""
	if _RENDERING or _SLOT_SYNC or context is None or getattr(context, "scene", None) is None:
		return
	scene = context.scene
	settings = getattr(scene, "visibleio_local", None)
	if settings is None:
		return
	index = setup.active_material_index
	if not (0 <= index < len(setup.materials)):
		_restore_named(scene, settings, setup.object_name)
		if 0 <= settings.active_task_index < len(settings.tasks):
			_preview_selected_task(settings, context)
		else:
			view_layer = getattr(context, "view_layer", None)
			if view_layer is not None:
				view_layer.update()
			_redraw_view3d(context)
		return
	if not _preview_active(settings):
		_refresh_baseline(scene, settings)
	_restore_baseline(scene, settings)
	choice = setup.materials[index]
	primary = int(setup.slot_index or 0)
	rows = [{
		"object": setup.object_name,
		"slot": primary,
		"material": choice.material_name,
	}]
	for slot in choice.slots:
		if slot.slot_index == primary:
			continue
		rows.append({
			"object": setup.object_name,
			"slot": slot.slot_index,
			"material": _stored_material_name(slot.material_name),
		})
	missing = _apply_rows(scene, rows)
	_set_previewing(settings, True)
	_clear_task_highlight(settings)
	view_layer = getattr(context, "view_layer", None)
	if view_layer is not None:
		view_layer.update()
	_redraw_view3d(context)
	if missing:
		_set_status(context, iface_("Missing: %s") % ", ".join(missing[:6]))
	else:
		_set_status(context, iface_("Preview %s / %s") % (setup.object_name, choice.material_name))


def _on_active_material_changed(self, context):
	_preview_object_material(self, context)


def _on_slot_override_changed(self, context):
	if _SLOT_SYNC or _RENDERING or context is None or getattr(context, "scene", None) is None:
		return
	settings = context.scene.visibleio_local
	pointer = self.as_pointer()
	for setup in settings.objects:
		index = setup.active_material_index
		if not (0 <= index < len(setup.materials)):
			continue
		choice = setup.materials[index]
		if any(slot.as_pointer() == pointer for slot in choice.slots):
			_preview_object_material(setup, context)
			return


def _candidate_objects(context):
	objects = list(getattr(context, "selected_objects", None) or [])
	active = getattr(context, "active_object", None)
	if active is not None:
		objects.append(active)
	unique = []
	seen = set()
	for obj in objects:
		if obj is None:
			continue
		key = obj.as_pointer()
		if key in seen:
			continue
		seen.add(key)
		unique.append(obj)
	return unique


def _ordered_material_names(setup, context):
	if setup is None:
		return []
	used = {choice.material_name for choice in setup.materials}
	ordered = []
	scene = getattr(context, "scene", None) if context else None
	obj = scene.objects.get(setup.object_name) if scene is not None else None
	if obj is not None:
		for slot in obj.material_slots:
			material = slot.material
			if material is not None and material.name and material.name not in used and material.name not in ordered:
				ordered.append(material.name)
	for mat in bpy.data.materials:
		if mat.name and mat.name not in used and mat.name not in ordered:
			ordered.append(mat.name)
	return ordered


def _material_items(self, context):
	names = _ordered_material_names(self, context)
	if not names:
		return [("__NONE__", iface_("No materials"), "")]
	return [(name, name, "") for name in names]


def _available_camera_names(context):
	scene = getattr(context, "scene", None) if context else None
	settings = getattr(scene, "visibleio_local", None) if scene else None
	if scene is None or settings is None:
		return []
	existing = set(_selected_camera_names(settings))
	return [obj.name for obj in scene.objects if obj.type == "CAMERA" and obj.name and obj.name not in existing]


def _ordered_camera_names(context):
	names = _available_camera_names(context)
	scene = getattr(context, "scene", None) if context else None
	camera = getattr(scene, "camera", None) if scene is not None else None
	if camera is not None and camera.type == "CAMERA" and camera.name in names:
		names = [camera.name] + [name for name in names if name != camera.name]
	return names


def _camera_items(self, context):
	names = _ordered_camera_names(context)
	if not names:
		return [("__NONE__", iface_("No camera"), "")]
	return [(name, name, "") for name in names]


def _shown_choice(current, names):
	if current in names:
		return current
	return names[0] if names else ""


def _optional_material_items(self, context):
	items = [(_EMPTY_MATERIAL, iface_("(empty)"), "")]
	for mat in bpy.data.materials:
		if mat.name:
			items.append((mat.name, mat.name, ""))
	return items


def _on_primary_slot_changed(self, context):
	scene = getattr(context, "scene", None) if context else None
	if scene is None:
		return
	_sync_slot_overrides(scene, scene.visibleio_local, self)
	_preview_object_material(self, context)


def _active_axes(settings):
	axes = []
	for setup in settings.objects:
		names = []
		seen = set()
		for choice in setup.materials:
			name = choice.material_name
			if name and name not in seen:
				seen.add(name)
				names.append(name)
		if names:
			axes.append({
				"object": setup.object_name,
				"slot": int(setup.slot_index or 0),
				"materials": names,
			})
	return axes


def _combination_count(axes):
	if not axes:
		return 0
	total = 1
	for axis in axes:
		total *= len(axis["materials"])
	return total


def _object_in_frustum(scene, camera_obj, target_obj):
	"""True when any bound-box corner lies inside the camera view."""
	try:
		import bpy_extras.object_utils
		from mathutils import Vector

		if target_obj.type in {"MESH", "CURVE", "SURFACE", "FONT"}:
			corners = [target_obj.matrix_world @ Vector(corner) for corner in target_obj.bound_box]
		else:
			corners = [target_obj.matrix_world.translation]
		for corner in corners:
			co = bpy_extras.object_utils.world_to_camera_view(scene, camera_obj, corner)
			if 0.0 <= co.x <= 1.0 and 0.0 <= co.y <= 1.0 and co.z > 0.0:
				return True
		return False
	except Exception:
		return False


def _axes_for_camera(scene, settings, camera_name):
	camera = scene.objects.get(camera_name) if scene is not None else None
	if camera is None or camera.type != "CAMERA":
		return []
	kept = []
	for axis in _active_axes(settings):
		obj = _find_object(scene, axis["object"])
		if obj is not None and _object_in_frustum(scene, camera, obj):
			kept.append(axis)
	return kept


def _selected_camera_names(settings):
	names = []
	seen = set()
	for item in settings.cameras:
		name = item.camera_name
		if name and name not in seen:
			seen.add(name)
			names.append(name)
	return names


def _camera_plans(scene, settings):
	plans = []
	for name in _selected_camera_names(settings):
		camera = scene.objects.get(name) if scene is not None else None
		if camera is None or camera.type != "CAMERA":
			plans.append({
				"camera": name,
				"axes": [],
				"count": 0,
				"missing": True,
			})
			continue
		axes = _axes_for_camera(scene, settings, name)
		plans.append({
			"camera": name,
			"axes": axes,
			"count": _combination_count(axes),
			"missing": False,
		})
	return plans


def _total_count(plans):
	return sum(plan["count"] for plan in plans)


def _plan_label(plan):
	if plan.get("missing"):
		return iface_("Camera is missing: %s") % plan["camera"]
	if plan["axes"]:
		parts = " × ".join(str(len(axis["materials"])) for axis in plan["axes"])
		return iface_("%s: %s = %d") % (plan["camera"], parts, plan["count"])
	return iface_("%s: %d") % (plan["camera"], plan["count"])


def _secondary_key(settings, object_name, material_names):
	setup = next((item for item in settings.objects if item.object_name == object_name), None)
	if setup is None:
		return ""
	chunks = []
	for name in material_names:
		choice = next((item for item in setup.materials if item.material_name == name), None)
		if choice is None:
			continue
		slots = ",".join(
			f"{item.slot_index}:{_stored_material_name(item.material_name)}"
			for item in choice.slots
		)
		chunks.append(f"{name}@{slots}")
	return "|".join(chunks)


def _fingerprint(scene, settings):
	parts = []
	for plan in _camera_plans(scene, settings):
		if plan.get("missing"):
			parts.append(f"{plan['camera']}:missing")
		else:
			parts.append(f"{plan['camera']}:{_axis_fingerprint(settings, plan['axes'])}")
	return "|".join(parts)


def _axis_fingerprint(settings, axes):
	return ";".join(
		f"{axis['object']}|{axis['slot']}|{','.join(axis['materials'])}|{_secondary_key(settings, axis['object'], axis['materials'])}"
		for axis in axes
	)


def _iter_combinations(axes):
	pools = [axis["materials"] for axis in axes]
	indexes = [0] * len(pools)
	while True:
		yield [
			{
				"object": axes[i]["object"],
				"slot": axes[i]["slot"],
				"material": pools[i][indexes[i]],
			}
			for i in range(len(axes))
		]
		cursor = len(indexes) - 1
		while cursor >= 0:
			indexes[cursor] += 1
			if indexes[cursor] < len(pools[cursor]):
				break
			indexes[cursor] = 0
			cursor -= 1
		else:
			return


def _task_name(index, camera_name, rows):
	materials = "_".join(_safe_name(row["material"], "Material") for row in rows)
	camera = _safe_name(camera_name, "Camera")
	return f"{index + 1:03d}_{camera}_{materials}"[:80]


def _view3d_window(context):
	area = context.area if context.area is not None and context.area.type == "VIEW_3D" else None
	screen = context.screen
	if area is None and screen is not None:
		for candidate in screen.areas:
			if candidate.type == "VIEW_3D":
				area = candidate
				break
	if area is None:
		return None, None
	for region in area.regions:
		if region.type == "WINDOW":
			return area, region
	return None, None


def _slot_items(self, context):
	obj = None
	scene = getattr(context, "scene", None) if context else None
	if scene is not None:
		obj = scene.objects.get(self.object_name)
	if obj is None:
		obj = bpy.data.objects.get(self.object_name)
	items = []
	slots = getattr(obj, "material_slots", None) if obj is not None else None
	if slots:
		for index, slot in enumerate(slots):
			label = slot.material.name if slot.material else iface_("(empty)")
			items.append((str(index), f"{index}: {label}", ""))
	if not items:
		items.append(("0", "0", ""))
	return items


class VisibleIOLocalSlotOverride(bpy.types.PropertyGroup):
	slot_index: bpy.props.IntProperty(name="Slot", min=0)
	material_name: bpy.props.EnumProperty(
		name="Material",
		description="Material for this slot in the selected combination",
		items=_optional_material_items,
		update=_on_slot_override_changed,
	)


class VisibleIOLocalMaterialChoice(bpy.types.PropertyGroup):
	material_name: bpy.props.StringProperty(name="Material")
	slots: bpy.props.CollectionProperty(type=VisibleIOLocalSlotOverride)


class VisibleIOLocalAssignment(bpy.types.PropertyGroup):
	object_name: bpy.props.StringProperty(name="Object")
	slot_index: bpy.props.IntProperty(name="Slot", min=0)
	material_name: bpy.props.StringProperty(name="Material")


class VisibleIOLocalObject(bpy.types.PropertyGroup):
	object_name: bpy.props.StringProperty(name="Object")
	slot_index: bpy.props.EnumProperty(
		name="Slot",
		description="Material slot on this object",
		items=_slot_items,
		update=_on_primary_slot_changed,
	)
	materials: bpy.props.CollectionProperty(type=VisibleIOLocalMaterialChoice)
	active_material_index: bpy.props.IntProperty(name="Material", default=0, update=_on_active_material_changed)
	material_to_add: bpy.props.EnumProperty(
		name="Material",
		description="Material to add to this object's combinations",
		items=_material_items,
	)


def _unique_task_values(settings, kind):
	seen = set()
	values = []
	for task in settings.tasks:
		if kind == "camera":
			names = [task.camera_name]
		elif kind == "object":
			names = [row.object_name for row in task.materials]
		else:
			names = [row.material_name for row in task.materials if row.material_name]
		for name in names:
			if name and name not in seen:
				seen.add(name)
				values.append(name)
	return values


def _filter_items(self, context, kind):
	items = [(_FILTER_ALL, iface_("All"), "")]
	scene = getattr(context, "scene", None) if context else None
	settings = getattr(scene, "visibleio_local", None) if scene else None
	if settings is None:
		return items
	for name in _unique_task_values(settings, kind):
		items.append((name, name, ""))
	return items


def _task_camera_filter_items(self, context):
	return _filter_items(self, context, "camera")


def _task_object_filter_items(self, context):
	return _filter_items(self, context, "object")


def _task_material_filter_items(self, context):
	return _filter_items(self, context, "material")


def _task_matches_filter(task, settings):
	camera = settings.task_camera_filter
	if camera and camera != _FILTER_ALL and task.camera_name != camera:
		return False
	object_name = settings.task_object_filter
	material_name = settings.task_material_filter
	need_object = bool(object_name and object_name != _FILTER_ALL)
	need_material = bool(material_name and material_name != _FILTER_ALL)
	if not need_object and not need_material:
		return True
	found_object = not need_object
	found_material = not need_material
	for row in task.materials:
		if need_object and row.object_name == object_name:
			found_object = True
		if need_material and row.material_name == material_name:
			found_material = True
		if found_object and found_material:
			return True
	return False


def _visible_task_count(settings):
	return sum(1 for task in settings.tasks if _task_matches_filter(task, settings))


def _on_task_filter_changed(self, context):
	if _RENDERING or context is None or getattr(context, "scene", None) is None:
		return
	index = self.active_task_index
	if 0 <= index < len(self.tasks) and _task_matches_filter(self.tasks[index], self):
		return
	for next_index, task in enumerate(self.tasks):
		if _task_matches_filter(task, self):
			self.active_task_index = next_index
			return


class VisibleIOLocalCamera(bpy.types.PropertyGroup):
	camera_name: bpy.props.StringProperty(name="Camera")


class VisibleIOLocalTask(bpy.types.PropertyGroup):
	name: bpy.props.StringProperty(name="Name", default="Task")
	camera_name: bpy.props.StringProperty(name="Camera")
	materials: bpy.props.CollectionProperty(type=VisibleIOLocalAssignment)


class VisibleIOLocalSettings(bpy.types.PropertyGroup):
	objects: bpy.props.CollectionProperty(type=VisibleIOLocalObject)
	active_object_index: bpy.props.IntProperty(name="Object", default=0)
	tasks: bpy.props.CollectionProperty(type=VisibleIOLocalTask)
	active_task_index: bpy.props.IntProperty(
		name="Task",
		default=0,
		min=-1,
		update=_preview_selected_task,
	)
	task_fingerprint: bpy.props.StringProperty(name="Fingerprint", default="")
	task_camera_filter: bpy.props.EnumProperty(
		name="Camera Filter",
		description="Show tasks for this camera",
		translation_context=_I18N,
		items=_task_camera_filter_items,
		default=0,
		update=_on_task_filter_changed,
	)
	task_object_filter: bpy.props.EnumProperty(
		name="Object",
		description="Show tasks that include this object",
		translation_context=_I18N,
		items=_task_object_filter_items,
		default=0,
		update=_on_task_filter_changed,
	)
	task_material_filter: bpy.props.EnumProperty(
		name="Material",
		description="Show tasks that use this material",
		translation_context=_I18N,
		items=_task_material_filter_items,
		default=0,
		update=_on_task_filter_changed,
	)
	cameras: bpy.props.CollectionProperty(type=VisibleIOLocalCamera)
	active_camera_index: bpy.props.IntProperty(name="Camera", default=0)
	camera_to_add: bpy.props.EnumProperty(
		name="Camera",
		description="Camera to add. Objects outside its frustum are excluded",
		items=_camera_items,
	)
	output_dir: bpy.props.StringProperty(
		name="Output Folder",
		description="Empty saves PNGs next to this file, in visibleio_local/",
		subtype="DIR_PATH",
		default="",
	)
	baseline_camera: bpy.props.StringProperty(name="Baseline Camera", default="", options={"HIDDEN"})
	baseline_materials: bpy.props.StringProperty(name="Baseline Materials", default="", options={"HIDDEN"})
	previewing: bpy.props.BoolProperty(name="Previewing", default=False, options={"HIDDEN"})


class VISIBLEIO_LOCAL_UL_objects(bpy.types.UIList):
	bl_idname = "VISIBLEIO_LOCAL_UL_objects"

	def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
		layout.label(text=f"{item.object_name}  ({len(item.materials)})", icon="OBJECT_DATA", translate=False)


class VISIBLEIO_LOCAL_UL_materials(bpy.types.UIList):
	bl_idname = "VISIBLEIO_LOCAL_UL_materials"

	def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
		layout.label(text=item.material_name or iface_("(empty)"), icon="MATERIAL", translate=False)


class VISIBLEIO_LOCAL_UL_tasks(bpy.types.UIList):
	bl_idname = "VISIBLEIO_LOCAL_UL_tasks"

	def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
		layout.label(text=item.name, icon="RENDER_STILL", translate=False)

	def filter_items(self, context, data, propname):
		tasks = getattr(data, propname)
		flags = [self.bitflag_filter_item] * len(tasks)
		name_filter = (self.filter_name or "").lower()
		for index, task in enumerate(tasks):
			if not _task_matches_filter(task, data):
				flags[index] = 0
			elif name_filter and name_filter not in task.name.lower():
				flags[index] = 0
		return flags, []


class VISIBLEIO_LOCAL_UL_cameras(bpy.types.UIList):
	bl_idname = "VISIBLEIO_LOCAL_UL_cameras"

	def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
		layout.label(text=item.camera_name, icon="CAMERA_DATA", translate=False)


class VISIBLEIO_LOCAL_OT_camera_add(bpy.types.Operator):
	bl_idname = "visibleio_local.camera_add"
	bl_label = "Add Camera"
	bl_description = "Camera to add. Objects outside its frustum are excluded"
	bl_translation_context = _I18N
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		return context.scene is not None and not _RENDERING

	def execute(self, context):
		settings = context.scene.visibleio_local
		selected = [obj.name for obj in _candidate_objects(context) if obj.type == "CAMERA"]
		available = _ordered_camera_names(context)
		names = [name for name in selected if name in available]
		if not names:
			shown = _shown_choice(settings.camera_to_add, available)
			names = [shown] if shown else []
		if not names:
			if any(obj.type == "CAMERA" for obj in context.scene.objects):
				name = next(obj.name for obj in context.scene.objects if obj.type == "CAMERA")
				self.report({"INFO"}, iface_("%s is already included") % name)
			else:
				self.report({"WARNING"}, iface_("Add a camera to the scene"))
			return {"CANCELLED"}
		existing = set(_selected_camera_names(settings))
		for name in names:
			if name in existing:
				continue
			item = settings.cameras.add()
			item.camera_name = name
			existing.add(name)
			settings.active_camera_index = len(settings.cameras) - 1
		remaining = _ordered_camera_names(context)
		if remaining:
			settings.camera_to_add = remaining[0]
		return {"FINISHED"}


class VISIBLEIO_LOCAL_OT_camera_remove(bpy.types.Operator):
	bl_idname = "visibleio_local.camera_remove"
	bl_label = "Remove Camera"
	bl_translation_context = _I18N
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		if _RENDERING:
			return False
		settings = getattr(context.scene, "visibleio_local", None)
		return settings is not None and 0 <= settings.active_camera_index < len(settings.cameras)

	def execute(self, context):
		settings = context.scene.visibleio_local
		index = settings.active_camera_index
		settings.cameras.remove(index)
		settings.active_camera_index = min(index, len(settings.cameras) - 1)
		return {"FINISHED"}


class VISIBLEIO_LOCAL_OT_object_add(bpy.types.Operator):
	bl_idname = "visibleio_local.object_add"
	bl_label = "Add Selected"
	bl_description = "Add the selected objects. Each object's current slot material is included"
	bl_translation_context = _I18N
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		return context.scene is not None and not _RENDERING

	def execute(self, context):
		scene = context.scene
		settings = scene.visibleio_local
		existing = {item.object_name for item in settings.objects}
		added = 0
		duplicate = ""
		for obj in _candidate_objects(context):
			if obj.type == "CAMERA" or not _has_materials(obj):
				continue
			if obj.name in existing:
				duplicate = duplicate or obj.name
				continue
			_remember_objects(scene, settings, [obj])
			setup = settings.objects.add()
			setup.object_name = obj.name
			slot = obj.active_material_index
			if slot < 0 or slot >= len(obj.material_slots):
				slot = 0
			setup.slot_index = str(slot)
			current = obj.material_slots[slot].material
			if current is not None:
				choice = setup.materials.add()
				choice.material_name = current.name
			_sync_slot_overrides(scene, settings, setup)
			existing.add(obj.name)
			added += 1
			settings.active_object_index = len(settings.objects) - 1
			remaining = _ordered_material_names(setup, context)
			if remaining:
				setup.material_to_add = remaining[0]
		if added == 0:
			if duplicate:
				self.report({"INFO"}, iface_("%s is already included") % duplicate)
			else:
				self.report({"WARNING"}, iface_("Select an object that has a material slot"))
			return {"CANCELLED"}
		return {"FINISHED"}


class VISIBLEIO_LOCAL_OT_object_remove(bpy.types.Operator):
	bl_idname = "visibleio_local.object_remove"
	bl_label = "Remove Object"
	bl_translation_context = _I18N
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		if _RENDERING:
			return False
		settings = getattr(context.scene, "visibleio_local", None)
		return settings is not None and 0 <= settings.active_object_index < len(settings.objects)

	def execute(self, context):
		scene = context.scene
		settings = scene.visibleio_local
		index = settings.active_object_index
		object_name = settings.objects[index].object_name
		_restore_named(scene, settings, object_name)
		_forget_object(settings, object_name)
		settings.objects.remove(index)
		settings.active_object_index = min(index, len(settings.objects) - 1)
		if _preview_active(settings):
			_preview_selected_task(settings, context)
		return {"FINISHED"}


class VISIBLEIO_LOCAL_OT_material_add(bpy.types.Operator):
	bl_idname = "visibleio_local.material_add"
	bl_label = "Add Material"
	bl_description = "Add this material to the selected object's combinations"
	bl_translation_context = _I18N
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		if _RENDERING:
			return False
		settings = getattr(context.scene, "visibleio_local", None)
		return settings is not None and 0 <= settings.active_object_index < len(settings.objects)

	def execute(self, context):
		scene = context.scene
		settings = scene.visibleio_local
		setup = settings.objects[settings.active_object_index]
		name = _shown_choice(setup.material_to_add, _ordered_material_names(setup, context))
		if not name:
			if setup.materials:
				self.report({"INFO"}, iface_("%s is already included") % setup.materials[-1].material_name)
			else:
				self.report({"WARNING"}, iface_("Create a material first"))
			return {"CANCELLED"}
		choice = setup.materials.add()
		choice.material_name = name
		_sync_slot_overrides(scene, settings, setup)
		setup.active_material_index = len(setup.materials) - 1
		remaining = _ordered_material_names(setup, context)
		setup.material_to_add = remaining[0] if remaining else "__NONE__"
		_preview_object_material(setup, context)
		return {"FINISHED"}


class VISIBLEIO_LOCAL_OT_material_remove(bpy.types.Operator):
	bl_idname = "visibleio_local.material_remove"
	bl_label = "Remove Material"
	bl_translation_context = _I18N
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		if _RENDERING:
			return False
		settings = getattr(context.scene, "visibleio_local", None)
		if settings is None or not (0 <= settings.active_object_index < len(settings.objects)):
			return False
		setup = settings.objects[settings.active_object_index]
		return 0 <= setup.active_material_index < len(setup.materials)

	def execute(self, context):
		scene = context.scene
		settings = scene.visibleio_local
		setup = settings.objects[settings.active_object_index]
		index = setup.active_material_index
		setup.materials.remove(index)
		if setup.materials:
			setup.active_material_index = min(index, len(setup.materials) - 1)
			_preview_object_material(setup, context)
		else:
			_restore_named(scene, settings, setup.object_name)
			setup.active_material_index = 0
		return {"FINISHED"}


class VISIBLEIO_LOCAL_OT_generate(bpy.types.Operator):
	bl_idname = "visibleio_local.generate"
	bl_label = "Compile Render Queue"
	bl_description = "Compile one variant per material combination inside each camera view"
	bl_translation_context = _I18N
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		return context.scene is not None and not _RENDERING

	def execute(self, context):
		scene = context.scene
		settings = scene.visibleio_local
		if not _active_axes(settings):
			self.report({"WARNING"}, iface_("Add objects and at least one material"))
			return {"CANCELLED"}
		for setup in settings.objects:
			_sync_slot_overrides(scene, settings, setup)
		plans = _camera_plans(scene, settings)
		if not plans:
			self.report({"WARNING"}, iface_("Add one or more cameras"))
			return {"CANCELLED"}
		missing_cameras = [plan["camera"] for plan in plans if plan.get("missing")]
		if missing_cameras:
			self.report({"WARNING"}, iface_("Camera is missing: %s") % missing_cameras[0])
			return {"CANCELLED"}
		count = _total_count(plans)
		if count == 0:
			self.report({"WARNING"}, iface_("No selected objects are inside the camera frustums"))
			return {"CANCELLED"}
		if count > _MAX_COMBINATIONS:
			self.report({"ERROR"}, iface_("%d combinations is over the limit of %d") % (count, _MAX_COMBINATIONS))
			return {"CANCELLED"}
		settings.tasks.clear()
		index = 0
		for plan in plans:
			if plan["count"] == 0:
				continue
			for rows in _iter_combinations(plan["axes"]):
				task = settings.tasks.add()
				task.name = _task_name(index, plan["camera"], rows)
				task.camera_name = plan["camera"]
				for row in _expand_rows(settings, rows):
					item = task.materials.add()
					item.object_name = row["object"]
					item.slot_index = row["slot"]
					item.material_name = row["material"]
				index += 1
		settings.task_camera_filter = _FILTER_ALL
		settings.task_object_filter = _FILTER_ALL
		settings.task_material_filter = _FILTER_ALL
		settings.active_task_index = 0
		_preview_selected_task(settings, context)
		settings.task_fingerprint = _fingerprint(scene, settings)
		text = iface_("Compiled %d variants") % count
		_set_status(context, text)
		self.report({"INFO"}, text)
		return {"FINISHED"}


class VISIBLEIO_LOCAL_OT_render_all(bpy.types.Operator):
	bl_idname = "visibleio_local.render_all"
	bl_label = "Start Batch Render"
	bl_description = "Render every compiled variant in its camera window"
	bl_translation_context = _I18N
	bl_options = {"REGISTER"}

	@classmethod
	def poll(cls, context):
		return not _RENDERING and context.scene is not None

	def execute(self, context):
		error = self._prepare(context)
		if error:
			self.report({"ERROR"}, error)
			return {"CANCELLED"}
		try:
			while self._index < len(self._jobs):
				self._render_one(context)
				self._index += 1
		except Exception as exc:
			self._finish(context, cancelled=True, error=str(exc))
			return {"CANCELLED"}
		self._finish(context, cancelled=False)
		return {"FINISHED"}

	def invoke(self, context, event):
		error = self._prepare(context)
		if error:
			self.report({"ERROR"}, error)
			return {"CANCELLED"}
		if context.window is None:
			return self.execute(context)
		global _RENDER_EVENT
		_RENDER_EVENT = ""
		self._use_window = True
		self._phase = "launch"
		try:
			self._display_type = context.preferences.view.render_display_type
			context.preferences.view.render_display_type = "WINDOW"
		except Exception:
			self._display_type = ""
		_render_handlers_on()
		self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
		_set_status(context, iface_("Rendering %d/%d: %s") % (1, len(self._jobs), self._jobs[0]["name"]))
		context.window_manager.modal_handler_add(self)
		return {"RUNNING_MODAL"}

	def _prepare(self, context):
		global _RENDERING
		scene = context.scene
		settings = scene.visibleio_local
		if not settings.tasks:
			return iface_("Compile the render queue first")
		if settings.task_fingerprint != _fingerprint(scene, settings):
			return iface_("Frustum changed — Recompile queue")
		jobs = _jobs_from_tasks(settings.tasks)
		for job in jobs:
			camera = scene.objects.get(job.get("camera") or "")
			if camera is None or camera.type != "CAMERA":
				return iface_("Camera is missing: %s") % (job.get("camera") or job["name"])
		output_dir = _resolve_output_dir(settings)
		if not output_dir:
			return iface_("Save this .blend, or set an output folder")
		_restore_baseline(scene, settings)
		_set_previewing(settings, False)
		area, region = _view3d_window(context)
		self._jobs = jobs
		self._index = 0
		self._output_dir = output_dir
		self._used_names = set()
		self._missing = []
		self._written = []
		self._snapshot = _snapshot_materials(scene, jobs)
		self._camera_name = scene.camera.name if scene.camera else ""
		image = scene.render.image_settings
		self._file_format = image.file_format
		self._color_depth = image.color_depth
		self._filepath = scene.render.filepath
		self._window = context.window
		self._screen = getattr(context, "screen", None)
		self._area = area
		self._region = region
		self._timer = None
		self._use_window = False
		self._phase = ""
		self._pending_path = ""
		self._pending_filename = ""
		self._display_type = ""
		self._saw_render = False
		self._stable_idle = 0
		_RENDERING = True
		return ""

	def modal(self, context, event):
		if event.type == "ESC" and self._phase != "wait":
			self._finish(context, cancelled=True)
			return {"CANCELLED"}
		if event.type != "TIMER":
			return {"PASS_THROUGH"}
		global _RENDER_EVENT
		try:
			if self._phase == "launch":
				status = self._launch_window_render(context)
				if status == "busy":
					return {"PASS_THROUGH"}
				self._phase = "wait"
				if status == "finished":
					_RENDER_EVENT = "complete"
				return {"PASS_THROUGH"}
			if self._phase == "wait" and bpy.app.is_job_running("RENDER"):
				self._saw_render = True
				self._stable_idle = 0
			elif (
				self._phase == "wait"
				and self._saw_render
				and _RENDER_EVENT == ""
				and os.path.isfile(self._pending_path)
			):
				self._stable_idle += 1
				if self._stable_idle >= 5:
					_RENDER_EVENT = "complete"
			if _RENDER_EVENT == "cancel":
				self._finish(context, cancelled=True)
				return {"CANCELLED"}
			if _RENDER_EVENT == "complete":
				if bpy.app.is_job_running("RENDER"):
					return {"PASS_THROUGH"}
				self._save_launched(context)
				self._index += 1
				if self._index >= len(self._jobs):
					self._finish(context, cancelled=False)
					return {"FINISHED"}
				job = self._jobs[self._index]
				_set_status(context, iface_("Rendering %d/%d: %s") % (self._index + 1, len(self._jobs), job["name"]))
				self._redraw()
				self._phase = "launch"
		except Exception as exc:
			self._finish(context, cancelled=True, error=str(exc))
			return {"CANCELLED"}
		return {"PASS_THROUGH"}

	def _launch_window_render(self, context):
		global _RENDER_EVENT
		if bpy.app.is_job_running("RENDER"):
			return "busy"
		scene = context.scene
		job = self._jobs[self._index]
		_RENDER_EVENT = ""
		self._saw_render = False
		self._stable_idle = 0
		_restore_materials(scene, self._snapshot)
		missing = _apply_rows(scene, job["materials"])
		if missing:
			raise RuntimeError(iface_("Missing: %s") % ", ".join(missing[:6]))
		camera = scene.objects.get(job.get("camera") or "")
		if camera is None or camera.type != "CAMERA":
			raise RuntimeError(iface_("Camera is missing"))
		scene.camera = camera
		context.view_layer.update()
		image = scene.render.image_settings
		image.file_format = "PNG"
		if image.color_depth not in {"8", "16"}:
			image.color_depth = "8"
		os.makedirs(self._output_dir, exist_ok=True)
		filename = _output_filename(job["name"], camera.name, self._used_names, self._output_dir)
		filepath = os.path.join(self._output_dir, filename)
		self._pending_filename = filename
		self._pending_path = filepath
		scene.render.filepath = filepath
		result = bpy.ops.render.render("INVOKE_DEFAULT", write_still=True)
		if result == {"CANCELLED"}:
			if bpy.app.is_job_running("RENDER"):
				return "started"
			raise RuntimeError(iface_("Render failed for %s") % job["name"])
		if result == {"FINISHED"}:
			return "finished"
		return "started"

	def _save_launched(self, context):
		if not os.path.isfile(self._pending_path):
			render_result = bpy.data.images.get("Render Result")
			if render_result is None:
				raise RuntimeError(iface_("Render Result is missing"))
			render_result.save_render(self._pending_path, scene=context.scene)
		self._written.append(self._pending_filename)

	def _render_one(self, context):
		scene = context.scene
		job = self._jobs[self._index]
		_restore_materials(scene, self._snapshot)
		missing = _apply_rows(scene, job["materials"])
		if missing:
			raise RuntimeError(iface_("Missing: %s") % ", ".join(missing[:6]))
		camera = scene.objects.get(job.get("camera") or "")
		if camera is None or camera.type != "CAMERA":
			raise RuntimeError(iface_("Camera is missing"))
		scene.camera = camera
		context.view_layer.update()

		image = scene.render.image_settings
		image.file_format = "PNG"
		if image.color_depth not in {"8", "16"}:
			image.color_depth = "8"

		os.makedirs(self._output_dir, exist_ok=True)
		filename = _output_filename(job["name"], camera.name, self._used_names, self._output_dir)
		filepath = os.path.join(self._output_dir, filename)
		if self._window is not None and self._area is not None and self._region is not None:
			override = {
				"window": self._window,
				"screen": self._screen,
				"area": self._area,
				"region": self._region,
				"scene": scene,
			}
			with context.temp_override(**override):
				result = bpy.ops.render.render(write_still=False)
		else:
			result = bpy.ops.render.render(write_still=False)
		if result != {"FINISHED"}:
			raise RuntimeError(iface_("Render failed for %s") % job["name"])
		render_result = bpy.data.images.get("Render Result")
		if render_result is None:
			raise RuntimeError(iface_("Render Result is missing"))
		render_result.save_render(filepath, scene=scene)
		self._written.append(filename)

	def _finish(self, context, cancelled, error=""):
		global _RENDERING
		scene = context.scene
		try:
			_restore_materials(scene, self._snapshot)
			if self._camera_name:
				original = scene.objects.get(self._camera_name)
				if original is not None and original.type == "CAMERA":
					scene.camera = original
			image = scene.render.image_settings
			image.file_format = self._file_format
			try:
				image.color_depth = self._color_depth
			except TypeError:
				pass
			scene.render.filepath = self._filepath
			if self._display_type:
				try:
					context.preferences.view.render_display_type = self._display_type
				except Exception:
					pass
				self._display_type = ""
			context.view_layer.update()
		finally:
			_render_handlers_off()
			if self._timer is not None:
				context.window_manager.event_timer_remove(self._timer)
				self._timer = None
			_RENDERING = False
			_set_previewing(scene.visibleio_local, False)

		if error:
			text = error
			self.report({"ERROR"}, error)
		elif cancelled:
			text = iface_("Cancelled. Wrote %d stills to %s") % (len(self._written), _display_output_dir(scene.visibleio_local))
			self.report({"WARNING"}, iface_("Render cancelled"))
		else:
			text = iface_("%d tasks rendered successfully") % len(self._written)
			self.report({"INFO"}, text)
		if self._missing:
			preview = "; ".join(self._missing[:6])
			text = f"{text}\n{iface_('Missing: %s') % preview}"
		_set_status(context, text)
		self._redraw()

	def _redraw(self):
		if self._area is not None:
			self._area.tag_redraw()


class VIEW3D_PT_visibleio_local_quick_start(bpy.types.Panel):
	bl_label = "VisibleIO Local - Quick Start"
	bl_translation_context = _I18N
	bl_idname = "VIEW3D_PT_visibleio_local_quick_start"
	bl_space_type = "VIEW_3D"
	bl_region_type = "UI"
	bl_category = "VisibleIO"
	bl_order = 0

	def draw(self, context):
		layout = self.layout
		layout.label(text="Fast local batching, Built by the VisibleIO Team.", text_ctxt=_I18N)
		layout.label(text="1. Select target objects to include", text_ctxt=_I18N)
		layout.label(text="2. Add variation materials", text_ctxt=_I18N)
		layout.label(text="3. Select target slot for multi-material meshes", text_ctxt=_I18N)
		layout.label(text="4. Add render cameras", text_ctxt=_I18N)
		layout.label(text="Out-of-frustum objects are culled automatically", text_ctxt=_I18N, icon="DOT")


class VIEW3D_PT_visibleio_local_workspace(bpy.types.Panel):
	bl_label = "Workspace"
	bl_translation_context = _I18N
	bl_idname = "VIEW3D_PT_visibleio_local_workspace"
	bl_space_type = "VIEW_3D"
	bl_region_type = "UI"
	bl_category = "VisibleIO"
	bl_order = 1

	def draw(self, context):
		layout = self.layout
		scene = context.scene
		settings = scene.visibleio_local
		plans = _camera_plans(scene, settings)
		count = _total_count(plans)
		stale = bool(settings.tasks) and settings.task_fingerprint != _fingerprint(scene, settings)

		box = layout.box()
		box.label(text="Objects", text_ctxt=_I18N, icon="OBJECT_DATA")
		row = box.row()
		row.template_list(
			"VISIBLEIO_LOCAL_UL_objects",
			"",
			settings,
			"objects",
			settings,
			"active_object_index",
			rows=3,
		)
		col = row.column(align=True)
		col.operator("visibleio_local.object_add", text="", icon="ADD")
		col.operator("visibleio_local.object_remove", text="", icon="REMOVE")

		index = settings.active_object_index
		if 0 <= index < len(settings.objects):
			setup = settings.objects[index]
			box.prop(setup, "slot_index", text="Target Slot", text_ctxt=_I18N)
			if _ordered_material_names(setup, context):
				box.prop(setup, "material_to_add", text="")
			mat_row = box.row()
			mat_row.template_list(
				"VISIBLEIO_LOCAL_UL_materials",
				"",
				setup,
				"materials",
				setup,
				"active_material_index",
				rows=3,
			)
			mat_col = mat_row.column(align=True)
			mat_col.operator("visibleio_local.material_add", text="", icon="ADD")
			mat_col.operator("visibleio_local.material_remove", text="", icon="REMOVE")
			box.label(text="Click material to preview in viewport", text_ctxt=_I18N)
			mat_index = setup.active_material_index
			if 0 <= mat_index < len(setup.materials) and len(setup.materials[mat_index].slots):
				choice = setup.materials[mat_index]
				box.label(text=iface_("Secondary Slots"), translate=False)
				for override in choice.slots:
					box.prop(override, "material_name", text=str(override.slot_index))

		cam_box = layout.box()
		cam_box.label(text="Cameras", text_ctxt=_I18N, icon="CAMERA_DATA")
		row = cam_box.row()
		row.template_list(
			"VISIBLEIO_LOCAL_UL_cameras",
			"",
			settings,
			"cameras",
			settings,
			"active_camera_index",
			rows=3,
		)
		col = row.column(align=True)
		col.operator("visibleio_local.camera_add", text="", icon="ADD")
		col.operator("visibleio_local.camera_remove", text="", icon="REMOVE")
		if _ordered_camera_names(context):
			cam_box.prop(settings, "camera_to_add", text="")
		elif not any(obj.type == "CAMERA" for obj in scene.objects):
			cam_box.label(text="Add a camera", text_ctxt=_I18N, icon="ERROR")

		count_box = layout.box()
		count_box.label(text="Queue Generation", text_ctxt=_I18N, icon="MESH_GRID")
		if not plans:
			count_box.label(text=iface_("Total: %d variants") % 0, translate=False)
		else:
			for plan in plans[:6]:
				count_box.label(text=_plan_label(plan), translate=False)
			if len(plans) > 6:
				count_box.label(text=iface_("%d more cameras") % (len(plans) - 6), translate=False)
			count_box.label(text=iface_("Total: %d variants") % count, translate=False)
		if count > _MAX_COMBINATIONS:
			count_box.label(text=iface_("Limit is %d") % _MAX_COMBINATIONS, icon="ERROR", translate=False)
		count_box.operator("visibleio_local.generate", text_ctxt=_I18N, icon="FILE_REFRESH")
		if stale:
			count_box.label(text="Frustum changed — Recompile queue", text_ctxt=_I18N, icon="ERROR")

		if settings.tasks:
			task_box = layout.box()
			shown = _visible_task_count(settings)
			if shown == len(settings.tasks):
				task_box.label(text=iface_("Queue (%d items)") % len(settings.tasks), icon="RENDERLAYERS", translate=False)
			else:
				task_box.label(
					text=iface_("Queue (%d / %d items)") % (shown, len(settings.tasks)),
					icon="RENDERLAYERS",
					translate=False,
				)
			task_box.prop(settings, "task_camera_filter", text_ctxt=_I18N)
			task_box.prop(settings, "task_object_filter", text_ctxt=_I18N)
			task_box.prop(settings, "task_material_filter", text_ctxt=_I18N)
			task_box.template_list(
				"VISIBLEIO_LOCAL_UL_tasks",
				"",
				settings,
				"tasks",
				settings,
				"active_task_index",
				rows=4,
			)
			task_box.label(text="Click a task to preview in viewport", text_ctxt=_I18N)

		box = layout.box()
		box.label(text="Output & Batch Render", text_ctxt=_I18N, icon="FILE_FOLDER")
		box.prop(settings, "output_dir", text="")
		box.label(text=_display_output_dir(settings), translate=False)
		box.operator("visibleio_local.render_all", text_ctxt=_I18N, icon="PLAY")
		if _RENDERING:
			box.label(text="Esc in the render window cancels", text_ctxt=_I18N, icon="INFO")
		status = context.window_manager.visibleio_local_status
		if status:
			for line in status.splitlines()[:4]:
				box.label(text=line[:120], translate=False)


_CLASSES = (
	VisibleIOLocalSlotOverride,
	VisibleIOLocalMaterialChoice,
	VisibleIOLocalAssignment,
	VisibleIOLocalObject,
	VisibleIOLocalCamera,
	VisibleIOLocalTask,
	VisibleIOLocalSettings,
	VISIBLEIO_LOCAL_UL_objects,
	VISIBLEIO_LOCAL_UL_materials,
	VISIBLEIO_LOCAL_UL_cameras,
	VISIBLEIO_LOCAL_UL_tasks,
	VISIBLEIO_LOCAL_OT_object_add,
	VISIBLEIO_LOCAL_OT_object_remove,
	VISIBLEIO_LOCAL_OT_material_add,
	VISIBLEIO_LOCAL_OT_material_remove,
	VISIBLEIO_LOCAL_OT_camera_add,
	VISIBLEIO_LOCAL_OT_camera_remove,
	VISIBLEIO_LOCAL_OT_generate,
	VISIBLEIO_LOCAL_OT_render_all,
	VIEW3D_PT_visibleio_local_quick_start,
	VIEW3D_PT_visibleio_local_workspace,
)


def register():
	try:
		del bpy.types.Scene.visibleio_local
	except Exception:
		pass
	try:
		del bpy.types.WindowManager.visibleio_local_status
	except Exception:
		pass
	for cls in reversed(_CLASSES):
		try:
			bpy.utils.unregister_class(cls)
		except RuntimeError:
			pass
	for cls in _CLASSES:
		bpy.utils.register_class(cls)
	bpy.types.Scene.visibleio_local = bpy.props.PointerProperty(type=VisibleIOLocalSettings)
	bpy.types.WindowManager.visibleio_local_status = bpy.props.StringProperty(name="Status", default="")
	from .translations import translations_dict
	try:
		bpy.app.translations.unregister(__name__)
	except Exception:
		pass
	bpy.app.translations.register(__name__, translations_dict)
	if _on_load_post not in bpy.app.handlers.load_post:
		bpy.app.handlers.load_post.append(_on_load_post)
	try:
		bpy.app.timers.register(_settle_saved_preview, first_interval=0.1)
	except Exception:
		pass


def unregister():
	try:
		if bpy.app.timers.is_registered(_settle_saved_preview):
			bpy.app.timers.unregister(_settle_saved_preview)
	except Exception:
		pass
	_settle_saved_preview()
	_render_handlers_off()
	if _on_load_post in bpy.app.handlers.load_post:
		bpy.app.handlers.load_post.remove(_on_load_post)
	bpy.app.translations.unregister(__name__)
	del bpy.types.WindowManager.visibleio_local_status
	del bpy.types.Scene.visibleio_local
	for cls in reversed(_CLASSES):
		bpy.utils.unregister_class(cls)


@bpy.app.handlers.persistent
def _on_load_post(_dummy):
	_settle_saved_preview()
