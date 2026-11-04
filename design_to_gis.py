#!/usr/bin/env python3
"""Convert an amaze-router design.json export into GDAL vector files.

Writes one file per feature type (trace, copper_area, via, pin, ...). Each file holds a single
table of Point, LineString, Polygon or MultiPolygon features with scalar attributes only. Arcs
are linearized to within --arc-tolerance-um of the true curve.

Source coordinates are microns and are mapped into a projected world CRS with

    world_x = offset_x + scale * x_um
    world_y = offset_y + scale * y_um

With the defaults (UTM zone 31N, 1 m per micron, origin at E 500000 / N 1000000) a 75 mm package
becomes a 75 km region centred on the zone's central meridian, and distances read in metres equal
the original distances in microns. Attribute values such as width_um stay in microns.
"""

import argparse
import collections
import json
import math
import multiprocessing
import os
import re
import sys
import time

from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

CONVERTER_VERSION = "1.0"

FORMATS = {"GPKG": ".gpkg", "FlatGeobuf": ".fgb"}
LAYER_OPTIONS = {
    "GPKG": ["SPATIAL_INDEX=YES", "FID=fid", "GEOMETRY_NAME=geom"],
    "FlatGeobuf": ["SPATIAL_INDEX=YES"],
}
FIELD_TYPES = {
    "str": (ogr.OFTString, ogr.OFSTNone),
    "int": (ogr.OFTInteger, ogr.OFSTNone),
    "float": (ogr.OFTReal, ogr.OFSTNone),
    "bool": (ogr.OFTInteger, ogr.OFSTBoolean),
}
UNIT_TO_UM = {"UM": 1.0, "MM": 1000.0, "MIL": 25.4, "IN": 25400.0}


class Context:
    def __init__(self, design, args):
        self.d = design
        self.scale = args.scale
        self.offset_x = args.offset_x
        self.offset_y = args.offset_y
        self.tol = args.arc_tolerance_um
        self.crs = args.crs
        self.prefix = args.prefix
        self.source_file = os.path.abspath(args.input)
        order = design.get("constraints", {}).get("layerOrder") or design["layers"]["conductor"]
        self.layer_index = {name: i for i, name in enumerate(order)}
        self.components = {c["id"]: c for c in design.get("components", [])}
        self.padstacks = {p["name"]: p for p in design.get("padstacks", [])}

    def xy(self, p):
        return self.offset_x + self.scale * p[0], self.offset_y + self.scale * p[1]

    def layer(self, full_name):
        """('LAYER_6', 8) for 'CONDUCTOR/LAYER_6'; the index is None for non-conductor layers."""
        if full_name is None:
            return None, None
        name = full_name.split("/", 1)[-1]
        return name, self.layer_index.get(name)

    def metadata(self):
        meta = self.d.get("meta", {})
        return {
            "CONVERTER": f"design_to_gis.py {CONVERTER_VERSION}",
            "SOURCE_FILE": self.source_file,
            "SOURCE_DESIGN": str(meta.get("design")),
            "SOURCE_EXPORTER": str(meta.get("exporter")),
            "SOURCE_UNITS": str(meta.get("units")),
            "TRANSFORM": "world = offset + scale * source_um",
            "SCALE": repr(self.scale),
            "OFFSET_X": repr(self.offset_x),
            "OFFSET_Y": repr(self.offset_y),
            "CRS": self.crs,
            "ARC_TOLERANCE_UM": repr(self.tol),
            "TABLE_PREFIX": self.prefix,
        }


CTX = None


# ---------------------------------------------------------------- geometry helpers

def dedupe(pts):
    out = [tuple(pts[0])]
    for p in pts[1:]:
        if p[0] != out[-1][0] or p[1] != out[-1][1]:
            out.append(tuple(p))
    return out


def point(ctx, p):
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(*ctx.xy(p))
    return g


def line_string(ctx, pts):
    g = ogr.Geometry(ogr.wkbLineString)
    for p in pts:
        g.AddPoint_2D(*ctx.xy(p))
    return g


def linear_ring(ctx, pts):
    g = ogr.Geometry(ogr.wkbLinearRing)
    for p in pts:
        g.AddPoint_2D(*ctx.xy(p))
    g.CloseRings()
    return g


