"""Minimal FBX reader (stdlib only): unit scale + approximate world bounding box.

Binary FBX (7.x) is fully parsed. ASCII FBX only supports UnitScaleFactor (like SyntyBrowser).

Bounding box: every Geometry's vertices are transformed by the full chain of its Model's
Lcl Translation / PreRotation * Lcl Rotation / Lcl Scaling up to the root, then converted to
meters with UnitScaleFactor (cm per file unit). Pivots/offsets are ignored, so treat the result
as a very good approximation, not an exact Unity import.
"""
from __future__ import annotations

import math
import re
import struct
import zlib

BINARY_MAGIC = b"Kaydara FBX Binary  \x00"

_ARRAY_FMT = {b"f": ("f", 4), b"d": ("d", 8), b"l": ("q", 8), b"i": ("i", 4), b"b": ("b", 1)}
_SCALAR_FMT = {b"Y": ("<h", 2), b"C": ("<?", 1), b"I": ("<i", 4), b"F": ("<f", 4), b"D": ("<d", 8), b"L": ("<q", 8)}


class FbxError(ValueError):
    pass


class Node:
    __slots__ = ("name", "props", "children")

    def __init__(self, name, props, children):
        self.name, self.props, self.children = name, props, children

    def find(self, name):
        return next((c for c in self.children if c.name == name), None)

    def find_all(self, name):
        return [c for c in self.children if c.name == name]

    def __repr__(self):
        return f"Node({self.name!r}, {len(self.props)} props, {len(self.children)} children)"


# ---------------------------------------------------------------- binary parsing
def _read_props(buf, off, count):
    props = []
    for _ in range(count):
        t = buf[off:off + 1]
        off += 1
        if t in _SCALAR_FMT:
            fmt, size = _SCALAR_FMT[t]
            props.append(struct.unpack_from(fmt, buf, off)[0])
            off += size
        elif t in _ARRAY_FMT:
            n, enc, clen = struct.unpack_from("<III", buf, off)
            off += 12
            raw = buf[off:off + clen]
            off += clen
            if enc == 1:
                raw = zlib.decompress(raw)
            code, _ = _ARRAY_FMT[t]
            props.append(list(struct.unpack(f"<{n}{code}", raw[: n * _ARRAY_FMT[t][1]])))
        elif t in (b"S", b"R"):
            (n,) = struct.unpack_from("<I", buf, off)
            off += 4
            data = bytes(buf[off:off + n])
            off += n
            props.append(data.decode("utf-8", "replace") if t == b"S" else data)
        else:
            raise FbxError(f"unknown property type {t!r} at {off - 1}")
    return props, off


def _read_node(buf, off, wide):
    if wide:
        end, nprops, _plen = struct.unpack_from("<QQQ", buf, off)
        off += 24
    else:
        end, nprops, _plen = struct.unpack_from("<III", buf, off)
        off += 12
    nlen = buf[off]
    off += 1
    if end == 0:
        return None, off
    name = bytes(buf[off:off + nlen]).decode("ascii", "replace")
    off += nlen
    props, off = _read_props(buf, off, nprops)
    children = []
    while off < end:
        child, off = _read_node(buf, off, wide)
        if child is None:
            break
        children.append(child)
    return Node(name, props, children), end


def parse_binary(data: bytes) -> Node:
    if not data.startswith(BINARY_MAGIC[:18]):
        raise FbxError("not a binary FBX")
    (version,) = struct.unpack_from("<I", data, 23)
    wide = version >= 7500
    off = 27
    top = []
    buf = memoryview(data)
    while off < len(data):
        node, off = _read_node(buf, off, wide)
        if node is None:
            break
        top.append(node)
    return Node("root", [version], top)


# ---------------------------------------------------------------- properties
def _props70(node: Node | None) -> dict:
    out = {}
    if node is None:
        return out
    p70 = node.find("Properties70")
    if p70 is None:
        return out
    for p in p70.find_all("P"):
        if p.props:
            out[p.props[0]] = p.props[4:]
    return out


_TEXT_USF = re.compile(rb'P:\s*"UnitScaleFactor"\s*,\s*"double"\s*,\s*"Number"\s*,\s*""\s*,\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)')


def unit_scale_cm(data: bytes) -> float:
    """Centimeters per FBX file unit (FBX default is 1.0 = cm)."""
    if data.startswith(BINARY_MAGIC[:18]):
        gs = _props70(parse_binary(data).find("GlobalSettings"))
        v = gs.get("UnitScaleFactor", [1.0])
        return float(v[0]) if v else 1.0
    m = _TEXT_USF.search(data)
    if not m:
        raise FbxError("ASCII FBX without UnitScaleFactor")
    return float(m.group(1))


