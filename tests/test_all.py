"""Tests use only synthetic files (no Synty content). Run: python -m unittest discover tests"""
import io
import json
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from synty_explorer import fbx  # noqa: E402
from synty_explorer.catalog import (build_index, find_texture, parse_material_list,  # noqa: E402
                                    resolve_fbx_candidate)
from synty_explorer.mcp_server import Tools  # noqa: E402
from synty_explorer.naming import classify, display_name, is_auxiliary  # noqa: E402
from synty_explorer.search import search  # noqa: E402


# ---------------------------------------------------------------- synthetic binary FBX writer
def _prop(v):
    if isinstance(v, str):
        b = v.encode()
        return b"S" + struct.pack("<I", len(b)) + b
    if isinstance(v, bool):
        return b"C" + struct.pack("<?", v)
    if isinstance(v, int):
        return b"L" + struct.pack("<q", v)
    if isinstance(v, float):
        return b"D" + struct.pack("<d", v)
    if isinstance(v, list):  # double array, zlib-compressed
        raw = struct.pack(f"<{len(v)}d", *v)
        comp = zlib.compress(raw)
        return b"d" + struct.pack("<III", len(v), 1, len(comp)) + comp
    raise TypeError(v)


def _node(name, props=(), children=(), offset=0, wide=True):
    pbytes = b"".join(_prop(p) for p in props)
    head = 25 if wide else 13
    start = offset + head + len(name) + len(pbytes)
    kids = b""
    for c in children:
        kids += c(start + len(kids))
    if children:
        kids += b"\x00" * head
    end = start + len(kids)
    fmt = "<QQQ" if wide else "<III"
    return struct.pack(fmt, end, len(props), len(pbytes)) + bytes([len(name)]) + name.encode() + pbytes + kids


def N(name, props=(), *children):
    return lambda off: _node(name, props, children, off)


def P(name, typ, *vals):
    return N("P", (name, typ, "", "A", *vals))


def make_fbx(vertices, scaling=(1.0, 1.0, 1.0), rotation=(0.0, 0.0, 0.0), unit=1.0, up=1, bones=(), duration=None):
    header = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", 7500)
    objs = []
    conns = []
    if vertices:
        objs += [N("Geometry", (100, "Geometry::g", "Mesh"), N("Vertices", (list(map(float, vertices)),))),
                 N("Model", (200, "Model::m", "Mesh"), N("Properties70", (),
                                                       P("Lcl Rotation", "Lcl Rotation", *map(float, rotation)),
                                                       P("Lcl Scaling", "Lcl Scaling", *map(float, scaling))))]
        conns += [N("C", ("OO", 100, 200)), N("C", ("OO", 200, 0))]
    for i, b in enumerate(bones):  # binary-style names: "Name\x00\x01Model"
        objs.append(N("Model", (300 + i, f"{b}\x00\x01Model", "LimbNode")))
    if duration:
        objs.append(N("AnimationStack", (900, "Take 001\x00\x01AnimStack", ""),
                      N("Properties70", (), P("LocalStop", "KTime", int(duration * 46186158000)))))
    top = [
        N("GlobalSettings", (), N("Properties70", (), P("UpAxis", "int", up), P("UnitScaleFactor", "double", unit))),
        N("Objects", (), *objs),
        N("Connections", (), *conns),
    ]
    out = header
    for t in top:
        out += t(len(out))
    return out + b"\x00" * 25


# 180 cm tall, 50 cm wide, 30 cm deep box (Y up), in cm units
BOX = [-25, 0, -15, 25, 180, 15, 0, 90, 0]


class FbxTests(unittest.TestCase):
    def test_bbox_in_meters(self):
        info = fbx.inspect(make_fbx(BOX))
        self.assertAlmostEqual(info["height_m"], 1.8, places=3)
        self.assertAlmostEqual(info["size_m"]["x"], 0.5, places=3)
        self.assertEqual(info["unit_scale_cm"], 1.0)

    def test_scaling_rotation_and_units(self):
        # Blender-style export: geometry Z-up in meters, model rotated -90 X, scaled 100, unit 1 cm
        verts = [-0.25, -0.15, 0.0, 0.25, 0.15, 1.8]
        info = fbx.inspect(make_fbx(verts, scaling=(100, 100, 100), rotation=(-90, 0, 0)))
        self.assertAlmostEqual(info["height_m"], 1.8, places=3)
        # UnitScaleFactor 100 (file in meters)
        info = fbx.inspect(make_fbx([0, 0, 0, 1, 2, 1], unit=100.0))
        self.assertAlmostEqual(info["height_m"], 2.0, places=3)

    def test_unit_scale(self):
        self.assertEqual(fbx.unit_scale_cm(make_fbx(BOX, unit=2.54)), 2.54)
        ascii_fbx = b'; FBX 7.4.0\nP: "UnitScaleFactor", "double", "Number", "",100\n'
        self.assertEqual(fbx.unit_scale_cm(ascii_fbx), 100.0)


class NamingTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("SM_Chr_Soldier_Male_01")["category"], "Character")
        self.assertEqual(classify("SM_Gen_Chr_Street_Male_01")["generic"], True)
        self.assertEqual(classify("SM_Chr_Attach_Helmet_01")["category"], "Attachment")
        self.assertEqual(classify("SM_Bld_House_01")["category"], "Building")
        self.assertEqual(classify("SK_Chr_Knight_01")["kind"], "skinned")
        self.assertEqual(classify("FX_Smoke_01")["category"], "FX")

    def test_display_and_aux(self):
        self.assertEqual(display_name("SM_Chr_Soldier_Male_01"), "Soldier Male 01")
        self.assertTrue(is_auxiliary("SM_Bld_House_01_Collision"))
        self.assertTrue(is_auxiliary("SM_Env_Tree_01_LOD1"))
        self.assertTrue(is_auxiliary("UCX_SM_Crate"))
        self.assertFalse(is_auxiliary("SM_Prop_Crate_01"))


class SourcePackTests(unittest.TestCase):
    """Behaviour ported from SyntyBrowser's SyntySourceCatalogTests."""

    def test_material_list(self):
        res = parse_material_list([
            "Folder Name: Prefabs",
            "Prefab Name: SM_Env_Tree_01",
            "    Mesh Name: SM_Env_Tree_01_LOD0",
            "        Slot: Tree_Mat (Uses custom shader)",
            "    Mesh Name: SM_Env_Tree_01_LOD1",
            "        Slot: Tree_Mat (Uses custom shader)",
            "    Mesh Name: SM_Env_Tree_01_MOSS_LOD0",
            "        Slot: Moss_Mat (Moss_Texture)",
        ])
        self.assertEqual(len(res), 1)
        self.assertEqual(len(res[0]["meshes"]), 3)
        self.assertEqual(res[0]["meshes"][1]["lod"], 1)
        self.assertTrue(res[0]["meshes"][0]["slots"][0]["custom_shader"])
        self.assertEqual(res[0]["meshes"][2]["slots"][0]["texture_hint"], "Moss_Texture")

    def test_fbx_candidate(self):
        render, coll = str(Path("Pack/FBX/SM_Bld_House_01.fbx")), str(Path("Pack/Collision/SM_Bld_House_01.fbx"))
        self.assertEqual(resolve_fbx_candidate([coll, render]), render)
        self.assertIsNone(resolve_fbx_candidate([str(Path("Pack/FBX/Props/C.fbx")), str(Path("Pack/FBX/Env/C.fbx"))]))

    def test_texture_locator(self):
        idx = {"polygondarkfortress_texture_01_a": "a.png", "brick_large_texture_01": "b.png"}
        self.assertEqual(find_texture(idx, "PolygonDarkFortress_Mat_01_A"), "a.png")
        self.assertEqual(find_texture(idx, "Brick_Large_01"), "b.png")

    def test_scan_source_dir(self):
        with tempfile.TemporaryDirectory() as d:
            pack = Path(d) / "POLYGON_Test"
            (pack / "FBX").mkdir(parents=True)
            (pack / "FBX" / "SM_Chr_Hero_01.fbx").write_bytes(make_fbx(BOX))
            (pack / "FBX" / "SM_Chr_Hero_01_Collision.fbx").write_bytes(make_fbx(BOX))
            (pack / "Textures").mkdir()
            (pack / "Textures" / "PolygonTest_Texture_01_A.png").write_bytes(b"png")
            (pack / "MaterialList_Test.txt").write_text(
                "Prefab Name: SM_Chr_Hero_01\nMesh Name: SM_Chr_Hero_01\nSlot: PolygonTest_Mat_01_A\n")
            idx = build_index([str(pack)], cache=None)
            self.assertEqual(idx["asset_count"], 1)
            a = idx["assets"][0]
            self.assertAlmostEqual(a["height_m"], 1.8, places=3)
            self.assertTrue(a["has_texture"])