def rectangle(ctx, bbox):
    (x0, y0), (x1, y1) = bbox
    if x1 <= x0 or y1 <= y0:
        return None
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(linear_ring(ctx, [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]))
    return poly


def circle(ctx, center, diameter):
    r = diameter / 2
    start = (center[0] + r, center[1])
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(linear_ring(ctx, [start] + arc_points(start, start, center, False, True, ctx.tol)))
    return poly


def quad_segments(radius, tol):
    """GEOS buffer segments per quarter circle that keep round caps within `tol` of the true arc."""
    if radius <= tol:
        return 2
    # GEOS rounds each join's segment count, so a segment can span up to 1.5x the nominal angle.
    return max(2, math.ceil(1.5 * (math.pi / 2) / (2 * math.acos(1 - tol / radius))))


def arc_points(start, end, center, clockwise, full_circle, tol):
    """Points along an arc after `start`, ending exactly at `end`, within `tol` of the true curve."""
    cx, cy = center
    r = math.hypot(start[0] - cx, start[1] - cy)
    if r == 0:
        return [tuple(end)]
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    if full_circle:
        sweep = -2 * math.pi if clockwise else 2 * math.pi
    else:
        sweep = (math.atan2(end[1] - cy, end[0] - cx) - a0) % (2 * math.pi)
        if sweep == 0:
            return [tuple(end)]
        if clockwise:
            sweep -= 2 * math.pi
    step = 2 * math.acos(1 - tol / r) if tol < r else math.pi / 2
    # At least 8 segments per full turn so tiny arcs still look round.
    n = max(1, math.ceil(abs(sweep) / step), math.ceil(abs(sweep) / (math.pi / 4)))
    pts = [(cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n))
           for i in range(1, n)]
    pts.append(tuple(end))
    return pts


def contour_points(segments, tol):
    """Closed ring of points for a chain of line/arc contour segments, or None if degenerate."""
    if not segments:
        return None, 0
    pts = [tuple(segments[0]["startEnd"][0])]
    n_arcs = 0
    for s in segments:
        start, end = s["startEnd"]
        if s["objType"] == "arc":
            n_arcs += 1
            pts += arc_points(start, end, s["xy"], s["isClockwise"], s["isCircle"], tol)
        else:
            pts.append(tuple(end))
    pts = dedupe(pts)
    if pts[-1] != pts[0]:
        pts.append(pts[0])
    if len(pts) < 4:
        return None, n_arcs
    return pts, n_arcs


def _collect_polygons(g, out):
    t = ogr.GT_Flatten(g.GetGeometryType())
    if t == ogr.wkbPolygon:
        out.AddGeometry(g)
    elif t in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        for i in range(g.GetGeometryCount()):
            _collect_polygons(g.GetGeometryRef(i), out)


def make_polygonal(geom):
    """(valid Polygon or MultiPolygon, was_repaired); geometry is None if nothing polygonal is left."""
    if geom.IsValid():
        return geom, False
    parts = ogr.Geometry(ogr.wkbMultiPolygon)
    _collect_polygons(geom.MakeValid(), parts)
    if parts.GetGeometryCount() == 0:
        return None, True
    if parts.GetGeometryCount() == 1:
        return parts.GetGeometryRef(0).Clone(), True
    return parts, True


# ---------------------------------------------------------------- attribute helpers

def parse_um(text):
    """Microns from strings like '11.896 UM' or '3 MIL'; None if the unit is unknown."""
    if text is None:
        return None
    m = re.fullmatch(r"\s*([-+0-9.eE]+)\s*([A-Za-z]*)\s*", str(text))
    if not m:
        return None
    factor = UNIT_TO_UM.get((m.group(2) or "UM").upper())
    return float(m.group(1)) * factor if factor is not None else None


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def layer_span(ctx, start_end):
    (s_name, s_idx), (e_name, e_idx) = (ctx.layer(x) for x in (start_end or [None, None]))
    return {"start_layer": s_name, "end_layer": e_name,
            "start_layer_index": s_idx, "end_layer_index": e_idx}


