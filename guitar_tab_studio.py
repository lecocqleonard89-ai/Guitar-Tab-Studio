bl_info = {
    'name': 'Guitar Tab Studio',
    'author': 'OpenAI',
    'version': (0, 9, 0),
    'blender': (4, 3, 0),
    'location': 'View3D > Sidebar (N) > Guitar Tab',
    'description': 'Animate existing guitar fret markers from MusicXML tablature',
    'category': 'Animation',
}

import bpy
import os
import re
import math
import bisect
import xml.etree.ElementTree as ET
from fractions import Fraction
from collections import defaultdict
from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty, EnumProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ImportHelper

PREFIX = 'MIDI_'
GAUGES = {1:'10', 2:'13', 3:'17', 4:'26', 5:'36', 6:'46'}
SUFFIX_RE = re.compile(r'_CORDE_(\d+)_CASE_(\d+)$')
HANDLER_NAME = 'gts_update_viewport'


def tag(el):
    return el.tag.rsplit('}', 1)[-1]


def child(el, name):
    if el is None:
        return None
    return next((x for x in el if tag(x) == name), None)


def children(el, name):
    return [] if el is None else [x for x in el if tag(x) == name]


def path(el, *names):
    for name in names:
        el = child(el, name)
        if el is None:
            return None
    return el


def value(el, *names, default=None):
    el = path(el, *names)
    return (el.text or '').strip() if el is not None else default


def fraction_duration(el, divisions):
    raw = value(el, 'duration', default='0')
    return Fraction(int(raw), divisions)


def get_markers():
    markers = {}
    duplicates = []
    for obj in bpy.data.objects:
        if not obj.name.startswith(PREFIX):
            continue
        match = SUFFIX_RE.search(obj.name)
        if match:
            key = (match.group(1), int(match.group(2)))
            if key in markers:
                duplicates.append(key)
            markers[key] = obj
    if duplicates:
        raise ValueError('Duplicate marker positions: ' + str(duplicates[:5]))
    return markers


def tempo_from_direction(direction):
    sound = child(direction, 'sound')
    if sound is not None and sound.get('tempo'):
        try:
            v = float(sound.get('tempo'))
            if v > 0:
                return v
        except ValueError:
            pass
    met = path(direction, 'direction-type', 'metronome')
    if met is not None:
        per = value(met, 'per-minute')
        unit = value(met, 'beat-unit', default='quarter')
        dots = len(children(met, 'beat-unit-dot'))
        units = {'whole':4, 'half':2, 'quarter':1, 'eighth':0.5, '16th':0.25, '32nd':0.125}
        if per and unit in units:
            try:
                bpm = float(per) * units[unit] * (sum(0.5**i for i in range(dots+1)))
                if bpm > 0:
                    return bpm
            except ValueError:
                pass
    return None