# ---------------------------------------------------------------- math
def _mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _translate(x, y, z):
    return [[1, 0, 0, x], [0, 1, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]


def _scale(x, y, z):
    return [[x, 0, 0, 0], [0, y, 0, 0], [0, 0, z, 0], [0, 0, 0, 1]]


def _rot_xyz(rx, ry, rz):
    """FBX default eEulerXYZ: R = Rz * Ry * Rx (degrees)."""
    rx, ry, rz = (math.radians(v) for v in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    mx = [[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]]
    my = [[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]]
    mz = [[cz, -sz, 0, 0], [sz, cz, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    return _mat_mul(mz, _mat_mul(my, mx))


def _vec3(props: dict, key, default):
    v = props.get(key)
    return tuple(float(x) for x in v[:3]) if v and len(v) >= 3 else default


def _local_matrix(model: Node):
    p = _props70(model)
    t = _vec3(p, "Lcl Translation", (0.0, 0.0, 0.0))
    pre = _vec3(p, "PreRotation", (0.0, 0.0, 0.0))
    r = _vec3(p, "Lcl Rotation", (0.0, 0.0, 0.0))
    s = _vec3(p, "Lcl Scaling", (1.0, 1.0, 1.0))
    return _mat_mul(_translate(*t), _mat_mul(_rot_xyz(*pre), _mat_mul(_rot_xyz(*r), _scale(*s))))


# ---------------------------------------------------------------- bbox / bones / animation
KTIME_PER_SECOND = 46186158000


def _model_name(raw) -> str:
    name = str(raw).split("\x00\x01")[0]
    return name[7:] if name.startswith("Model::") else name


def _duration_s(objects: Node) -> float | None:
    for stack in objects.find_all("AnimationStack"):
        p = _props70(stack)
        start = (p.get("LocalStart") or [0])[0]
        stop = (p.get("LocalStop") or [None])[0]
        if stop:
            return round((int(stop) - int(start or 0)) / KTIME_PER_SECOND, 3)
    return None


def inspect(data: bytes) -> dict:
    """Return unit scale, bounding box (meters, may be None), bone names and animation duration."""
    root = parse_binary(data)
    gs = _props70(root.find("GlobalSettings"))
    usf = float((gs.get("UnitScaleFactor") or [1.0])[0])
    up_axis = int((gs.get("UpAxis") or [1])[0])
    up = "xyz"[up_axis] if 0 <= up_axis <= 2 else "y"

    objects = root.find("Objects")
    conns = root.find("Connections")
    if objects is None:
        raise FbxError("FBX has no Objects section")
    models = {m.props[0]: m for m in objects.find_all("Model") if m.props}
    geoms = {g.props[0]: g for g in objects.find_all("Geometry") if g.props}
    bones = sorted({_model_name(m.props[1]) for m in models.values() if len(m.props) > 2 and m.props[2] == "LimbNode"})
    parent = {}
    geom_model = {}
    for c in (conns.find_all("C") if conns else []):
        if len(c.props) >= 3 and c.props[0] == "OO":
            child, par = c.props[1], c.props[2]
            if child in geoms and par in models:
                geom_model.setdefault(child, par)
            elif child in models:
                parent[child] = par

    def world(mid, depth=0):
        m = _local_matrix(models[mid])
        p = parent.get(mid)
        if p in models and depth < 64:
            return _mat_mul(world(p, depth + 1), m)
        return m

    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    count = 0
    for gid, geom in geoms.items():
        vnode = geom.find("Vertices")
        if vnode is None or not vnode.props or not vnode.props[0]:
            continue
        verts = vnode.props[0]
        mid = geom_model.get(gid)
        mat = world(mid) if mid in models else None
        count += 1
        for i in range(0, len(verts) - 2, 3):
            x, y, z = verts[i], verts[i + 1], verts[i + 2]
            if mat:
                x, y, z = (
                    mat[0][0] * x + mat[0][1] * y + mat[0][2] * z + mat[0][3],
                    mat[1][0] * x + mat[1][1] * y + mat[1][2] * z + mat[1][3],
                    mat[2][0] * x + mat[2][1] * y + mat[2][2] * z + mat[2][3],
                )
            for k, v in enumerate((x, y, z)):
                if v < lo[k]:
                    lo[k] = v
                if v > hi[k]:
                    hi[k] = v

    out = {
        "unit_scale_cm": usf, "up_axis": up, "geometry_count": count,
        "size_m": None, "height_m": None, "min": None, "max": None,
        "bones": bones, "duration_s": _duration_s(objects),
    }
    if count and lo[0] is not math.inf:
        to_m = usf / 100.0
        lo_m = [round(v * to_m, 4) for v in lo]
        hi_m = [round(v * to_m, 4) for v in hi]
        size = {ax: round(hi_m[i] - lo_m[i], 4) for i, ax in enumerate("xyz")}
        out.update(size_m=size, height_m=size[up], min=lo_m, max=hi_m)
    return out