# ---------------------------------------------------------------- tables
# Each producer yields (ogr.Geometry, {field: scalar}) and counts anything it drops in `st`.

def trace_runs(ctx, st):
    """(centerline points in microns, attributes) for each constant-width run of every path."""
    for p in ctx.d["paths"]:
        layer, idx = ctx.layer(p["layer"])
        prop = p.get("prop") or {}
        runs = []
        for s in p["segments"]:
            if runs and runs[-1][-1]["width"] == s["width"]:
                runs[-1].append(s)
            else:
                runs.append([s])
        for i, run in enumerate(runs):
            pts = dedupe([run[0]["points"][0]] + [s["points"][1] for s in run])
            if len(pts) < 2:
                st["skipped_zero_length"] += 1
                continue
            n_arcs = sum(s["objType"] == "arc" for s in run)
            st["arcs_without_center"] += n_arcs
            yield pts, {
                "path_id": p["id"], "run_index": i, "net_id": p.get("net"), "net_name": p.get("netName"),
                "layer": layer, "layer_index": idx, "width_um": run[0]["width"], "n_segments": len(run),
                "has_unrecoverable_arc": n_arcs > 0, "is_symbol_etch": prop.get("SYMBOL_ETCH", False),
                "symbol_owned": p.get("symbolOwned"), "is_fixed": prop.get("FIXED", False),
                "clip_drawing": prop.get("CLIP_DRAWING"), "vs_conpath_id": prop.get("VS_CONPATH_ID"),
                "via_array_id": prop.get("VIAARRAYID"),
            }


def t_trace(ctx, st):
    for pts, attrs in trace_runs(ctx, st):
        yield line_string(ctx, pts), attrs


def t_trace_copper(ctx, st):
    """Traces widened to their copper outline; Allegro lines have round ends and corners."""
    for pts, attrs in trace_runs(ctx, st):
        r = attrs["width_um"] / 2
        geom = line_string(ctx, pts).Buffer(r * ctx.scale, quad_segments(r, ctx.tol))
        geom, repaired = make_polygonal(geom)
        if geom is None:
            st["skipped_empty"] += 1
            continue
        st["repaired"] += repaired
        yield geom, attrs


def t_copper_area(ctx, st):
    for s in ctx.d["shapes"]:
        contours = s.get("nativeCenterContours") or {}
        root = contours.get("root")
        if not root:
            st["skipped_no_contour"] += 1
            continue
        outer, n_arcs = contour_points(root["segments"], ctx.tol)
        if outer is None:
            st["skipped_degenerate"] += 1
            continue
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(linear_ring(ctx, outer))
        n_vertices, n_voids = len(outer), 0
        for v in root.get("voids", []):
            pts, a = contour_points(v["segments"], ctx.tol)
            n_arcs += a
            if pts is None:
                st["skipped_degenerate_voids"] += 1
                continue
            poly.AddGeometry(linear_ring(ctx, pts))
            n_vertices += len(pts)
            n_voids += 1
        geom, repaired = make_polygonal(poly)
        if geom is None:
            st["skipped_invalid"] += 1
            continue
        st["repaired"] += repaired
        st["arcs_linearized"] += n_arcs
        layer, idx = ctx.layer(s["layer"])
        yield geom, {
            "shape_id": s["id"], "net_id": s.get("net"), "net_name": s.get("netName"),
            "layer": layer, "layer_index": idx, "is_rect": s.get("isRect"), "is_auto": s.get("shapeAuto"),
            "n_voids": n_voids, "n_arcs": n_arcs, "n_vertices": n_vertices, "repaired": repaired,
        }


def via_attrs(ctx, v):
    prop = v.get("prop") or {}
    return {
        "via_id": v["id"], "net_id": v.get("net"), "net_name": v.get("netName"),
        "padstack": v.get("definition"), **layer_span(ctx, v.get("startEnd")),
        "is_through": v.get("isThrough"), "symbol_owned": v.get("symbolOwned"),
        "is_fixed": prop.get("FIXED", False), "clip_drawing": prop.get("CLIP_DRAWING"),
        "retain_net_on_vias": prop.get("RETAIN_NET_ON_VIAS", False),
        "vs_conpath_id": prop.get("VS_CONPATH_ID"), "via_array_id": prop.get("VIAARRAYID"),
        "thermal_connect": prop.get("DYN_THERMAL_CON_TYPE"),
    }