def read_musicxml(filepath, selected_part, fallback_bpm):
    root = ET.parse(filepath).getroot()
    if tag(root) != 'score-partwise':
        raise ValueError('Only score-partwise MusicXML is supported. Export score-partwise from TuxGuitar.')
    parts = children(root, 'part')
    if not parts:
        raise ValueError('No MusicXML parts found.')
    if selected_part >= len(parts):
        raise ValueError(f'Part {selected_part + 1} does not exist (file has {len(parts)}).')
    part = parts[selected_part]
    warnings = []
    notes = []
    tempos = [(Fraction(0), float(fallback_bpm))]
    divisions = 1
    measure_start = Fraction(0)
    beats = 4
    beat_type = 4
    active_ties = {}
    last_start = {}
    tab_staffs = set()
    for measure in children(part, 'measure'):
        attr = child(measure, 'attributes')
        if attr is not None:
            for clef in children(attr, 'clef'):
                if value(clef, 'sign') == 'TAB':
                    tab_staffs.add(clef.get('number', '1'))
        for barline in children(measure, 'barline'):
            if child(barline, 'repeat') is not None:
                warnings.append('Repeat barlines detected: repeats are NOT unfolded. Export an expanded score.')
        cursor = Fraction(0)
        max_end = Fraction(0)
        for item in measure:
            kind = tag(item)
            if kind == 'attributes':
                d = value(item, 'divisions')
                if d:
                    divisions = int(d)
                    if divisions <= 0:
                        raise ValueError('Invalid divisions in MusicXML')
                t = child(item, 'time')
                if t is not None:
                    try:
                        beats = int(value(t, 'beats', default='4'))
                        beat_type = int(value(t, 'beat-type', default='4'))
                    except ValueError:
                        warnings.append('Unusual time signature; measure length may be inaccurate')
                for clef in children(item, 'clef'):
                    if value(clef, 'sign') == 'TAB':
                        tab_staffs.add(clef.get('number', '1'))
            elif kind == 'backup':
                cursor -= fraction_duration(item, divisions)
                if cursor < 0:
                    warnings.append('Backup moved before measure start; clamped')
                    cursor = Fraction(0)
            elif kind == 'forward':
                cursor += fraction_duration(item, divisions)
                max_end = max(max_end, cursor)
            elif kind == 'direction':
                tempo = tempo_from_direction(item)
                if tempo:
                    offset = value(item, 'offset', default='0')
                    try:
                        at = measure_start + cursor + Fraction(int(offset), divisions)
                    except ValueError:
                        at = measure_start + cursor
                    tempos.append((max(Fraction(0), at), tempo))
            elif kind == 'note':
                voice = value(item, 'voice', default='1')
                staff = value(item, 'staff', default='1')
                duration = fraction_duration(item, divisions)
                is_chord = child(item, 'chord') is not None
                if is_chord:
                    start = last_start.get((voice, staff), cursor)
                else:
                    start = cursor
                    last_start[(voice, staff)] = start
                    cursor += duration
                max_end = max(max_end, start + duration, cursor)
                if child(item, 'rest') is not None or duration <= 0:
                    continue
                technical = path(item, 'notations', 'technical')
                string = value(technical, 'string')
                fret = value(technical, 'fret')
                if string is None or fret is None:
                    continue
                if tab_staffs and staff not in tab_staffs:
                    continue
                try:
                    string_num, fret_num = int(string), int(fret)
                except ValueError:
                    continue
                if string_num not in GAUGES or fret_num < 0:
                    warnings.append(f'Unsupported string/fret {string}/{fret}')
                    continue
                global_start = measure_start + start
                global_end = global_start + duration
                # Tie chains combine consecutive notes of same string/fret into one sustained note.
                tied = children(path(item, 'notations'), 'tied')
                types = {t.get('type') for t in tied}
                key = (string_num, fret_num)
                if 'stop' in types and key in active_ties:
                    idx = active_ties[key]
                    notes[idx][1] = max(notes[idx][1], global_end)
                    if 'start' not in types:
                        del active_ties[key]
                else:
                    notes.append([global_start, global_end, string_num, fret_num])
                    if 'start' in types:
                        active_ties[key] = len(notes)-1
                if path(item, 'notations', 'technical', 'bend') is not None:
                    warnings.append('Bend detected: marker stays on original fret; string deformation is not yet implemented')
                notations = child(item, 'notations')
                if notations is not None:
                    for mark in notations:
                        if tag(mark) in {'slide', 'glissando', 'slur'}:
                            warnings.append('Slide/slur detected: continuous movement is not yet implemented')
        # Cursor may return to zero after backup; measure duration is maximum reached.
        measure_length = max_end
        if measure_length == 0:
            measure_length = Fraction(beats * 4, beat_type)
        measure_start += measure_length
    # Tempo at a given beat is last event at or before that beat.
    tempo_at = {}
    for pos, bpm in tempos:
        tempo_at[pos] = bpm
    tempo_sorted = sorted(tempo_at.items())
    def to_seconds(pos):
        total = 0.0
        previous = Fraction(0)
        bpm = float(fallback_bpm)
        for t, new_bpm in tempo_sorted:
            if t > pos:
                break
            if t > previous:
                total += float(t-previous)*60.0/bpm
            previous = t
            bpm = new_bpm
        total += float(pos-previous)*60.0/bpm
        return total
    events = [(to_seconds(s), to_seconds(e), string, fret) for s,e,string,fret in notes]
    total_time = to_seconds(measure_start)
    return events, total_time, sorted(set(warnings)), len(parts)


def clear_owned_animations(markers):
    for obj in markers.values():
        # This addon owns hide_render keyframes for these markers only.
        # Never modify object scale, location, materials, or mesh.
        obj.animation_data_clear()
        obj.hide_render = True
        obj.hide_set(True)


def animate(filepath, settings):
    markers = get_markers()
    if not markers:
        raise ValueError('No MIDI_*_CORDE_*_CASE_* markers found in the scene')
    events, duration, warnings, num_parts = read_musicxml(filepath, settings.part_index, settings.default_bpm)
    if not events:
        raise ValueError('No string/fret notes found in selected MusicXML part')
    fps = settings.fps
    grouped = defaultdict(list)
    skipped = 0
    for start, end, string, fret in events:
        key = (GAUGES[string], fret)
        if key not in markers:
            skipped += 1
            continue
        first = max(1, 1 + round(start * fps))
        last_exclusive = max(first+1, 1 + round(end * fps))
        grouped[key].append((first, last_exclusive))
    if not grouped:
        raise ValueError('No notes match existing markers (frets 1–12 only; no open-string markers).')
    # Union overlapping/touching intervals for each marker, including repeated notes.
    merged = {}
    for key, spans in grouped.items():
        spans.sort()
        output = []
        for start, end in spans:
            if output and start <= output[-1][1]:
                output[-1] = (output[-1][0], max(output[-1][1], end))
            else:
                output.append((start, end))
        merged[key] = output
    clear_owned_animations(markers)
    for key, obj in markers.items():
        obj.hide_render = True
        obj.keyframe_insert(data_path='hide_render', frame=1)
        for start, end in merged.get(key, []):
            # Boolean animation is discrete. Adjacent notes are merged.
            if start > 1:
                obj.hide_render = True
                obj.keyframe_insert(data_path='hide_render', frame=start-1)
            obj.hide_render = False
            obj.keyframe_insert(data_path='hide_render', frame=start)
            obj.hide_render = True
            obj.keyframe_insert(data_path='hide_render', frame=end)
    scene = bpy.context.scene
    scene.render.fps = fps
    scene.frame_start = 1
    scene.frame_end = max(2, 1 + math.ceil(duration*fps))
    scene.frame_set(1)
    gts_update_viewport(scene)
    return len(events), skipped, warnings, num_parts, len(markers)


