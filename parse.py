"""Python SOP of gcode::import: reads a G-code file into polylines.

Only the Python standard library is used, so the asset needs nothing besides itself.
Everything stays in G-code space here (millimetres, Z up, bed corner at the origin);
the VEX nodes after this one place, colour, trim and sweep the result.

One polyline = one uninterrupted run of moves of the same kind (extrusion, travel or
wipe) with the same feature, layer, object and tool. Per-move values (width, speed...)
sit on the points: a point carries the values of the move that ends at it, the first
point of a run those of the first move.
"""
import math
import os
import re
from bisect import bisect_left

import hou

S3D_LAYER = re.compile(r"layer -?\d+, Z = ")
WORDS = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
CONFIG_KEYS = {"filament_colour", "filament_diameter", "filament_flow_ratio",
               "print_flow_ratio", "extrusion_multiplier", "nozzle_diameter",
               "printable_area", "bed_shape"}
# Slic3r's bead: a rectangle with round ends, area = h * (w - h * (1 - pi / 4)).
ROUND_ENDS = 1.0 - math.pi / 4.0
TWO_PI = 2.0 * math.pi
EXTRUDE, TRAVEL, WIPE = 0, 1, 2
CUSTOM_FEATURE, BEFORE_LAYERS = 1, 2


def words_of(line):
    """{letter: number} of a command line."""
    params = {}
    for word in line.split()[1:]:
        try:
            params[word[0].upper()] = float(word[1:])
        except (ValueError, IndexError):
            pass
    return params


def numbers(text, default):
    """All numbers of a config value like '0.94' or '1.75,1.75' or '0.4;0.4'."""
    out = []
    for item in re.split(r"[,;\s]+", text.replace('"', "").strip()):
        try:
            out.append(float(item))
        except ValueError:
            pass
    return out or [default]


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def colours(text):
    out = []
    for item in re.split(r"[;,]", text.replace('"', "")):
        item = item.strip().lstrip("#")
        try:
            out.append(tuple(srgb_to_linear(int(item[i:i + 2], 16) / 255.0) for i in (0, 2, 4)))
        except ValueError:
            pass
    return out


def bed_centre(text):
    xs, ys = [], []
    for corner in text.replace('"', "").split(","):
        try:
            x, y = corner.lower().split("x")
            xs.append(float(x))
            ys.append(float(y))
        except ValueError:
            pass
    return (0.5 * (min(xs) + max(xs)), 0.5 * (min(ys) + max(ys)), 0.0) if xs else None