def bbox_size(bbox):
    return {"width_um": bbox[1][0] - bbox[0][0], "height_um": bbox[1][1] - bbox[0][1]}


def drill_attrs(ctx, padstack_name):
    """Drill diameter and plating if the padstack has a circular drill, else None."""
    ps = ctx.padstacks.get(padstack_name)
    if not ps or ps.get("holeType") != "circle_drill" or not ps.get("drillDiameter"):
        return None
    return {"drill_diameter_um": ps["drillDiameter"], "plating": ps.get("plating")}


def t_via(ctx, st):
    for v in ctx.d["vias"]:
        yield point(ctx, v["xy"]), via_attrs(ctx, v)


def t_via_extent(ctx, st):
    for v in ctx.d["vias"]:
        geom = rectangle(ctx, v["bBox"])
        if geom is None:
            st["skipped_degenerate"] += 1
            continue
        yield geom, {**via_attrs(ctx, v), **bbox_size(v["bBox"])}


def t_via_drill(ctx, st):
    for v in ctx.d["vias"]:
        drill = drill_attrs(ctx, v.get("definition"))
        if drill is None:
            st["skipped_no_drill"] += 1
            continue
        yield circle(ctx, v["xy"], drill["drill_diameter_um"]), {**via_attrs(ctx, v), **drill}


def pin_attrs(ctx, p):
    prop = p.get("prop") or {}
    return {
        "pin_id": p["id"], "pin_number": p.get("number"), "refdes": p.get("refdes"),
        "net_id": p.get("net"), "net_name": p.get("netName"), "padstack": p.get("definition"),
        "symbol_id": p.get("symbol"), **layer_span(ctx, p.get("startEnd")),
        "rotation_deg": p.get("rotation"), "is_mirrored": p.get("isMirrored"),
        "is_through": p.get("isThrough"), "is_mech": p.get("isMech"),
        "swap_code": p.get("swapCode"), "slot": p.get("slot"),
        "cds_pin_id": prop.get("CDS_PINID"), "xml_signal": prop.get("NV_XML_SIG"),
    }


def t_pin(ctx, st):
    for p in ctx.d["pins"]:
        yield point(ctx, p["xy"]), pin_attrs(ctx, p)


def t_pin_extent(ctx, st):
    for p in ctx.d["pins"]:
        geom = rectangle(ctx, p["bBox"])
        if geom is None:
            st["skipped_degenerate"] += 1
            continue
        yield geom, pin_attrs(ctx, p)


def t_pin_drill(ctx, st):
    for p in ctx.d["pins"]:
        drill = drill_attrs(ctx, p.get("definition"))
        if drill is None:
            st["skipped_no_drill"] += 1
            continue
        yield circle(ctx, p["xy"], drill["drill_diameter_um"]), {**pin_attrs(ctx, p), **drill}


def symbol_attrs(ctx, s):
    prop = s.get("prop") or {}
    comp = ctx.components.get(s.get("component")) or {}
    comp_prop = comp.get("prop") or {}
    return {
        "symbol_id": s["id"], "definition": s.get("definition"), "refdes": s.get("refdes"),
        "type": s.get("type"), "side": s.get("layer"), "rotation_deg": s.get("rotation"),
        "is_mirrored": s.get("isMirrored"), "mirror_type": s.get("mirrorType"), "n_pins": s.get("nPins"),
        "component_id": s.get("component"), "device_type": comp.get("deviceType"),
        "component_class": comp.get("class"), "package": comp.get("package"),
        "component_subtype": comp_prop.get("COMP_SUBTYPE"), "locked": prop.get("LOCKED", False),
        "is_fixed": prop.get("FIXED", False), "symbol_edited": prop.get("SYMBOL_EDITED", False),
        "clip_drawing": prop.get("CLIP_DRAWING"), "via_structure": prop.get("VIA_STRUCTURE"),
    }