@persistent
def gts_update_viewport(scene, depsgraph=None):
    if not getattr(scene, 'gts_settings', None):
        return
    if not scene.gts_settings.preview_sync:
        return
    # Use current frame-evaluated render visibility to update viewport.
    for obj in get_markers().values():
        try:
            obj.hide_set(bool(obj.hide_render))
        except RuntimeError:
            pass


def ensure_handler():
    handlers = bpy.app.handlers.frame_change_post
    for handler in list(handlers):
        if getattr(handler, '__name__', '') == HANDLER_NAME:
            handlers.remove(handler)
    handlers.append(gts_update_viewport)


class GTS_Settings(PropertyGroup):
    filepath: StringProperty(name='MusicXML', subtype='FILE_PATH')
    part_index: IntProperty(name='Part (0-based)', default=0, min=0, description='0 is the first instrument part')
    fps: IntProperty(name='FPS', default=30, min=1, max=240)
    default_bpm: FloatProperty(name='Fallback BPM', default=120.0, min=1, max=500)
    preview_sync: BoolProperty(name='Animate viewport visibility', default=True)
    status: StringProperty(name='Status', default='Ready')


class GTS_OT_choose(Operator, ImportHelper):
    bl_idname = 'gts.choose'
    bl_label = 'Choose MusicXML'
    filename_ext = '.musicxml'
    filter_glob: StringProperty(default='*.musicxml;*.xml', options={'HIDDEN'})
    def execute(self, context):
        context.scene.gts_settings.filepath = self.filepath
        return {'FINISHED'}


class GTS_OT_generate(Operator):
    bl_idname = 'gts.generate'
    bl_label = 'Generate Animation'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        settings = context.scene.gts_settings
        filepath = bpy.path.abspath(settings.filepath)
        if not filepath or not os.path.isfile(filepath):
            self.report({'ERROR'}, 'Select a valid MusicXML file first')
            return {'CANCELLED'}
        try:
            count, skipped, warnings, parts, markers = animate(filepath, settings)
            ensure_handler()
        except Exception as exc:
            settings.status = 'ERROR: ' + str(exc)[:200]
            self.report({'ERROR'}, settings.status)
            return {'CANCELLED'}
        settings.status = f'{count} notes | {skipped} without marker | {parts} parts'
        if skipped:
            self.report({'WARNING'}, f'{skipped} notes skipped (open strings, fret >12, etc.). See console.')
        for warning in warnings:
            print('Guitar Tab Studio WARNING:', warning)
        if warnings:
            self.report({'WARNING'}, f'Imported; {len(warnings)} technique/notation warnings in System Console')
        else:
            self.report({'INFO'}, f'Imported {count} notes. {skipped} skipped.')
        print('Guitar Tab Studio:', settings.status)
        print('NOTE: repeat endings, bends, slides, and open strings require further support.')
        return {'FINISHED'}


class GTS_OT_refresh(Operator):
    bl_idname = 'gts.refresh'
    bl_label = 'Refresh Viewport'
    def execute(self, context):
        ensure_handler()
        gts_update_viewport(context.scene)
        return {'FINISHED'}


class GTS_PT_main(Panel):
    bl_label = 'Guitar Tab Studio'
    bl_idname = 'GTS_PT_main'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Guitar Tab'
    def draw(self, context):
        layout = self.layout
        s = context.scene.gts_settings
        layout.label(text='TuxGuitar / MusicXML', icon='FILE_SOUND')
        row = layout.row(align=True)
        row.prop(s, 'filepath', text='')
        row.operator('gts.choose', text='', icon='FILE_FOLDER')
        layout.prop(s, 'part_index')
        row = layout.row(align=True)
        row.prop(s, 'fps')
        row.prop(s, 'default_bpm')
        layout.prop(s, 'preview_sync')
        layout.operator('gts.generate', icon='PLAY')
        layout.operator('gts.refresh', icon='FILE_REFRESH')
        layout.separator()
        layout.label(text=s.status[:70])
        layout.label(text=f'Markers found: {len(get_markers())}')
        layout.label(text='Fret 1-12, 6 strings, no open strings', icon='INFO')


CLASSES = (GTS_Settings, GTS_OT_choose, GTS_OT_generate, GTS_OT_refresh, GTS_PT_main)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.gts_settings = PointerProperty(type=GTS_Settings)
    ensure_handler()


def unregister():
    for handler in list(bpy.app.handlers.frame_change_post):
        if getattr(handler, '__name__', '') == HANDLER_NAME:
            bpy.app.handlers.frame_change_post.remove(handler)
    del bpy.types.Scene.gts_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
