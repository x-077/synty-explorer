"""Fuzzy search + curated tags. Ported from SyntyBrowser's SyntyAssetSearch.cs / SyntyAssetTags.cs.

Query syntax:
  plain terms         fuzzy match (prefix, transposition, subsequence, edit distance)
  tag:<id>            curated tag, e.g. tag:harbor-city
  pack:<name>         pack name contains
  cat:<category>      category equals (prop, building, character, weapon, vehicle, ...)
  kind:<kind>         static | skinned | fx
"""
from __future__ import annotations

import re

_TOKEN_SEP = re.compile(r"[^a-z0-9]+")

# ---------------------------------------------------------------- tags
TAGS = {
    "harbor-city": {
        "name": "Harbor City",
        "terms": {
            "dock", "docks", "harbor", "harbour", "pier", "wharf", "jetty", "quay", "marina",
            "shipyard", "boathouse", "lighthouse", "warehouse",
            "boat", "canoe", "dinghy", "gondola", "raft", "ship", "vessel", "anchor", "buoy",
            "mast", "oar", "paddle", "rudder", "sail", "net", "fishing", "fish", "lobster", "crab",
            "cargo", "barrel", "crate", "rope",
            "market", "stall", "merchant", "shop", "tavern", "inn", "city", "townhouse",
        },
        "exclude": {
            "airport", "airplane", "aircraft", "spaceship", "space", "scifi", "cyber", "futuristic",
            "apocalypse", "zombie", "submarine",
        },
    },
}


def _tok(value: str) -> list[str]:
    return [t for t in _TOKEN_SEP.split((value or "").lower()) if t]


def resolve_tags(asset: dict) -> list[str]:
    terms = set(_tok(f"{asset.get('name')} {asset.get('display_name')} {asset.get('group')}"))
    out = []
    for tag_id, rule in TAGS.items():
        if terms & rule["exclude"]:
            continue
        if terms & rule["terms"]:
            out.append(tag_id)
    return out


# ---------------------------------------------------------------- search
def _split_words(value: str) -> list[str]:
    return [w for w in re.split(r"[ _\-/\\.]+", (value or "").lower()) if w]


def _is_subsequence(needle: str, hay: str) -> bool:
    i = 0
    for ch in hay:
        if i < len(needle) and ch == needle[i]:
            i += 1
    return i == len(needle)


def _adjacent_transposition(word: str, term: str) -> bool:
    if len(word) != len(term):
        return False
    d = [i for i in range(len(word)) if word[i] != term[i]]
    return len(d) == 2 and d[1] == d[0] + 1 and word[d[0]] == term[d[1]] and word[d[1]] == term[d[0]]


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[len(b)]


def _word_score(word: str, term: str) -> int:
    if word.startswith(term):
        return 80
    if _adjacent_transposition(word, term):
        return 72
    if len(term) >= 3 and _is_subsequence(term, word):
        return 55 - min(20, len(word) - len(term))
    dist = _edit_distance(word, term)
    allowed = 2 if len(term) >= 6 else 1 if len(term) >= 4 else 0
    return 50 - dist * 8 if dist <= allowed else -1


def _searchable(asset: dict) -> str:
    return " ".join(
        str(x or "") for x in (
            asset.get("display_name"), asset.get("name"), asset.get("category"),
            asset.get("group"), asset.get("pack"), " ".join(asset.get("tags", [])),
        )
    ).lower()


def _score(asset: dict, terms: list[str]) -> int:
    text = _searchable(asset)
    words = _split_words(text)
    total = 0
    for term in terms:
        if term in text:
            total += 120 if text.startswith(term) else 90
            continue
        best = max((_word_score(w, term) for w in words), default=-1)
        if best < 0:
            return -1
        total += best
    return total


def parse_query(query: str) -> dict:
    q = {"terms": [], "tag": [], "pack": [], "cat": [], "kind": []}
    for token in (query or "").split():
        key, sep, val = token.partition(":")
        if sep and key.lower() in ("tag", "pack", "cat", "kind") and val:
            q[key.lower()].append(val.lower())
        else:
            q["terms"].extend(_split_words(token))
    return q


def _filters_ok(asset: dict, q: dict) -> bool:
    if any(t not in [x.lower() for x in asset.get("tags", [])] for t in q["tag"]):
        return False
    if any(p not in (asset.get("pack") or "").lower() for p in q["pack"]):
        return False
    if any(c != (asset.get("category") or "").lower() for c in q["cat"]):
        return False
    if any(k != (asset.get("kind") or "").lower() for k in q["kind"]):
        return False
    return True


def search(assets: list[dict], query: str, limit: int | None = None) -> list[dict]:
    q = parse_query(query)
    pool = [a for a in assets if _filters_ok(a, q)]
    if not q["terms"]:
        res = sorted(pool, key=lambda a: (a.get("pack") or "", a.get("display_name") or ""))
    else:
        scored = [(s, a) for a in pool if (s := _score(a, q["terms"])) >= 0]
        scored.sort(key=lambda x: (-x[0], (x[1].get("display_name") or "").lower()))
        res = [a for _, a in scored]
    return res[:limit] if limit else res