def t_symbol(ctx, st):
    for s in ctx.d["symbols"]:
        yield point(ctx, s["xy"]), symbol_attrs(ctx, s)


def t_symbol_extent(ctx, st):
    for s in ctx.d["symbols"]:
        geom = rectangle(ctx, s["bBox"])
        if geom is None:
            st["skipped_degenerate"] += 1
            continue
        yield geom, symbol_attrs(ctx, s)


def t_ratsnest(ctx, st):
    for n in ctx.d["nets"]:
        for r in n.get("rats", []):
            pts = dedupe(r["pinXY"])
            if len(pts) < 2:
                st["skipped_zero_length"] += 1
                continue
            yield line_string(ctx, pts), {
                "rat_id": r["id"], "net_id": n["id"], "net_name": n["name"],
                "pin_a_id": r["pins"][0], "pin_b_id": r["pins"][1],
                "pins_connected": r.get("pinsConnected"), "user_defined": r.get("userDefined"),
                "pwr_and_gnd": r.get("pwrAndGnd"),
            }


def t_tee(ctx, st):
    for t in ctx.d["tees"]:
        layer, idx = ctx.layer(t["layer"])
        yield point(ctx, t["xy"]), {
            "tee_id": t["id"], "net_id": t.get("net"), "net_name": t.get("netName"),
            "layer": layer, "layer_index": idx,
        }


def label_attrs(t):
    return {
        "label_id": t["id"], "label_text": t.get("text"), "drawing_layer": t.get("layer"),
        "rotation_deg": t.get("rotation"), "is_mirrored": t.get("isMirrored"),
    }


def t_label(ctx, st):
    for t in ctx.d["text"]:
        yield point(ctx, t["xy"]), label_attrs(t)


def t_label_extent(ctx, st):
    for t in ctx.d["text"]:
        geom = rectangle(ctx, t["bBox"])
        if geom is None:
            st["skipped_degenerate"] += 1
            continue
        yield geom, {**label_attrs(t), **bbox_size(t["bBox"])}


def t_drc_marker(ctx, st):
    for x in ctx.d["drcs"]:
        layer, idx = ctx.layer(x["layer"])
        yield point(ctx, x["xy"]), {
            "drc_id": x["id"], "rule": x.get("name"), "constraint_type": x.get("type"),
            "drc_layer": layer, "layer_index": idx, "actual": x.get("actual"), "expected": x.get("expected"),
            "actual_um": parse_um(x.get("actual")), "expected_um": parse_um(x.get("expected")),
            "waived": x.get("waived"),
        }


def design_extent_fields(ctx):
    stats = ctx.d.get("stats", {})
    fields = [("design", "str"), ("source_units", "str"), ("exporter", "str"),
              ("scale", "float"), ("offset_x", "float"), ("offset_y", "float"),
              ("arc_tolerance_um", "float"), ("crs", "str")]
    fields += [("stat_" + snake(k), "int") for k, v in stats.items() if isinstance(v, int)]
    return fields


def t_design_extent(ctx, st):
    meta = ctx.d.get("meta", {})
    geom = rectangle(ctx, meta["bBox"])
    if geom is None:
        st["skipped_degenerate"] += 1
        return
    vals = {"design": meta.get("design"), "source_units": meta.get("units"),
            "exporter": meta.get("exporter"), "scale": ctx.scale, "offset_x": ctx.offset_x,
            "offset_y": ctx.offset_y, "arc_tolerance_um": ctx.tol, "crs": ctx.crs}
    vals.update({"stat_" + snake(k): v for k, v in ctx.d.get("stats", {}).items() if isinstance(v, int)})
    yield geom, vals


