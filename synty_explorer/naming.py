"""Name parsing and classification for Synty assets.

Ported and extended from SyntyBrowser's SyntyAssetNaming.cs.
"""
from __future__ import annotations

import re

STRUCTURAL_TOKENS = {"SM", "SK", "ENV", "PROP", "MESH", "PREFAB", "BLD", "FX", "GEN"}

# Token after SM_/SK_ (or after SM_Gen_) -> category
CATEGORY_BY_TOKEN = {
    "prop": "Prop",
    "bld": "Building",
    "wep": "Weapon",
    "chr": "Character",
    "char": "Character",
    "veh": "Vehicle",
    "env": "Environment",
    "item": "Item",
    "generic": "Generic",
    "icon": "Icon",
    "sign": "Sign",
}

LOD_RE = re.compile(r"(?:^|_)LOD\d+(?:_|$)", re.I)
COLLIDER_PREFIX_RE = re.compile(r"^(?:UCX|UBX|UCP|USP)_", re.I)
SPLIT_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


def normalize_id(value: str) -> str:
    v = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return v or "asset"


def _format_token(tok: str) -> str:
    if tok.isdigit() or (len(tok) <= 3 and tok.isupper()):
        return tok
    return tok[0].upper() + tok[1:].lower()


def display_name(source_name: str) -> str:
    value = SPLIT_CAMEL_RE.sub(" ", (source_name or "").strip())
    tokens = [t for t in NON_ALNUM_RE.split(value) if t]
    while tokens and tokens[0].upper() in STRUCTURAL_TOKENS:
        tokens.pop(0)
    # Drop a second-level structural token like "Chr", "Wep" so "SM_Chr_Soldier_Male_01" -> "Soldier Male 01"
    if tokens and tokens[0].lower() in CATEGORY_BY_TOKEN and len(tokens) > 1:
        tokens.pop(0)
    return " ".join(_format_token(t) for t in tokens) if tokens else source_name


def classify(name: str) -> dict:
    """Return {kind, category, generic} from a Synty asset name.

    kind: static | skinned | fx | other
    """
    parts = name.split("_")
    head = parts[0].upper() if parts else ""
    kind = {"SM": "static", "SK": "skinned", "FX": "fx"}.get(head, "other")
    generic = False
    rest = parts[1:]
    if rest and rest[0].lower() == "gen":
        generic = True
        rest = rest[1:]
    category = None
    if kind == "fx":
        category = "FX"
    elif rest:
        category = CATEGORY_BY_TOKEN.get(rest[0].lower())
    if category == "Character" and any(t.lower() in ("attach", "attachment", "attachments") for t in rest[1:2]):
        category = "Attachment"
    if kind == "skinned" and category is None:
        category = "Character"
    if category is None and name.lower().startswith("character_"):
        category, kind = "Character", "skinned"
    return {"kind": kind, "category": category or "Other", "generic": generic}


def is_auxiliary(name: str) -> bool:
    """Collision helpers, LOD files, convex collider shapes."""
    if not name:
        return False
    low = name.lower()
    return (
        low.endswith("_collision")
        or "_collision_" in low
        or bool(LOD_RE.search(name))
        or bool(COLLIDER_PREFIX_RE.match(name))
    )


def is_animation(name: str) -> bool:
    return name.upper().startswith("A_")


def is_auxiliary_path(path: str) -> bool:
    """Path-level exclusions (e.g. Models/CollisionConvex/...)."""
    segs = [s.lower() for s in re.split(r"[\\/]", path)]
    return any(s.startswith("collision") for s in segs[:-1])


BODY_TOKENS = {"femn": "Feminine", "masc": "Masculine", "fem": "Feminine", "male": "Masculine", "female": "Feminine",
               "neut": "Neutral", "neutral": "Neutral"}
PHASE_TOKENS = {"enter", "loop", "exit"}


def animation_info(name: str, path: str = "") -> dict:
    """Parse e.g. Animations/Polygon/Feminine/ArmsFolded/Stances/A_POLY_IDL_ArmsFolded_Casual_Loop_Femn.fbx."""
    tokens = [t for t in name.split("_") if t]
    if tokens and tokens[0].upper() == "A":
        tokens = tokens[1:]
    rig_token = tokens[0].upper() if tokens else ""
    # drop short all-caps codes (POLY, SK, IDL, ...)
    while tokens and tokens[0].isupper() and len(tokens[0]) <= 5:
        tokens = tokens[1:]
    body = None
    if tokens and tokens[-1].lower() in BODY_TOKENS:
        body = BODY_TOKENS[tokens.pop().lower()]
    phase = next((t.capitalize() for t in tokens if t.lower() in PHASE_TOKENS), None)
    root_motion = "RM" in tokens
    additive = any(t.lower() == "additive" for t in tokens)
    tokens = [t for t in tokens if t != "RM"]

    segs = path.replace("\\", "/").split("/")
    low = [s.lower() for s in segs]
    family = None
    if "animations" in low:
        after = segs[low.index("animations") + 1:-1]
        if after:
            family = after[0]
        for s in after[1:]:
            if s.lower() in ("feminine", "masculine", "neutral"):
                body = body or s.capitalize()
        kind_dir = next((s for s in after if s.lower() in ("actions", "stances")), None)
    else:
        kind_dir = None
    if not family:
        family = "Polygon" if rig_token.startswith("POLY") else "Sidekick" if rig_token in ("SK", "SIDE", "SIDEKICK") else None
    pretty = display_name("_".join(tokens)) if tokens else name
    if root_motion:
        pretty += ", root motion"
    return {
        "display_name": f"{pretty} ({body})" if body else pretty,
        "rig_family": family,
        "body": body,
        "phase": phase,
        "motion": kind_dir.capitalize()[:-1] if kind_dir else None,  # Action | Stance
        "root_motion": root_motion,
        "additive": additive,
    }
