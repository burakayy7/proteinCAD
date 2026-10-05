"""Fetch structures by PDB id, with a local cache.

Lookup order: the bundled samples, then the cache, then the RCSB. Anything
downloaded is written to the cache so a second visit works offline.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

RCSB_URL = "https://files.rcsb.org/download/{id}.{ext}"
TIMEOUT = 30
ID_PATTERN = re.compile(r"^[A-Za-z0-9]{4,12}$")


class FetchError(Exception):
    pass


def local_path(pdb_id: str, *directories: Path) -> Path | None:
    """Find an already-present copy of a structure, in any supported format."""
    stem = pdb_id.lower()
    for directory in directories:
        if not directory.is_dir():
            continue
        for extension in ("cif", "pdb", "ent", "mmcif"):
            for candidate in (directory / f"{stem}.{extension}", directory / f"{pdb_id.upper()}.{extension}"):
                if candidate.is_file():
                    return candidate
    return None


def fetch(pdb_id: str, cache_dir: Path, *extra_dirs: Path) -> tuple[str, str]:
    """Return (text, filename) for a PDB id.

    Raises FetchError when the id is malformed or the download fails.
    """
    if not ID_PATTERN.match(pdb_id):
        raise FetchError(f"{pdb_id!r} does not look like a PDB id")

    found = local_path(pdb_id, *extra_dirs, cache_dir)
    if found:
        return found.read_text(errors="replace"), found.name

    cache_dir.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for extension in ("cif", "pdb"):
        url = RCSB_URL.format(id=pdb_id.upper(), ext=extension)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "proteinCAD/0.1"})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code == 404:
                continue
            raise FetchError(f"RCSB returned {error.code} for {pdb_id}") from error
        except Exception as error:  # network down, DNS, timeout
            raise FetchError(f"could not reach the RCSB: {error}") from error

        filename = f"{pdb_id.lower()}.{extension}"
        (cache_dir / filename).write_text(text)
        return text, filename

    raise FetchError(f"no structure found for {pdb_id} ({last_error})")