PIN_FIELDS = [
    ("pin_id", "str"), ("pin_number", "str"), ("refdes", "str"), ("net_id", "str"), ("net_name", "str"),
    ("padstack", "str"), ("symbol_id", "str"), ("start_layer", "str"), ("end_layer", "str"),
    ("start_layer_index", "int"), ("end_layer_index", "int"), ("rotation_deg", "float"),
    ("is_mirrored", "bool"), ("is_through", "bool"), ("is_mech", "bool"), ("swap_code", "int"),
    ("slot", "str"), ("cds_pin_id", "str"), ("xml_signal", "str"),
]
SYMBOL_FIELDS = [
    ("symbol_id", "str"), ("definition", "str"), ("refdes", "str"), ("type", "str"), ("side", "str"),
    ("rotation_deg", "float"), ("is_mirrored", "bool"), ("mirror_type", "str"), ("n_pins", "int"),
    ("component_id", "str"), ("device_type", "str"), ("component_class", "str"), ("package", "str"),
    ("component_subtype", "str"), ("locked", "bool"), ("is_fixed", "bool"), ("symbol_edited", "bool"),
    ("clip_drawing", "str"), ("via_structure", "str"),
]
TRACE_FIELDS = [
    ("path_id", "str"), ("run_index", "int"), ("net_id", "str"), ("net_name", "str"),
    ("layer", "str"), ("layer_index", "int"), ("width_um", "float"), ("n_segments", "int"),
    ("has_unrecoverable_arc", "bool"), ("is_symbol_etch", "bool"), ("symbol_owned", "bool"),
    ("is_fixed", "bool"), ("clip_drawing", "str"), ("vs_conpath_id", "str"), ("via_array_id", "str"),
]
VIA_FIELDS = [
    ("via_id", "str"), ("net_id", "str"), ("net_name", "str"), ("padstack", "str"),
    ("start_layer", "str"), ("end_layer", "str"), ("start_layer_index", "int"),
    ("end_layer_index", "int"), ("is_through", "bool"), ("symbol_owned", "bool"), ("is_fixed", "bool"),
    ("clip_drawing", "str"), ("retain_net_on_vias", "bool"), ("vs_conpath_id", "str"),
    ("via_array_id", "str"), ("thermal_connect", "str"),
]
LABEL_FIELDS = [
    ("label_id", "str"), ("label_text", "str"), ("drawing_layer", "str"), ("rotation_deg", "float"),
    ("is_mirrored", "bool"),
]
SIZE_FIELDS = [("width_um", "float"), ("height_um", "float")]
DRILL_FIELDS = [("drill_diameter_um", "float"), ("plating", "str")]

# name: (geometry type, fields, producer). A geometry type of None means Polygon, promoted to
# MultiPolygon for the whole table if any repaired feature came out as several parts.
TABLES = {
    "trace": (ogr.wkbLineString, TRACE_FIELDS, t_trace),
    "trace_copper": (None, TRACE_FIELDS, t_trace_copper),
    "copper_area": (None, [
        ("shape_id", "str"), ("net_id", "str"), ("net_name", "str"), ("layer", "str"),
        ("layer_index", "int"), ("is_rect", "bool"), ("is_auto", "bool"), ("n_voids", "int"),
        ("n_arcs", "int"), ("n_vertices", "int"), ("repaired", "bool"),
    ], t_copper_area),
    "via": (ogr.wkbPoint, VIA_FIELDS, t_via),
    "via_extent": (ogr.wkbPolygon, VIA_FIELDS + SIZE_FIELDS, t_via_extent),
    "via_drill": (ogr.wkbPolygon, VIA_FIELDS + DRILL_FIELDS, t_via_drill),
    "pin": (ogr.wkbPoint, PIN_FIELDS, t_pin),
    "pin_extent": (ogr.wkbPolygon, PIN_FIELDS, t_pin_extent),
    "pin_drill": (ogr.wkbPolygon, PIN_FIELDS + DRILL_FIELDS, t_pin_drill),
    "symbol": (ogr.wkbPoint, SYMBOL_FIELDS, t_symbol),
    "symbol_extent": (ogr.wkbPolygon, SYMBOL_FIELDS, t_symbol_extent),
    "ratsnest": (ogr.wkbLineString, [
        ("rat_id", "str"), ("net_id", "str"), ("net_name", "str"), ("pin_a_id", "str"),
        ("pin_b_id", "str"), ("pins_connected", "bool"), ("user_defined", "bool"), ("pwr_and_gnd", "bool"),
    ], t_ratsnest),
    "tee": (ogr.wkbPoint, [
        ("tee_id", "str"), ("net_id", "str"), ("net_name", "str"), ("layer", "str"), ("layer_index", "int"),
    ], t_tee),
    "label": (ogr.wkbPoint, LABEL_FIELDS, t_label),
    "label_extent": (ogr.wkbPolygon, LABEL_FIELDS + SIZE_FIELDS, t_label_extent),
    "drc_marker": (ogr.wkbPoint, [
        ("drc_id", "str"), ("rule", "str"), ("constraint_type", "str"), ("drc_layer", "str"),
        ("layer_index", "int"), ("actual", "str"), ("expected", "str"), ("actual_um", "float"),
        ("expected_um", "float"), ("waived", "bool"),
    ], t_drc_marker),
    "design_extent": (ogr.wkbPolygon, None, t_design_extent),
}