def read(path, arc_tol):
    """Parse the file into flat per-point and per-polyline lists."""
    pos = []                        # x, y, z per point
    p_epm, p_height, p_speed, p_clock = [], [], [], []
    prim_start, prim_kind, prim_feature, prim_layer = [], [], [], []
    prim_object, prim_tool, prim_custom = [], [], []
    feature_ids, object_ids = {}, {"": 0}
    config = {}
    slicer = ""
    m73_marks, cura_marks = [], []

    def feature_id(name):
        return feature_ids.setdefault(name, len(feature_ids))

    travel_id, wipe_id = feature_id("Travel"), feature_id("Wipe")
    feat_id, feat_custom = feature_id(""), 0

    # Positions are machine coordinates; G92 offsets are applied when a move is read.
    x = y = z = 0.0
    ox = oy = oz = 0.0
    e_last = 0.0
    rel = rel_e = False
    unit = 1.0
    feed = 1800.0
    clock = 0.0                     # seconds, from feed rates only
    tool = obj = 0
    height_c = 0.0                  # bead height from the slicer's comments
    in_wipe = False
    layer = -1
    markers = False                 # a layer change comment was seen
    extruded = False                # extrusion since the last layer change comment
    levels = []                     # extrusion Z levels, to derive heights without comments
    last_r = None
    run_key = None
    run_epm, run_unsure = 0.0, False
    npt = 0
    line_no = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line_no += 1
            line = raw.strip()
            if not line:
                continue

            if line[0] == ";":
                c = line[1:].strip()
                if c.startswith("FEATURE:") or c.startswith("TYPE:"):
                    feature = c.split(":", 1)[1].strip()
                    feat_id = feature_id(feature)
                    feat_custom = CUSTOM_FEATURE if feature.lower() == "custom" else 0
                elif c.startswith("LAYER_HEIGHT:") or c.startswith("HEIGHT:"):
                    height_c = numbers(c.split(":", 1)[1], height_c)[0]
                elif (c.startswith("CHANGE_LAYER") or c.startswith("LAYER_CHANGE")
                      or c.startswith("LAYER:") or S3D_LAYER.match(c)):
                    # Orca writes two markers per layer: count a layer only after extrusion.
                    if extruded or not markers:
                        layer += 1
                    markers, extruded = True, False
                elif c.startswith("WIPE_START"):
                    in_wipe = True
                elif c.startswith("WIPE_END"):
                    in_wipe = False
                elif c.startswith("feature "):          # Simplify3D
                    feat_id, feat_custom = feature_id(c[8:].strip()), 0
                elif c.startswith("tool H"):            # Simplify3D: tool H0.2 W0.48
                    height_c = numbers(c[6:].split()[0], height_c)[0]
                elif c.startswith("TIME_ELAPSED:"):     # Cura
                    cura_marks.append((clock, numbers(c[13:], 0.0)[0]))
                elif "printing object" in c:            # Bambu, Prusa
                    if c.startswith("stop"):
                        obj = 0
                    else:
                        name = (c.split("id:", 1)[1] if "unique label id:" in c
                                else c.split("printing object", 1)[1]).strip(" ,:")
                        obj = object_ids.setdefault(name, len(object_ids))
                elif c.startswith("MESH:"):             # Cura
                    name = c[5:].strip()
                    obj = 0 if name == "NONMESH" else object_ids.setdefault(name, len(object_ids))
                elif " = " in c:
                    key, value = c.split(" = ", 1)
                    if key in CONFIG_KEYS:
                        config[key] = value.strip()
                elif not slicer and line_no < 30 and re.search(
                        r"slicer|studio|cura|generated", c, re.I):
                    slicer = c
                continue

            k = line.find(";")
            if k >= 0:
                line = line[:k].rstrip()
                if not line:
                    continue
            cmd = line.split(None, 1)[0].upper()
            if cmd[0] in "GM" and len(cmd) > 2 and not cmd[1:].replace(".", "").isdigit():
                # Written without spaces: G1X10Y20E.5
                found = WORDS.findall(line)
                if not found:
                    continue
                line = " ".join(a + b for a, b in found)
                cmd = line.split(None, 1)[0].upper()
            if len(cmd) > 2 and cmd[1] == "0" and cmd[2:].isdigit():
                cmd = cmd[0] + cmd[2:]                  # G01 -> G1

            if cmd in ("G1", "G0", "G2", "G3"):
                p = words_of(line)
                if p.get("F", 0.0) > 0.0:
                    feed = p["F"] * unit
                x1 = p["X"] * unit + (x if rel else ox) if "X" in p else x
                y1 = p["Y"] * unit + (y if rel else oy) if "Y" in p else y
                z1 = p["Z"] * unit + (z if rel else oz) if "Z" in p else z
                de = 0.0
                if "E" in p:
                    if rel or rel_e:
                        de = p["E"] * unit
                    else:
                        de = p["E"] * unit - e_last
                        e_last = p["E"] * unit
                v = feed / 60.0

                arc = None
                if cmd == "G2" or cmd == "G3":
                    cw = cmd == "G2"
                    chord = math.hypot(x1 - x, y1 - y)
                    cx = None
                    if "I" in p or "J" in p:
                        cx = x + p.get("I", 0.0) * unit
                        cy = y + p.get("J", 0.0) * unit
                    elif "R" in p and chord > 1e-9:
                        r = p["R"] * unit
                        h = math.sqrt(max(r * r - chord * chord / 4.0, 0.0))
                        # Short arcs turn around a centre on their inner side; R < 0 = long arc.
                        side = (1.0 if cw else -1.0) * (1.0 if r > 0 else -1.0)
                        cx = 0.5 * (x + x1) + side * h * (y1 - y) / chord
                        cy = 0.5 * (y + y1) - side * h * (x1 - x) / chord
                    if cx is not None:
                        r0 = math.hypot(x - cx, y - cy)
                        r1 = math.hypot(x1 - cx, y1 - cy)
                        rm = 0.5 * (r0 + r1)
                        turns = max(int(p.get("P", 0)), 0)
                        a0 = math.atan2(y - cy, x - cx)
                        if chord < 1e-6:                # back to the start: full circle(s)
                            sweep = TWO_PI * max(turns, 1)
                        else:
                            a1 = math.atan2(y1 - cy, x1 - cx)
                            sweep = (a0 - a1) if cw else (a1 - a0)
                            if sweep <= 0.0:
                                sweep += TWO_PI
                            sweep += TWO_PI * turns
                        if rm > 1e-9:
                            c = 1.0 - arc_tol / rm
                            step = min(2.0 * math.acos(c) if c > 0.0 else math.pi, math.pi / 2)
                            n = max(1, int(math.ceil(sweep / step - 1e-9)))
                            arc = (cx, cy, r0, r1, a0, -sweep if cw else sweep, n)
                            length = math.hypot(sweep * rm, z1 - z)
                if arc is None:
                    length = math.sqrt((x1 - x) ** 2 + (y1 - y) ** 2 + (z1 - z) ** 2)

                if length < 1e-7:
                    if de:
                        clock += abs(de) / v
                    continue
                dt = length / v

                custom = feat_custom | (0 if markers else BEFORE_LAYERS)
                if de > 0.0:
                    kind, fid = EXTRUDE, feat_id
                    if height_c > 0.0:
                        h = height_c
                    else:                       # no height comments: distance to the level below
                        zr = round(z1, 4)
                        i = bisect_left(levels, zr)
                        if not custom and (i == len(levels) or levels[i] != zr):
                            levels.insert(i, zr)    # a purge line is no layer
                        h = max(zr - levels[i - 1] if i > 0 else zr, 0.01)
                    epm = de / length           # filament per path length
                    if not custom:
                        extruded = True
                else:
                    kind, fid = (WIPE, wipe_id) if in_wipe else (TRAVEL, travel_id)
                    epm = h = 0.0

                key = (kind, fid, layer, obj, tool, custom)
                # Coordinates are rounded to microns, so on a move of a few microns the
                # flow is noise: such a move keeps the flow of its run.
                tiny = kind == EXTRUDE and length < 0.02
                if key == run_key and kind == EXTRUDE:
                    if tiny:
                        epm = run_epm
                    elif run_unsure:
                        for j in range(prim_start[-1], npt):
                            p_epm[j] = epm
                    run_unsure = run_unsure and tiny
                run_epm = epm
                if key != run_key:
                    run_key = key
                    run_unsure = tiny
                    prim_start.append(npt)
                    prim_kind.append(kind)
                    prim_feature.append(fid)
                    prim_layer.append(layer)
                    prim_object.append(obj)
                    prim_tool.append(tool)
                    prim_custom.append(custom)
                    pos += (x, y, z)
                    p_epm.append(epm)
                    p_height.append(h)
                    p_speed.append(v)
                    p_clock.append(clock)
                    npt += 1
                if arc is not None:
                    cx, cy, r0, r1, a0, s, n = arc
                    for i in range(1, n):
                        t = i / n
                        a = a0 + s * t
                        r = r0 + (r1 - r0) * t
                        pos += (cx + r * math.cos(a), cy + r * math.sin(a), z + (z1 - z) * t)
                        p_epm.append(epm)
                        p_height.append(h)
                        p_speed.append(v)
                        p_clock.append(clock + dt * t)
                    npt += n - 1
                pos += (x1, y1, z1)
                p_epm.append(epm)
                p_height.append(h)
                p_speed.append(v)
                clock += dt
                p_clock.append(clock)
                npt += 1
                x, y, z = x1, y1, z1

            elif cmd == "G90":
                rel = False
            elif cmd == "G91":
                rel = True
            elif cmd == "M82":
                rel_e = False
            elif cmd == "M83":
                rel_e = True
            elif cmd == "G92":
                p = words_of(line) or {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0}
                if "X" in p:
                    ox = x - p["X"] * unit
                if "Y" in p:
                    oy = y - p["Y"] * unit
                if "Z" in p:
                    oz = z - p["Z"] * unit
                if "E" in p:
                    e_last = p["E"] * unit
            elif cmd == "G28":
                axes = {w[0].upper() for w in line.split()[1:]} & set("XYZ") or set("XYZ")
                if "X" in axes:
                    x = ox = 0.0
                if "Y" in axes:
                    y = oy = 0.0
                if "Z" in axes:
                    z = oz = 0.0
                run_key = None
            elif cmd == "G4":
                p = words_of(line)
                clock += p.get("P", 0.0) / 1000.0 + p.get("S", 0.0)
            elif cmd == "G20":
                unit = 25.4
            elif cmd == "G21":
                unit = 1.0
            elif cmd == "M73":
                p = words_of(line)
                if "R" in p and (last_r is None or p["R"] < last_r):
                    last_r = p["R"]
                    m73_marks.append((clock, last_r))
            elif cmd[0] == "T" and cmd[1:].isdigit():
                if int(cmd[1:]) < 64:           # T255, T1000: firmware specials, not tools
                    tool = int(cmd[1:])
            elif cmd == "M486":
                p = words_of(line)
                if "S" in p:
                    obj = 0 if p["S"] < 0 else object_ids.setdefault(
                        "%d" % p["S"], len(object_ids))
            elif cmd == "EXCLUDE_OBJECT_START":
                name = line.split("NAME=", 1)[1].split()[0] if "NAME=" in line else "?"
                obj = object_ids.setdefault(name, len(object_ids))
            elif cmd == "EXCLUDE_OBJECT_END":
                obj = 0

    return dict(pos=pos, epm=p_epm, height=p_height, speed=p_speed, clock=p_clock,
                prim_start=prim_start, prim_kind=prim_kind, prim_feature=prim_feature,
                prim_layer=prim_layer, prim_object=prim_object, prim_tool=prim_tool,
                prim_custom=prim_custom, features=feature_ids, objects=object_ids,
                config=config, slicer=slicer, markers=markers, m73=m73_marks,
                cura=cura_marks)