# ---------------------------------------------------------------- synthetic unitypackage
def _add(tar, name, data: bytes):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def make_unitypackage(path: Path, pack: str, height_cm: float, generic=True):
    g = lambda n: f"{n:032x}"  # noqa: E731
    files = {
        g(1): (f"Assets/Synty/{pack}/Models/SM_Chr_Soldier_01.fbx", make_fbx([-25, 0, -15, 25, height_cm, 15])),
        g(2): (f"Assets/Synty/{pack}/Materials/{pack}_Mat_01_A.mat",
               f"m_SavedProperties:\n  m_TexEnvs:\n    - _BumpMap:\n        m_Texture: {{fileID: 2800000, guid: {g(5)}, type: 3}}\n"
               f"    - _BaseMap:\n        m_Texture: {{fileID: 2800000, guid: {g(4)}, type: 3}}\n".encode()),
        g(3): (f"Assets/Synty/{pack}/Prefabs/Characters/SM_Chr_Soldier_01.prefab",
               f"m_Mesh: {{fileID: 4300000, guid: {g(1)}, type: 3}}\nm_Materials:\n- {{fileID: 2100000, guid: {g(2)}, type: 2}}\n".encode()),
        g(4): (f"Assets/Synty/{pack}/Textures/{pack}_Texture_01_A.png", b"\x89PNG"),
        g(5): (f"Assets/Synty/{pack}/Textures/{pack}_Normals.png", b"\x89PNG"),
        g(6): (f"Assets/Synty/{pack}/Models/CollisionConvex/SM_Chr_Soldier_01.fbx", b"x"),
        g(7): (f"Assets/Synty/{pack}/Prefabs/Props/SM_Prop_Crate_01_LOD1.prefab", b""),
    }
    if generic:
        files[g(8)] = ("Assets/Synty/PolygonGeneric/Prefabs/Props/SM_Gen_Prop_Box_01.prefab", b"")
    with tarfile.open(path, "w:gz") as tar:
        for guid, (p, data) in files.items():
            _add(tar, f"{guid}/pathname", p.encode() + b"\n00")
            _add(tar, f"{guid}/asset", data)
            if p.endswith("Soldier_01.prefab"):
                _add(tar, f"{guid}/preview.png", b"\x89PNGpreview")


class UnityPackageTests(unittest.TestCase):
    def test_index_dedupe_scale_and_mcp(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            make_unitypackage(d / "a.unitypackage", "PolygonMilitary", 180)
            make_unitypackage(d / "b.unitypackage", "PolygonCity", 240)
            idx = build_index([str(d)], cache=str(d / "cache"))
            names = sorted(a["id"] for a in idx["assets"])
            self.assertEqual(names, [
                "polygoncity/sm_chr_soldier_01",
                "polygongeneric/sm_gen_prop_box_01",
                "polygonmilitary/sm_chr_soldier_01",
            ])
            self.assertEqual(idx["duplicates_removed"], 1)
            mil = next(a for a in idx["assets"] if a["id"] == "polygonmilitary/sm_chr_soldier_01")
            self.assertAlmostEqual(mil["height_m"], 1.8, places=3)
            self.assertEqual(mil["texture"], "PolygonMilitary_Texture_01_A.png")
            self.assertTrue(mil["has_thumb"] and mil["has_model"] and mil["has_texture"])
            self.assertTrue(Path(mil["_model_path"]).is_file())

            tools = Tools(idx)
            cmp_ = tools.synty_compare_scale(["polygonmilitary/sm_chr_soldier_01", "polygoncity/sm_chr_soldier_01"])
            self.assertEqual(len(cmp_["assets"]), 2)
            self.assertTrue(any(r["outlier"] for r in cmp_["assets"]))
            self.assertNotIn("_model_path", tools.synty_inspect_asset("polygonmilitary/sm_chr_soldier_01"))
            self.assertEqual(search(idx["assets"], "soldr cat:character pack:city")[0]["pack"], "PolygonCity")

            # MCP over stdio, end to end
            (d / "index.json").write_text(json.dumps(idx))
            msgs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "synty_search", "arguments": {"query": "soldier"}}},
            ]
            out = subprocess.run([sys.executable, "-m", "synty_explorer", "mcp", "-i", str(d / "index.json")],
                                 input="\n".join(json.dumps(m) for m in msgs), capture_output=True, text=True, cwd=ROOT)
            replies = [json.loads(line) for line in out.stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3])
            self.assertEqual(len(replies[1]["result"]["tools"]), 4)
            self.assertEqual(json.loads(replies[2]["result"]["content"][0]["text"])["total"], 2)