# ---------------------------------------------------------------- writing

def make_srs(crs):
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def write_table(job):
    name, out_dir, fmt = job
    ctx = CTX
    t0 = time.time()
    geom_type, fields, producer = TABLES[name]
    if fields is None:
        fields = design_extent_fields(ctx)
    st = collections.Counter()
    rows = producer(ctx, st)
    if geom_type is None:
        rows = list(rows)
        multi = any(ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbMultiPolygon for g, _ in rows)
        geom_type = ogr.wkbMultiPolygon if multi else ogr.wkbPolygon

    driver = ogr.GetDriverByName(fmt)
    table_name = ctx.prefix + name
    path = os.path.join(out_dir, table_name + FORMATS[fmt])
    if os.path.exists(path):
        driver.DeleteDataSource(path)
    ds = driver.CreateDataSource(path)
    lyr = ds.CreateLayer(table_name, make_srs(ctx.crs), geom_type, LAYER_OPTIONS[fmt])
    for fname, ftype in fields:
        otype, subtype = FIELD_TYPES[ftype]
        fd = ogr.FieldDefn(fname, otype)
        fd.SetSubType(subtype)
        lyr.CreateField(fd)
    defn = lyr.GetLayerDefn()

    transactional = ds.TestCapability(ogr.ODsCTransactions)
    if transactional:
        ds.StartTransaction()
    for geom, vals in rows:
        f = ogr.Feature(defn)
        for k, v in vals.items():
            if v is None:
                f.SetFieldNull(k)
            else:
                f.SetField(k, int(v) if isinstance(v, bool) else v)
        if geom_type == ogr.wkbMultiPolygon and ogr.GT_Flatten(geom.GetGeometryType()) == ogr.wkbPolygon:
            geom = ogr.ForceToMultiPolygon(geom)
        f.SetGeometryDirectly(geom)
        lyr.CreateFeature(f)
        st["features"] += 1
    if transactional:
        ds.CommitTransaction()
    meta = ctx.metadata()
    ds.SetMetadata(meta)
    lyr.SetMetadata(meta)
    ds = None
    return name, path, ogr.GeometryTypeToName(geom_type), dict(st), time.time() - t0


def world_extent_check(ctx, bbox):
    """Warn when the transformed design falls outside the CRS's published area of use."""
    srs = make_srs(ctx.crs)
    corners = [ctx.xy((x, y)) for x in (bbox[0][0], bbox[1][0]) for y in (bbox[0][1], bbox[1][1])]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    print(f"world extent: x {min(xs):.3f} .. {max(xs):.3f}, y {min(ys):.3f} .. {max(ys):.3f} "
          f"({srs.GetLinearUnitsName() if srs.IsProjected() else srs.GetAngularUnitsName()})")
    if srs.IsGeographic():
        print("warning: CRS is geographic; --scale and offsets are in degrees and distances will be distorted",
              file=sys.stderr)
    aou = srs.GetAreaOfUse()
    if aou is None or not srs.IsProjected():
        return
    geo = srs.CloneGeogCS()
    geo.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(srs, geo)
    try:
        lonlat = [ct.TransformPoint(x, y)[:2] for x, y in corners]
    except RuntimeError:
        print(f"warning: the transformed design cannot be expressed in longitude/latitude for {aou.name}; "
              f"reduce --scale or move the offsets", file=sys.stderr)
        return
    lons, lats = [p[0] for p in lonlat], [p[1] for p in lonlat]
    print(f"geographic extent: lon {min(lons):.4f} .. {max(lons):.4f}, lat {min(lats):.4f} .. {max(lats):.4f}")
    if (min(lons) < aou.west_lon_degree or max(lons) > aou.east_lon_degree
            or min(lats) < aou.south_lat_degree or max(lats) > aou.north_lat_degree):
        print(f"warning: design extends outside the CRS area of use ({aou.name}: lon "
              f"{aou.west_lon_degree}..{aou.east_lon_degree}, lat {aou.south_lat_degree}.."
              f"{aou.north_lat_degree}); reduce --scale or move the offsets", file=sys.stderr)


