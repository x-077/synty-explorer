"""Build an asset index from Synty content.

Supported inputs (mix freely, one index):
  *.unitypackage     Unity Asset Store / Synty Store packages (no Unity needed)
  <directory>        Synty Store *source* packs (FBX + MaterialList_*.txt), as in SyntyBrowser
  *.txt              a plain list of asset paths (e.g. from `cat */pathname`); names only, no geometry

Each asset is one prefab (unitypackage / path list) or one MaterialList prefab entry (source pack).
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import tarfile
from pathlib import Path

from . import fbx as fbxmod
from .naming import (animation_info, classify, display_name, is_animation, is_auxiliary,
                     is_auxiliary_path, normalize_id)
from .search import resolve_tags

GUID_RE = re.compile(r"guid: ([0-9a-f]{32})")
TEXENV_RE = re.compile(r"-\s*(\w+):\s*\n\s*m_Texture:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-f]{32})")
TEXTURE_EXT = {".png", ".tga", ".jpg", ".jpeg", ".psd"}
WEB_TEXTURE_EXT = {".png", ".tga", ".jpg", ".jpeg"}
TEXTURE_KEY_PRIORITY = ("_basemap", "_maintex", "_albedo", "_base_texture", "_basecolormap", "_basecolor")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- helpers
def pack_from_path(path: str) -> str:
    """Assets/Synty/PolygonMilitary/... -> PolygonMilitary ; Assets/PolygonCity/... -> PolygonCity."""
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    if len(segs) >= 3 and segs[0] == "Assets" and segs[1].lower() == "synty":
        return segs[2]
    if len(segs) >= 2 and segs[0] == "Assets":
        return segs[1]
    return segs[0] if segs else "Unknown"


def group_from_path(path: str) -> str:
    """Sub-folder below Prefabs/ (e.g. 'Weapons/Modular/Weapon_A')."""
    segs = path.replace("\\", "/").split("/")
    low = [s.lower() for s in segs]
    if "prefabs" in low:
        i = low.index("prefabs")
        return "/".join(segs[i + 1:-1])
    return "/".join(segs[-3:-1])


def make_asset(name: str, pack: str, path: str, source: str) -> dict:
    cls = classify(name)
    pack_id = normalize_id(pack)
    asset = {
        "id": f"{pack_id}/{normalize_id(name)}",
        "name": name,
        "display_name": display_name(name),
        "pack": pack,
        "pack_id": pack_id,
        "kind": cls["kind"],
        "category": cls["category"],
        "generic": cls["generic"] or pack.lower().endswith("generic"),
        "group": group_from_path(path),
        "path": path,
        "source": source,
        "size_m": None,
        "height_m": None,
        "unit_scale_cm": None,
        "has_thumb": False,
        "has_model": False,
        "has_texture": False,
        "error": None,
    }
    asset["tags"] = resolve_tags(asset)
    return asset


def make_animation_asset(name: str, pack: str, path: str, source: str) -> dict:
    a = make_asset(name, pack, path, source)
    info = animation_info(name, path)
    a.update(kind="animation", category="Animation", display_name=info["display_name"],
             rig_family=info["rig_family"], body=info["body"], phase=info["phase"], motion=info["motion"],
             root_motion=info["root_motion"], additive=info["additive"],
             duration_s=None, fbx=Path(path).name, materials=[], texture=None)
    segs = path.replace("\\", "/").split("/")
    low = [x.lower() for x in segs]
    a["group"] = "/".join(segs[low.index("animations") + 1:-1]) if "animations" in low else "/".join(segs[-4:-1])
    a["tags"] = []
    return a


def make_fbx_asset(name: str, pack: str, path: str, source: str) -> dict:
    """An FBX no prefab points at (e.g. sample mannequins in animation packs)."""
    a = make_asset(name, pack, path, source)
    if a["category"] == "Other" and any(s.lower() == "characters" for s in path.replace("\\", "/").split("/")):
        a["category"] = "Character"
    a.update(fbx=Path(path).name, materials=[], texture=None)
    return a


def _choose_texture(mat_text: str, guid_path: dict) -> str | None:
    found = [(k.lower(), g) for k, g in TEXENV_RE.findall(mat_text) if g in guid_path]
    found = [(k, g) for k, g in found if Path(guid_path[g]).suffix.lower() in TEXTURE_EXT]
    if not found:
        return None
    for pref in TEXTURE_KEY_PRIORITY:
        for k, g in found:
            if k.startswith(pref):
                return g
    for k, g in found:
        if not any(x in k for x in ("normal", "bump", "mask", "emission", "metal", "occlusion", "rough")):
            return g
    return None


def _apply_geometry(asset: dict, data: bytes) -> None:
    try:
        info = fbxmod.inspect(data)
    except Exception as exc:  # noqa: BLE001 - keep indexing on bad files
        asset["error"] = f"geometry: {exc}"
        return
    asset["unit_scale_cm"] = info["unit_scale_cm"]
    if asset["kind"] == "animation":
        asset["duration_s"] = info["duration_s"]
    else:
        asset["size_m"] = info["size_m"]
        asset["height_m"] = info["height_m"]
    if info["bones"]:
        asset["_bones"] = info["bones"]
        asset["bone_count"] = len(info["bones"])
        if asset["kind"] != "animation":
            asset["rigged"] = True


# ---------------------------------------------------------------- unitypackage
def scan_unitypackage(pkg: Path, cache: Path | None, geometry: bool = True) -> list[dict]:
    log(f"[unitypackage] {pkg.name}: pass 1/3 (paths)")
    guid_path: dict[str, str] = {}
    with tarfile.open(pkg, "r:*") as tar:
        for m in tar:
            if m.isfile() and m.name.endswith("/pathname"):
                guid = m.name.split("/")[-2]
                raw = tar.extractfile(m).read().decode("utf-8", "replace")
                guid_path[guid] = raw.splitlines()[0].strip() if raw else ""
    path_guid = {p: g for g, p in guid_path.items()}

    def wanted_prefab(p: str) -> bool:
        return p.lower().endswith(".prefab") and not is_auxiliary_path(p)

    prefab_guids = {g for g, p in guid_path.items() if wanted_prefab(p)}
    fbx_guids = {g for g, p in guid_path.items() if p.lower().endswith(".fbx") and not is_auxiliary_path(p)
                 and not is_auxiliary(Path(p).stem)}
    mat_guids = {g for g, p in guid_path.items() if p.lower().endswith(".mat")}

    log(f"[unitypackage] {pkg.name}: pass 2/3 (prefabs, materials, previews) – {len(prefab_guids)} prefabs")
    prefab_text: dict[str, str] = {}
    mat_text: dict[str, str] = {}
    previews: dict[str, bytes] = {}
    with tarfile.open(pkg, "r:*") as tar:
        for m in tar:
            if not m.isfile():
                continue
            guid, _, leaf = m.name.rpartition("/")
            guid = guid.split("/")[-1]
            if leaf == "asset" and guid in prefab_guids:
                prefab_text[guid] = tar.extractfile(m).read().decode("utf-8", "replace")
            elif leaf == "asset" and guid in mat_guids:
                mat_text[guid] = tar.extractfile(m).read().decode("utf-8", "replace")
            elif leaf == "preview.png" and guid in prefab_guids and cache is not None:
                previews[guid] = tar.extractfile(m).read()

    assets: list[dict] = []
    need_fbx: dict[str, list[dict]] = {}
    need_tex: dict[str, list[dict]] = {}
    referenced: set[str] = set()
    for guid in sorted(prefab_guids, key=lambda g: guid_path[g].lower()):
        path = guid_path[guid]
        name = Path(path).stem
        if is_auxiliary(name) or is_animation(name):
            continue
        a = make_asset(name, pack_from_path(path), path, f"unitypackage:{pkg.name}")
        refs = GUID_RE.findall(prefab_text.get(guid, ""))
        fbx_refs = [g for g in dict.fromkeys(refs)
                    if guid_path.get(g, "").lower().endswith(".fbx") and not is_auxiliary_path(guid_path[g])
                    and not is_auxiliary(Path(guid_path[g]).stem)]
        fbx_refs.sort(key=lambda g: Path(guid_path[g]).stem.lower() != name.lower())
        a["fbx"] = Path(guid_path[fbx_refs[0]]).name if fbx_refs else None
        referenced.update(g for g in fbx_refs if Path(guid_path[g]).stem.lower() == name.lower())
        mats = [g for g in dict.fromkeys(refs) if g in mat_text]
        a["materials"] = [Path(guid_path[g]).stem for g in mats]
        tex = next((t for t in (_choose_texture(mat_text[g], guid_path) for g in mats) if t), None)
        a["texture"] = Path(guid_path[tex]).name if tex else None
        if fbx_refs and (geometry or cache):
            need_fbx.setdefault(fbx_refs[0], []).append(a)
        if tex and cache and Path(guid_path[tex]).suffix.lower() in WEB_TEXTURE_EXT:
            need_tex.setdefault(tex, []).append(a)
        if guid in previews:
            dst = cache / "thumbs" / a["pack_id"] / f"{normalize_id(name)}.png"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(previews[guid])
            a["_thumb_path"] = str(dst)
            a["has_thumb"] = True
        assets.append(a)

    # FBX files no prefab uses: animation clips (A_*) and sample meshes/mannequins
    for guid in sorted(fbx_guids - referenced, key=lambda g: guid_path[g].lower()):
        path = guid_path[guid]
        name = Path(path).stem
        maker = make_animation_asset if is_animation(name) else make_fbx_asset
        a = maker(name, pack_from_path(path), path, f"unitypackage:{pkg.name}")
        assets.append(a)
        if geometry or cache:
            need_fbx.setdefault(guid, []).append(a)

    if need_fbx or need_tex:
        log(f"[unitypackage] {pkg.name}: pass 3/3 (geometry) – {len(need_fbx)} FBX, {len(need_tex)} textures")
        with tarfile.open(pkg, "r:*") as tar:
            for m in tar:
                if not m.isfile() or not m.name.endswith("/asset"):
                    continue
                guid = m.name.split("/")[-2]
                if guid in need_fbx:
                    data = tar.extractfile(m).read()
                    stored = None
                    if cache is not None:
                        pid = need_fbx[guid][0]["pack_id"]
                        stored = cache / "models" / pid / Path(guid_path[guid]).name
                        stored.parent.mkdir(parents=True, exist_ok=True)
                        stored.write_bytes(data)
                    for a in need_fbx[guid]:
                        if geometry:
                            _apply_geometry(a, data)
                        if stored:
                            a["_model_path"] = str(stored)
                            a["has_model"] = True
                elif guid in need_tex:
                    pid = need_tex[guid][0]["pack_id"]
                    stored = cache / "textures" / pid / Path(guid_path[guid]).name
                    stored.parent.mkdir(parents=True, exist_ok=True)
                    stored.write_bytes(tar.extractfile(m).read())
                    for a in need_tex[guid]:
                        a["_texture_path"] = str(stored)
                        a["has_texture"] = True
    del path_guid
    return assets


# ---------------------------------------------------------------- source packs (SyntyBrowser port)
LOD_FAMILY_RE = re.compile(r"^(?P<family>.+?)_LOD(?P<level>\d+)$", re.I)


def parse_material_list(lines) -> list[dict]:
    """Parse Synty MaterialList_*.txt into [{prefab, folder, meshes:[{name, lod, slots:[{name, texture_hint, custom_shader}]}]}]."""
    out, folder, cur = [], None, None
    for raw in lines:
        line = (raw or "").strip()
        low = line.lower()
        if low.startswith("folder name:"):
            folder = line[len("Folder Name:"):].strip()
        elif low.startswith("prefab name:"):
            cur = {"prefab": line[len("Prefab Name:"):].strip(), "folder": folder, "meshes": []}
            out.append(cur)
        elif low.startswith("mesh name:") and cur is not None:
            name = line[len("Mesh Name:"):].strip()
            m = LOD_FAMILY_RE.match(name)
            cur["meshes"].append({"name": name, "lod": int(m["level"]) if m else None, "slots": []})
        elif low.startswith("slot:") and cur is not None and cur["meshes"]:
            value = line[len("Slot:"):].strip()
            detail = None
            i = value.rfind(" (")
            if i >= 0 and value.endswith(")"):
                detail = value[i + 2:-1].strip()
                value = value[:i].strip()
            custom = (detail or "").lower() == "uses custom shader"
            cur["meshes"][-1]["slots"].append(
                {"name": value, "texture_hint": None if custom else detail, "custom_shader": custom})
    return out


def resolve_fbx_candidate(matches: list[str]) -> str | None:
    """Same rule as SyntyBrowser: unique match, else unique under an 'FBX' folder, else non-'Generic'."""
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    canonical = [p for p in matches if any(s.lower() == "fbx" for s in Path(p).parent.parts)]
    if len(canonical) == 1:
        return canonical[0]
    specific = [p for p in canonical if not any(s.lower() == "generic" for s in Path(p).parent.parts)]
    return specific[0] if len(specific) == 1 else None


def find_texture(index: dict[str, str], hint: str | None) -> str | None:
    """Port of SyntyTextureLocator: exact name, then *_Mat_* -> *_Texture_* style guesses."""
    if not hint:
        return None
    if hint.lower() in index:
        return index[hint.lower()]
    cands = {hint.replace("_Mat_", "_Texture_")}
    simple = hint.replace("_Double_", "_").replace("_Half_", "_")
    m = re.match(r"^(?P<prefix>.+?)(?P<suffix>_\d+(?:_[A-Za-z])?)$", simple)
    if m:
        cands.add(f"{m['prefix']}_Texture{m['suffix']}")
    for c in cands:
        if c.lower() in index:
            return index[c.lower()]
    return None


def _find_pack_roots(root: Path) -> list[Path]:
    if any(root.glob("MaterialList_*.txt")) or any(root.glob("*.fbx")):
        return [root]
    kids = sorted(d for d in root.iterdir() if d.is_dir() and next(d.rglob("*.fbx"), None))
    return kids or [root]


def scan_source_dir(root: Path, geometry: bool = True) -> list[dict]:
    assets = []
    for pack_root in _find_pack_roots(root):
        pack = pack_root.name
        log(f"[source] {pack}")
        fbx_files = sorted(str(p) for p in pack_root.rglob("*") if p.suffix.lower() == ".fbx")
        by_name: dict[str, list[str]] = {}
        for p in fbx_files:
            by_name.setdefault(Path(p).stem.lower(), []).append(p)
        tex_index = {}
        for p in pack_root.rglob("*"):
            if p.suffix.lower() in WEB_TEXTURE_EXT:
                tex_index.setdefault(p.stem.lower(), str(p))
        claimed = set()
        entries = []
        for ml in sorted(pack_root.rglob("MaterialList_*.txt")):
            entries += parse_material_list(ml.read_text("utf-8", "replace").splitlines())
        seen = set()
        for e in entries:
            name = e["prefab"]
            if is_auxiliary(name) or is_animation(name) or name.lower() in seen:
                continue
            seen.add(name.lower())
            path = resolve_fbx_candidate(by_name.get(name.lower(), []))
            a = make_asset(name, pack, path or name, f"source:{pack_root}")
            a["group"] = e["folder"] or a["group"]
            slots = [s for mesh in e["meshes"] for s in mesh["slots"]]
            a["materials"] = list(dict.fromkeys(s["name"] for s in slots))
            tex = next((t for t in (find_texture(tex_index, s["texture_hint"] or s["name"]) for s in slots) if t), None)
            a["fbx"] = Path(path).name if path else None
            if path:
                claimed.add(path)
                a["_model_path"] = path
                a["has_model"] = True
            else:
                a["error"] = f"No unique FBX named '{name}.fbx'"
            if tex:
                a["_texture_path"], a["has_texture"], a["texture"] = tex, True, Path(tex).name
            assets.append(a)
        for p in fbx_files:  # fallback: FBX not listed in any MaterialList
            name = Path(p).stem
            if p in claimed or is_auxiliary(name) or is_auxiliary_path(p) or name.lower() in seen:
                continue
            seen.add(name.lower())
            rel = os.path.relpath(p, pack_root).replace(os.sep, "/")
            maker = make_animation_asset if is_animation(name) else make_fbx_asset
            a = maker(name, pack, rel, f"source:{pack_root}")
            a.update(_model_path=p, has_model=True)
            assets.append(a)
        if geometry:
            for a in assets:
                if a.get("_model_path") and a["pack"] == pack and a["size_m"] is None:
                    try:
                        _apply_geometry(a, Path(a["_model_path"]).read_bytes())
                    except OSError as exc:
                        a["error"] = str(exc)
    return assets


# ---------------------------------------------------------------- path list
def scan_path_list(txt: Path) -> list[dict]:
    log(f"[list] {txt.name}")
    assets = []
    for line in txt.read_text("utf-8", "replace").splitlines():
        p = line.strip()
        name = Path(p).stem
        if is_auxiliary_path(p) or is_auxiliary(name):
            continue
        if p.lower().endswith(".fbx") and is_animation(name):
            assets.append(make_animation_asset(name, pack_from_path(p), p, f"list:{txt.name}"))
        elif p.lower().endswith(".prefab") and not is_animation(name):
            a = make_asset(name, pack_from_path(p), p, f"list:{txt.name}")
            a.update(fbx=None, materials=[], texture=None)
            assets.append(a)
    return assets


# ---------------------------------------------------------------- entry point
def build_index(inputs: list[str], cache: str | None = None, geometry: bool = True) -> dict:
    cache_dir = Path(cache).resolve() if cache else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    all_assets: list[dict] = []
    for raw in inputs:
        p = Path(raw).expanduser()
        if p.is_dir():
            pkgs = sorted(p.rglob("*.unitypackage"))
            if pkgs:
                for pkg in pkgs:
                    all_assets += scan_unitypackage(pkg, cache_dir, geometry)
            else:
                all_assets += scan_source_dir(p, geometry)
        elif p.suffix.lower() == ".unitypackage":
            all_assets += scan_unitypackage(p, cache_dir, geometry)
        elif p.suffix.lower() == ".txt":
            all_assets += scan_path_list(p)
        else:
            log(f"skip: {p} (not a .unitypackage, .txt or directory)")

    # Dedupe: shared packs (e.g. PolygonGeneric ships inside many packages) appear once.
    unique: dict[str, dict] = {}
    dupes = 0
    for a in all_assets:
        if a["id"] in unique:
            dupes += 1
            kept = unique[a["id"]]
            for k in ("size_m", "height_m", "unit_scale_cm", "_model_path", "_texture_path", "_thumb_path"):
                if kept.get(k) is None and a.get(k) is not None:
                    kept[k] = a[k]
            for k in ("has_model", "has_texture", "has_thumb"):
                kept[k] = kept[k] or a[k]
            continue
        unique[a["id"]] = a
    assets = sorted(unique.values(), key=lambda a: (a["pack"].lower(), a["display_name"].lower()))
    skeletons = link_skeletons(assets)
    packs: dict[str, dict] = {}
    for a in assets:
        pk = packs.setdefault(a["pack"], {"name": a["pack"], "id": a["pack_id"], "count": 0})
        pk["count"] += 1
    return {
        "version": 1,
        "asset_count": len(assets),
        "duplicates_removed": dupes,
        "packs": sorted(packs.values(), key=lambda x: x["name"].lower()),
        "skeletons": skeletons,
        "assets": assets,
    }


def link_skeletons(assets: list[dict], min_overlap: float = 0.9) -> dict:
    """Group animation clips by skeleton and list the rigged models each skeleton can play on.

    A clip skeleton is compatible with a rigged model when >= 90% of its bone names exist in the model.
    Bone lists are dropped afterwards to keep the index small.
    """
    for a in assets:
        if a.get("rigged") and a["category"] == "Other" and "character" in a["name"].lower():
            a["category"] = "Character"
    rigged = [a for a in assets if a.get("rigged") and a.get("_bones")]
    skeletons: dict[str, dict] = {}
    for a in assets:
        bones = a.get("_bones")
        if a["kind"] != "animation" or not bones:
            continue
        sid = hashlib.sha1(",".join(bones).encode()).hexdigest()[:10]
        a["skeleton_id"] = sid
        if sid not in skeletons:
            bs = set(bones)
            compat = [r for r in rigged if len(bs & set(r["_bones"])) >= min_overlap * len(bs)]
            # mannequins / sample rigs from the clip's own pack first
            compat.sort(key=lambda r: (r["pack"] != a["pack"], "character" not in r["name"].lower(), r["display_name"].lower()))
            skeletons[sid] = {"bone_count": len(bs), "compatible": [r["id"] for r in compat]}
    for a in assets:
        if a["kind"] == "animation":
            sk = skeletons.get(a.get("skeleton_id") or "", {})
            fam = (a.get("rig_family") or "").lower()
            compat = sk.get("compatible", [])
            a["preview_rig"] = next((c for c in compat if fam and fam in c and "character" in c), compat[0] if compat else None)
        a.pop("_bones", None)
    return skeletons


def public_index(index: dict) -> dict:
    """Index without private absolute paths (keys starting with '_')."""
    return {**index, "assets": [{k: v for k, v in a.items() if not k.startswith("_")} for a in index["assets"]]}
