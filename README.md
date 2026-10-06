# Synty Explorer

Search, preview and compare your Synty assets **outside Unity**: a local web UI and an MCP server
over one index. Python 3.10+, standard library only (the 3D viewer loads three.js from a CDN).

Search, classification, source-pack parsing and texture lookup are ported from
[TheDimin/SyntyBrowser](https://github.com/TheDimin/SyntyBrowser) (MIT, s&box), extended to
`.unitypackage` files, characters, and measured sizes.

## Use

```bash
# 1. Index (any mix of .unitypackage files, folders of them, Synty source-pack folders, or path-list .txt)
python -m synty_explorer index ~/SyntyPackages -o index.json --cache cache

# 2. Browse  ->  http://127.0.0.1:7860
python -m synty_explorer serve -i index.json

# 3. Terminal search
python -m synty_explorer search "barel cat:prop pack:military"
```

Unity Asset Store downloads live in `%AppData%\Unity\Asset Store-5.x\Synty Studios\` (Windows)
or `~/Library/Unity/Asset Store-5.x/` (macOS).

Indexing reads each package three times (paths → prefabs/materials/previews → FBX/textures).
`--no-geometry` skips FBX sizes, `--no-cache` extracts nothing (names only).

## Docker

No Dockerfile needed: `docker-compose.yml` runs the stock `python:3.12-slim` image.

```bash
cp ~/SyntyPackages/*.unitypackage packages/     # or source-pack folders
docker compose run --rm index                   # -> data/index.json, data/cache/
docker compose up -d web                        # -> http://127.0.0.1:7860
```

Re-run `index` after adding packages, then `docker compose restart web`.
On Linux, run `export UID GID` first so extracted files belong to you.

MCP through Docker:

```json
{ "mcpServers": { "synty": {
  "command": "docker",
  "args": ["compose", "-f", "/abs/path/synty-explorer/docker-compose.yml", "run", "--rm", "-T", "mcp"] } } }
```

## MCP

```json
{ "mcpServers": { "synty": {
  "command": "python", "args": ["-m", "synty_explorer", "mcp", "-i", "/abs/path/index.json"],
  "cwd": "/abs/path/synty-explorer" } } }
```

Tools: `synty_catalog_status`, `synty_search`, `synty_inspect_asset`, `synty_compare_scale`.

## Query syntax

Fuzzy terms (typos OK) plus filters: `pack:city` `cat:character` `kind:skinned` `tag:harbor-city`.
Categories: prop, building, character, attachment, weapon, vehicle, environment, item, fx, generic.

## Animations

Animation packs (e.g. `ANIMATION_Idles`) are indexed too: every `A_*.fbx` clip becomes an asset
(`cat:animation`) with its duration, rig family (Polygon / Sidekick), body and phase (Enter / Loop / Exit).

* Open a clip to play it on the pack's sample mannequin, or pick any other compatible character.
* Open a rigged character to try every compatible clip on it.
* Compatibility = at least 90% of the clip's bone names exist on the model (computed at index time).

Re-run `index` after updating to pick up clips and mannequins.

## What gets indexed

* One asset per `.prefab` (packages) or per `MaterialList` prefab (source packs).
* Also FBX files no prefab uses (animation clips, sample mannequins).
* Skipped: collision folders/meshes, `UCX_*`, LOD files.
* Shared packs (e.g. `PolygonGeneric`, bundled in many packages) are de-duplicated.
* Sizes: FBX vertices × node transforms × `UnitScaleFactor`, in meters. Pivots and prefab
  overrides are ignored, so treat heights as close approximations.

## Layout

```
synty_explorer/
  naming.py      name -> category/kind/display name, exclusion rules
  catalog.py     .unitypackage / source pack / path-list scanners, dedupe, index
  fbx.py         binary FBX parser: UnitScaleFactor + world bounding box
  search.py      fuzzy search + curated tags (SyntyBrowser port)
  server.py      local HTTP server (127.0.0.1 only)
  mcp_server.py  MCP over stdio, no SDK needed
  web/index.html UI: filters, grid, 3D viewer (three.js), height lineup
tests/test_all.py  synthetic FBX + unitypackage fixtures; python -m unittest discover tests
```

## Next steps

* Verify heights against real packs (the FBX reader is only tested on synthetic files).
* Render thumbnails for source packs (they ship no `preview.png`).
* Per-slot textures for multi-material models (currently one texture per asset).
* Tag overrides / more curated tags (SyntyBrowser has `SyntyAssetTagOverrides`).

Synty content stays on your machine: the server binds to localhost and the index stores no
asset data. Synty is a trademark of Synty Studios; this project is not affiliated with them.
