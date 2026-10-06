"""Command line: index | search | serve | mcp."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import build_index
from .search import search


def _load(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        sys.exit(f"Index not found: {p}. Run: python -m synty_explorer index <packages...> -o {p}")
    return json.loads(p.read_text("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="synty_explorer", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="build index.json from .unitypackage files, source pack folders or path lists")
    pi.add_argument("inputs", nargs="+", help=".unitypackage, folder (of packages or source packs), or .txt path list")
    pi.add_argument("-o", "--output", default="index.json")
    pi.add_argument("--cache", default="cache", help="where thumbnails/models/textures are extracted (default: ./cache)")
    pi.add_argument("--no-cache", action="store_true", help="index only, extract nothing")
    pi.add_argument("--no-geometry", action="store_true", help="skip FBX bounding boxes (faster)")

    ps = sub.add_parser("search", help="search the index from the terminal")
    ps.add_argument("query")
    ps.add_argument("-i", "--index", default="index.json")
    ps.add_argument("-n", "--limit", type=int, default=20)

    pv = sub.add_parser("serve", help="web UI on http://127.0.0.1:7860")
    pv.add_argument("-i", "--index", default="index.json")
    pv.add_argument("--port", type=int, default=7860)
    pv.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside Docker")

    pm = sub.add_parser("mcp", help="MCP server over stdio")
    pm.add_argument("-i", "--index", default="index.json")

    args = ap.parse_args(argv)
    if args.cmd == "index":
        idx = build_index(args.inputs, None if args.no_cache else args.cache, not args.no_geometry)
        Path(args.output).write_text(json.dumps(idx, indent=1), "utf-8")
        print(f"{idx['asset_count']} assets from {len(idx['packs'])} packs "
              f"({idx['duplicates_removed']} duplicates removed) -> {args.output}", file=sys.stderr)
    elif args.cmd == "search":
        for a in search(_load(args.index)["assets"], args.query, args.limit):
            h = (f"{a['height_m']:.2f} m" if a.get("height_m") is not None
                 else f"{a['duration_s']:.1f} s" if a.get("duration_s") else "   ?   ")
            print(f"{h:>8}  {a['category']:<12} {a['pack']:<20} {a['name']}")
    elif args.cmd == "serve":
        from .server import serve
        serve(_load(args.index), host=args.host, port=args.port)
    elif args.cmd == "mcp":
        from .mcp_server import run
        run(_load(args.index))
    return 0
