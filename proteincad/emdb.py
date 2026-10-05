"""Fetch EMDB density maps by id, with a local cache.

The same job `rcsb.py` does for coordinates, and it matters more here: a map is
tens of megabytes where a structure is a few, so asking the EBI twice for the
same one is the difference between a reload that takes a moment and one that
takes a minute.

The bytes are passed through untouched -- still gzipped, exactly as EBI serves
them. The browser has a CCP4 reader already, because a static copy of `web/`
has to be able to read a map with no server behind it at all, and a second
parser on this side would be a second place for the axis-order handling to be
wrong. So this is a cache, not a converter.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

MAP_URL = "https://ftp.ebi.ac.uk/pub/databases/emdb/structures/EMD-{id}/map/emd_{id}.map.gz"
ENTRY_URL = "https://www.ebi.ac.uk/emdb/api/entry/EMD-{id}"
TIMEOUT = 120
# A map is big and the connection is not always fast; read it in pieces so a
# slow download is not one enormous allocation at the end.
CHUNK = 1 << 20
# EMD-25576 is 244 MB raw and 5 MB gzipped. The cap is on what arrives, which is
# the gzipped size, and is here so a mistyped id cannot fill a disk.
MAX_BYTES = 512 * 1024 * 1024

ID_PATTERN = re.compile(r"^(?:EMD[-_]?)?(\d{3,6})$", re.IGNORECASE)


class EmdbError(Exception):
    pass


def normalise(emdb_id: str) -> str:
    """"emd_25575", "EMD-25575", "25575" -> "EMD-25575"."""
    match = ID_PATTERN.match(str(emdb_id).strip())
    if not match:
        raise EmdbError(f"{emdb_id!r} does not look like an EMDB id (try EMD-25575)")
    return f"EMD-{match.group(1)}"


def cache_paths(emdb_id: str, cache_dir: Path) -> tuple[Path, Path]:
    number = normalise(emdb_id).split("-")[1]
    return cache_dir / f"emd_{number}.map.gz", cache_dir / f"emd_{number}.json"


def fetch_map(emdb_id: str, cache_dir: Path) -> tuple[bytes, str]:
    """Return (gzipped map bytes, filename), from the cache or from the EBI."""
    identifier = normalise(emdb_id)
    number = identifier.split("-")[1]
    path, _ = cache_paths(identifier, cache_dir)
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes(), path.name

    cache_dir.mkdir(parents=True, exist_ok=True)
    url = MAP_URL.format(id=number)
    request = urllib.request.Request(url, headers={"User-Agent": "proteinCAD/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_BYTES:
                raise EmdbError(
                    f"{identifier} is {declared // (1024 * 1024)} MB, over the "
                    f"{MAX_BYTES // (1024 * 1024)} MB this server will cache")
            pieces = []
            total = 0
            while True:
                piece = response.read(CHUNK)
                if not piece:
                    break
                total += len(piece)
                if total > MAX_BYTES:
                    raise EmdbError(f"{identifier} is larger than this server will cache")
                pieces.append(piece)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise EmdbError(f"EMDB has no entry {identifier}") from error
        raise EmdbError(f"EBI returned {error.code} for {identifier}") from error
    except EmdbError:
        raise
    except Exception as error:  # DNS, timeout, connection reset
        raise EmdbError(f"could not reach the EMDB at EBI: {error}") from error

    data = b"".join(pieces)
    # Written whole and moved into place, so a download interrupted halfway does
    # not leave a truncated map in the cache that every later visit then reads.
    temporary = path.with_suffix(".part")
    temporary.write_bytes(data)
    temporary.replace(path)
    return data, path.name


def fetch_meta(emdb_id: str, cache_dir: Path) -> dict:
    """The entry document: title, recommended contour level, fitted models.

    Best effort. A map with no metadata still renders -- it contours at a few
    sigma instead of at the level the depositors chose -- so this never raises
    for a network problem, only for an id that is not one.
    """
    identifier = normalise(emdb_id)
    number = identifier.split("-")[1]
    _, meta_path = cache_paths(identifier, cache_dir)
    if meta_path.is_file():
        try:
            return json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass

    url = ENTRY_URL.format(id=number)
    request = urllib.request.Request(url, headers={"User-Agent": "proteinCAD/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return {"id": identifier, "title": "", "contour": None, "fitted": []}

    meta = summarise(payload)
    meta["id"] = identifier
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        meta_path.write_text(json.dumps(meta, indent=2))
    except OSError:
        pass
    return meta


def summarise(payload: dict) -> dict:
    """The two things worth having out of EBI's entry document.

    Mirrors `readEbiEntry` in web/src/io/emdb.js, because a static deployment
    asks the EBI directly and gets the same document with no server in the way.
    """
    body = payload.get("map") or {}
    contours = ((body.get("contour_list") or {}).get("contour")) or []
    primary = next((c for c in contours if c.get("primary")), None) or (contours[0] if contours else None)
    level = None
    if primary is not None:
        try:
            level = float(primary.get("level"))
        except (TypeError, ValueError):
            level = None

    references = ((payload.get("crossreferences") or {}).get("pdb_list") or {}).get("pdb_reference") or []
    return {
        "title": (payload.get("admin") or {}).get("title", ""),
        "contour": level,
        "fitted": [r.get("pdb_id") for r in references if r.get("pdb_id")],
        "resolution": _resolution(payload),
    }


def _resolution(payload: dict):
    """The reported resolution, wherever in the document it is this time.

    It lives under structure_determination -> image_processing ->
    final_reconstruction -> resolution -> valueOf_, which is four levels of
    nesting through two lists, and entries do not all agree on the depth. So
    this looks for the key rather than pathing to it, breadth first, so the
    shallowest match wins and the answer does not depend on dict ordering.
    """
    queue = [payload]
    visited = 0
    while queue and visited < 10000:
        node = queue.pop(0)
        visited += 1
        if isinstance(node, dict):
            value = node.get("resolution")
            if isinstance(value, dict):
                value = value.get("valueOf_")
            if isinstance(value, (int, float, str)):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None