def main():
    global CTX
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", default="design.json", help="design.json export (default: %(default)s)")
    ap.add_argument("-o", "--out-dir", help="output directory (default: <input name>_gis next to the input)")
    ap.add_argument("--crs", default="EPSG:32631",
                    help="output CRS, any GDAL-accepted definition (default: %(default)s, UTM zone 31N)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="CRS units per source micron (default: %(default)s, i.e. 1 um -> 1 m)")
    ap.add_argument("--offset-x", type=float, default=500000.0,
                    help="CRS x of the design origin (default: %(default)s, the UTM central meridian)")
    ap.add_argument("--offset-y", type=float, default=1000000.0,
                    help="CRS y of the design origin (default: %(default)s, about 9 deg N)")
    ap.add_argument("--arc-tolerance-um", type=float, default=0.5,
                    help="maximum deviation of linearized arcs from the true curve, in source microns "
                         "(default: %(default)s)")
    ap.add_argument("--format", choices=sorted(FORMATS), default="GPKG", help="GDAL driver (default: %(default)s)")
    ap.add_argument("--prefix", default="",
                    help="prefix for output file and table names, e.g. 'jags_' gives jags_via.gpkg "
                         "containing table jags_via (default: none)")
    ap.add_argument("--tables", nargs="+", choices=list(TABLES), default=list(TABLES),
                    help="subset of tables to write (default: all)")
    ap.add_argument("-j", "--jobs", type=int, default=min(len(TABLES), os.cpu_count() or 1),
                    help="tables written in parallel (default: %(default)s)")
    args = ap.parse_args()
    if args.scale <= 0:
        ap.error("--scale must be positive")
    if args.arc_tolerance_um <= 0:
        ap.error("--arc-tolerance-um must be positive")
    if args.prefix and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.prefix):
        ap.error("--prefix must start with a letter or underscore and contain only letters, digits and "
                 "underscores")
    make_srs(args.crs)

    out_dir = args.out_dir or os.path.splitext(os.path.abspath(args.input))[0] + "_gis"
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    print(f"loading {args.input} ...", flush=True)
    with open(args.input) as fh:
        design = json.load(fh)
    print(f"loaded in {time.time() - t0:.1f}s")
    CTX = Context(design, args)
    world_extent_check(CTX, design["meta"]["bBox"])

    jobs = [(name, out_dir, args.format) for name in args.tables]
    results = {}

    def report(name, path, gtype, st, secs):
        results[name] = {"file": os.path.basename(path), "geometry": gtype, **st}
        print(f"  {name:14s} {gtype:13s} {st.get('features', 0):>9,d} features  {secs:6.1f}s  "
              + " ".join(f"{k}={v}" for k, v in st.items() if k != "features"), flush=True)

    if args.jobs > 1:
        with multiprocessing.get_context("fork").Pool(min(args.jobs, len(jobs))) as pool:
            for result in pool.imap_unordered(write_table, jobs):
                report(*result)
    else:
        for job in jobs:
            report(*write_table(job))

    summary_path = os.path.join(out_dir, "conversion.json")
    tables = {}
    if os.path.exists(summary_path):
        with open(summary_path) as fh:
            previous = json.load(fh)
        if {k: v for k, v in previous.items() if k != "tables"} == CTX.metadata():
            tables = previous.get("tables", {})
    tables.update(results)
    summary = {**CTX.metadata(), "tables": {k: tables[k] for k in TABLES if k in tables}}
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"wrote {len(results)} files to {out_dir} in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
