"""Minimal MCP server over stdio (JSON-RPC 2.0, newline-delimited), no dependencies.

Tools mirror SyntyBrowser's read-only MCP surface, plus search and scale comparison:
  synty_catalog_status, synty_search, synty_inspect_asset, synty_compare_scale
"""
from __future__ import annotations

import json
import sys

from .catalog import public_index
from .search import TAGS, search

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "synty_catalog_status",
        "description": "Summary of the indexed Synty library: packs, asset counts per category, how many assets have measured size.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "synty_search",
        "description": (
            "Fuzzy search Synty assets and animation clips. Filters: pack:<name> "
            "cat:<prop|building|character|attachment|weapon|vehicle|environment|item|fx|animation> "
            "kind:<static|skinned|fx|animation> tag:<" + "|".join(TAGS) + ">. Example: 'barrel cat:prop pack:military'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 200},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "synty_inspect_asset",
        "description": "Full metadata for one asset id (e.g. 'polygonmilitary/sm_chr_soldier_male_01'): path, category, size in meters, materials, texture. "
                       "For animation clips: duration, rig family, body type, phase, and the characters it can play on.",
        "inputSchema": {"type": "object", "properties": {"asset_id": {"type": "string"}}, "required": ["asset_id"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "synty_compare_scale",
        "description": "Compare measured bounding-box sizes (meters) of several assets, e.g. characters from different packs, and flag outliers.",
        "inputSchema": {
            "type": "object",
            "properties": {"asset_ids": {"type": "array", "items": {"type": "string"}, "minItems": 2}},
            "required": ["asset_ids"],
        },
        "annotations": {"readOnlyHint": True},
    },
]

SUMMARY_KEYS = ("id", "display_name", "pack", "category", "kind", "group", "height_m", "size_m", "duration_s")


def _summary(a: dict) -> dict:
    return {k: a.get(k) for k in SUMMARY_KEYS}


class Tools:
    def __init__(self, index: dict):
        self.index = public_index(index)
        self.by_id = {a["id"]: a for a in self.index["assets"]}
        self.skeletons = index.get("skeletons", {})

    def synty_catalog_status(self):
        cats: dict[str, int] = {}
        for a in self.index["assets"]:
            cats[a["category"]] = cats.get(a["category"], 0) + 1
        return {
            "asset_count": self.index["asset_count"],
            "measured_count": sum(1 for a in self.index["assets"] if a.get("height_m") is not None),
            "packs": self.index["packs"],
            "categories": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
            "tags": list(TAGS),
        }

    def synty_search(self, query: str, limit: int = 25):
        res = search(self.index["assets"], query)
        return {"total": len(res), "results": [_summary(a) for a in res[: max(1, min(int(limit), 200))]]}

    def synty_inspect_asset(self, asset_id: str):
        a = self.by_id.get(asset_id) or self.by_id.get(asset_id.lower())
        if not a:
            hits = search(self.index["assets"], asset_id.split("/")[-1], 5)
            return {"error": f"asset '{asset_id}' not found", "did_you_mean": [h["id"] for h in hits]}
        if a.get("kind") == "animation":
            compat = self.skeletons.get(a.get("skeleton_id") or "", {}).get("compatible", [])
            return {**a, "compatible_characters": compat[:50]}
        return a

    def synty_compare_scale(self, asset_ids: list[str]):
        rows, missing = [], []
        for i in asset_ids:
            a = self.by_id.get(i)
            if not a or a.get("height_m") is None:
                missing.append(i)
            else:
                rows.append(_summary(a))
        heights = sorted(r["height_m"] for r in rows)
        median = heights[len(heights) // 2] if heights else None
        for r in rows:
            r["vs_median"] = round(r["height_m"] / median, 3) if median else None
            r["outlier"] = bool(median) and not (0.8 <= r["height_m"] / median <= 1.25)
        return {"median_height_m": median, "assets": rows, "missing_or_unmeasured": missing}


def run(index: dict) -> None:
    tools = Tools(index)

    def reply(msg_id, result=None, error=None):
        out = {"jsonrpc": "2.0", "id": msg_id}
        out["error" if error else "result"] = error or result
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:  # notification (e.g. notifications/initialized)
            continue
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synty-explorer", "version": "0.1.0"},
            })
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            fn = getattr(tools, name, None) if name in {t["name"] for t in TOOLS} else None
            if fn is None:
                reply(msg_id, error={"code": -32602, "message": f"unknown tool {name}"})
                continue
            try:
                data = fn(**args)
                reply(msg_id, {"content": [{"type": "text", "text": json.dumps(data, indent=1)}], "isError": False})
            except Exception as exc:  # noqa: BLE001
                reply(msg_id, {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True})
        else:
            reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