def slicer_timeline(d):
    """(clock, slicer seconds) pairs from M73 R or Cura's TIME_ELAPSED, if any."""
    if len(d["m73"]) >= 2:
        r0 = d["m73"][0][1]
        return [(c, (r0 - r) * 60.0) for c, r in d["m73"]]
    if len(d["cura"]) >= 2:
        return d["cura"]
    return []


def remap(clocks, marks):
    """Piecewise-linear clock -> slicer time through the marks; slope 1 outside them."""
    if not marks:
        return list(clocks)
    out = []
    k, n = 0, len(marks)
    for c in clocks:                    # clocks never decrease
        while k + 1 < n and marks[k + 1][0] <= c:
            k += 1
        ca, sa = marks[k]
        if k + 1 < n and c >= ca:
            cb, sb = marks[k + 1]
            out.append(sa + (sb - sa) * (c - ca) / (cb - ca))
        else:
            out.append(sa + (c - ca))
    return out


def per_tool(values, tool):
    return values[min(tool, len(values) - 1)]


def build(geo, d, path):
    starts = d["prim_start"]
    npt = len(d["clock"])
    nprim = len(starts)
    ends = starts[1:] + [npt]
    config = d["config"]

    # Final custom flag: "before the first layer" only counts if the file has layers.
    custom = [int(bool(c & CUSTOM_FEATURE or (d["markers"] and c & BEFORE_LAYERS)))
              for c in d["prim_custom"]]
    layer = list(d["prim_layer"])
    if not d["markers"]:                # no layer comments: one layer per extrusion Z
        zs = sorted({round(d["pos"][3 * starts[i] + 2], 3)
                     for i in range(nprim) if d["prim_kind"][i] == EXTRUDE})
        current = 0
        for i in range(nprim):
            if d["prim_kind"][i] == EXTRUDE:
                current = bisect_left(zs, round(d["pos"][3 * starts[i] + 2], 3))
            layer[i] = current

    # Bead width from the extruded volume. Flow ratio and filament diameter may be
    # written after the moves (PrusaSlicer), hence this second pass.
    diameters = numbers(config.get("filament_diameter", "1.75"), 1.75)
    print_flow = numbers(config.get("print_flow_ratio", "1"), 1.0)[0]
    flows = [f * print_flow for f in numbers(
        config.get("filament_flow_ratio", config.get("extrusion_multiplier", "1")), 1.0)]
    nozzle = numbers(config.get("nozzle_diameter", "0.4"), 0.4)[0]
    epm, height = d["epm"], d["height"]
    width = [0.0] * npt
    for i in range(nprim):
        if d["prim_kind"][i] != EXTRUDE:
            continue
        tool = d["prim_tool"][i]
        dia = per_tool(diameters, tool)
        k_area = math.pi * dia * dia / 4.0 / max(per_tool(flows, tool), 1e-3)
        for j in range(starts[i], ends[i]):
            h = height[j]
            width[j] = min(epm[j] * k_area / h + h * ROUND_ENDS, 4.0 * nozzle)

    # Time: feed-rate clock bent onto the slicer's own estimate, zero at the first
    # imported point that is not start/end G-code.
    time = remap(d["clock"], slicer_timeline(d))
    keep = [i for i in range(nprim) if not custom[i]]
    t0 = time[starts[keep[0]]] if keep else 0.0
    time = [t - t0 for t in time]
    print_time = time[ends[keep[-1]] - 1] if keep else 0.0

    # Footprint of the print itself (extrusions, no custom G-code).
    pos = d["pos"]
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for i in keep:
        if d["prim_kind"][i] != EXTRUDE:
            continue
        for a in range(3):
            column = pos[3 * starts[i] + a:3 * ends[i]:3]
            lo[a] = min(lo[a], min(column))
            hi[a] = max(hi[a], max(column))
    if lo[0] == math.inf:
        lo = hi = [0.0, 0.0, 0.0]
    centre = (0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1]), 0.0)
    bed = bed_centre(config.get("printable_area", config.get("bed_shape", ""))) or centre

    # Geometry.
    geo.createPoints([(0.0, 0.0, 0.0)] * npt)
    geo.setPointFloatAttribValues("P", pos)
    for name, values in (("width", width), ("height", height), ("speed", d["speed"]),
                         ("time", time)):
        geo.addAttrib(hou.attribType.Point, name, 0.0)
        geo.setPointFloatAttribValues(name, values)
    geo.createPolygons([range(s, e) for s, e in zip(starts, ends)], False)

    names = {i: n for n, i in d["features"].items()}
    objects = {i: n for n, i in d["objects"].items()}
    geo.addAttrib(hou.attribType.Prim, "feature", "")
    geo.setPrimStringAttribValues("feature", [names[i] for i in d["prim_feature"]])
    geo.addAttrib(hou.attribType.Prim, "object", "")
    geo.setPrimStringAttribValues("object", [objects[i] for i in d["prim_object"]])
    geo.addAttrib(hou.attribType.Prim, "layer", 0)
    geo.setPrimIntAttribValues("layer", layer)
    geo.addAttrib(hou.attribType.Prim, "tool", 0)
    geo.setPrimIntAttribValues("tool", d["prim_tool"])
    prims = geo.prims()
    geo.createPrimGroup("travel").add([prims[i] for i in range(nprim)
                                       if d["prim_kind"][i] != EXTRUDE])
    geo.createPrimGroup("custom").add([prims[i] for i in range(nprim) if custom[i]])

    filament = colours(config.get("filament_colour", "")) or [(0.6, 0.6, 0.6)]
    geo.addArrayAttrib(hou.attribType.Global, "filament_colour", hou.attribData.Float, 3)
    geo.setGlobalAttribValue("filament_colour", [c for rgb in filament for c in rgb])
    for name, value in (("print_time", print_time), ("nozzle_diameter", nozzle)):
        geo.addAttrib(hou.attribType.Global, name, 0.0)
        geo.setGlobalAttribValue(name, value)
    for name, value in (("print_centre", centre), ("bed_centre", bed)):
        geo.addAttrib(hou.attribType.Global, name, (0.0, 0.0, 0.0))
        geo.setGlobalAttribValue(name, value)
    geo.addAttrib(hou.attribType.Global, "layer_count", 0)
    geo.setGlobalAttribValue("layer_count", max([layer[i] for i in keep] or [-1]) + 1)
    for name, value in (("slicer", d["slicer"]), ("source", path)):
        geo.addAttrib(hou.attribType.Global, name, "")
        geo.setGlobalAttribValue(name, value)


def cook():
    node = hou.pwd()
    asset = node.parent()
    geo = node.geometry()
    path = asset.evalParm("file").strip()
    if not path:
        return
    if not os.path.isfile(path):
        raise hou.NodeError("G-code file not found: %s" % path)
    data = read(path, max(asset.evalParm("arctol"), 1e-4))
    if not data["prim_start"]:
        node.addWarning("No moves found in %s" % os.path.basename(path))
        return
    build(geo, data, path)


cook()