class AnimationTests(unittest.TestCase):
    def test_clips_mannequin_and_skeleton_link(self):
        bones = ["Hips", "Spine_01", "Head", "Hand_L", "Hand_R"]
        g = lambda n: f"{n + 100:032x}"  # noqa: E731
        base = "Assets/Synty/AnimationIdles"
        files = {
            g(1): (f"{base}/Samples/Meshes/Characters/PolygonSyntyCharacter.fbx", make_fbx(BOX, bones=bones)),
            g(2): (f"{base}/Animations/Polygon/Feminine/Wave/Actions/A_POLY_IDL_Wave_Large_Femn.fbx",
                   make_fbx([], bones=bones, duration=2.5)),
            g(3): (f"{base}/Animations/Sidekick/Feminine/Wave/Actions/A_SK_IDL_Wave_Large_Femn.fbx",
                   make_fbx([], bones=["root", "pelvis", "spine_01", "head"], duration=1.0)),
            g(4): (f"{base}/Samples/Meshes/Props/SM_Prop_Cup_01.fbx", make_fbx(BOX)),
            g(6): (f"{base}/Samples/Prefabs/PF_PolygonPlayer.prefab", f"m_Mesh: {{fileID: 1, guid: {g(1)}, type: 3}}".encode()),
            g(5): (f"{base}/Samples/Prefabs/SM_Prop_Cup_01.prefab", f"m_Mesh: {{fileID: 1, guid: {g(4)}, type: 3}}".encode()),
        }
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d) / "anim.unitypackage"
            with tarfile.open(pkg, "w:gz") as tar:
                for guid, (p, data) in files.items():
                    _add(tar, f"{guid}/pathname", p.encode())
                    _add(tar, f"{guid}/asset", data)
            idx = build_index([str(pkg)], cache=str(Path(d) / "cache"))
            by = {a["name"]: a for a in idx["assets"]}
            self.assertEqual(set(by), {"PolygonSyntyCharacter", "PF_PolygonPlayer", "A_POLY_IDL_Wave_Large_Femn", "A_SK_IDL_Wave_Large_Femn", "SM_Prop_Cup_01"})
            clip = by["A_POLY_IDL_Wave_Large_Femn"]
            self.assertEqual((clip["kind"], clip["category"], clip["phase"], clip["body"]), ("animation", "Animation", None, "Feminine"))
            self.assertEqual(clip["display_name"], "Wave Large (Feminine)")
            self.assertEqual(clip["rig_family"], "Polygon")
            self.assertAlmostEqual(clip["duration_s"], 2.5)
            self.assertTrue(clip["has_model"])
            man = by["PolygonSyntyCharacter"]
            self.assertTrue(man["rigged"])
            self.assertEqual(man["category"], "Character")
            self.assertEqual(clip["preview_rig"], man["id"])
            self.assertEqual(idx["skeletons"][clip["skeleton_id"]]["compatible"][0], man["id"])  # mannequin first, then PF_PolygonPlayer
            self.assertIsNone(by["A_SK_IDL_Wave_Large_Femn"]["preview_rig"])  # no Sidekick mannequin in this package
            self.assertNotIn("_bones", json.dumps(idx))
            self.assertEqual(len(search(idx["assets"], "wave cat:animation")), 2)
            insp = Tools(idx).synty_inspect_asset(clip["id"])
            self.assertEqual(insp["compatible_characters"][0], man["id"])
            from synty_explorer.naming import animation_info
            info = animation_info("A_MOD_GBL_Run_FwdStrafe_FL_RM_Neut",
                                  "Assets/Synty/AnimationGoblinLocomotion/Animations/Sidekick/Neutral/Locomotion/Run/x.fbx")
            self.assertEqual((info["rig_family"], info["body"], info["root_motion"]), ("Sidekick", "Neutral", True))
            self.assertEqual(info["display_name"], "Run Fwd Strafe FL, root motion (Neutral)")


if __name__ == "__main__":
    unittest.main()
