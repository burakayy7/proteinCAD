"""Rotational scans: where they are kept, and how they are run.

A scan is minutes of CPU on one structure, and it is *deterministic* -- the same
assembly turned by the same steps and scored the same way gives the same curve
every time. Two things follow, and together they are most of this file.

It should never be run twice. The scan's name is a hash of everything that goes
into it, coordinates included, so asking for one that has already been computed
is a directory listing rather than a job. A panel can therefore offer "scan
this" without having to remember whether it already did.

And it should be possible to stop. A fine scan writes one line per angle as it
finishes it, so the work already done survives a cancel, a crash, or the server
being restarted -- resuming is just "which angles are not in the file yet". The
same lines are what the panel draws while the scan is still running, which is
why a partial curve appears rather than a spinner.

The work happens in `python3 -m proteincad.landscape`, as a child process. Not
because it has to be a different process to be correct, but because the thing
holding a whole assembly in memory for several minutes should not be the thing
answering the browser.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .landscape import (
    BACKENDS, LandscapeError, angle_list, backend_catalogue, descriptors,
    done_angles, request_digest, rise_list,
)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# A whole assembly as PDB text. 7CGO is 335k atoms and about 27 MB, and a scan
# of the whole thing is not a thing anybody wants -- the rotor and the axle are
# a handful of chains out of it, and the panel sends only those.
MAX_PDB_BYTES = 48 * 1024 * 1024


class ScanStore:
    """Every scan this deployment has, keyed by what was asked for.

    Held on the server's Context beside the job queue. Running children are
    tracked in memory; everything else is read off disk, so a scan that finished
    before the last restart is still a finished scan.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.children: dict[str, subprocess.Popen] = {}
        self.cancelled: set[str] = set()

    # ------------------------------------------------------------ submitting

    def submit(self, payload: dict) -> dict:
        """Start a scan, or hand back one that is already done or running.

        Returns the same shape `status` does, so the caller never has to tell
        "started" apart from "was already there" -- which is the point of
        naming a scan after its contents.
        """
        pdb = payload.get("pdb") or ""
        if not isinstance(pdb, str) or not pdb.strip():
            raise LandscapeError("no structure was sent to scan")
        if len(pdb.encode("utf-8", "ignore")) > MAX_PDB_BYTES:
            raise LandscapeError(
                f"that structure is over {MAX_PDB_BYTES // (1024 * 1024)} MB. Send the rotor "
                "and the axle rather than the whole assembly.")

        request = _clean(payload)
        # Validated here rather than in the child, so a bad step is a 400 on the
        # request that made it instead of a job that fails a second later.
        angles = angle_list(request["step"])
        rises = rise_list(request["rise"])

        scan_id = request_digest(request, pdb)
        directory = self.dir / scan_id
        state = self.status(scan_id)
        if state["status"] in (RUNNING, DONE):
            return state

        directory.mkdir(parents=True, exist_ok=True)
        (directory / "request.json").write_text(json.dumps(request, indent=2))
        if not (directory / "assembly.pdb").is_file():
            (directory / "assembly.pdb").write_text(pdb)
        for leftover in ("error.txt", "finished"):
            (directory / leftover).unlink(missing_ok=True)
        self.cancelled.discard(scan_id)

        self._spawn(scan_id, directory)
        state = self.status(scan_id)
        state["total"] = len(angles) * len(rises)
        return state

    def _spawn(self, scan_id: str, directory: Path) -> None:
        # The same interpreter that is running the server, so a scan cannot end
        # up on a Python without this package importable.
        command = [sys.executable, "-m", "proteincad.landscape", str(directory)]
        log = (directory / "log.txt").open("ab")
        try:
            child = subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True,
            )
        except OSError as error:
            log.close()
            raise LandscapeError(f"could not start the scan process: {error}") from error
        self.children[scan_id] = child

    # -------------------------------------------------------------- reporting

    def status(self, scan_id: str, points: bool = False) -> dict:
        """What this scan is doing, and the curve so far if asked for.

        `points=False` is the polling shape: enough to draw a progress bar
        without re-sending a curve the panel already has.
        """
        directory = self.dir / scan_id
        if not (directory / "request.json").is_file():
            return {"id": scan_id, "status": "unknown", "exists": False}

        request = json.loads((directory / "request.json").read_text())
        rows = done_angles(directory / "points.ndjson")
        axis = _read_json(directory / "axis.json")
        total = (axis or {}).get("total") or 0
        if not total:
            try:
                total = len(angle_list(request["step"])) * len(rise_list(request.get("rise")))
            except LandscapeError:
                total = 0

        error = ""
        if (directory / "error.txt").is_file():
            error = (directory / "error.txt").read_text().strip()

        child = self.children.get(scan_id)
        alive = child is not None and child.poll() is None
        finished = (directory / "finished").is_file()

        if error:
            state = FAILED
        elif finished or (total and len(rows) >= total and not alive):
            state = DONE
        elif alive:
            state = RUNNING
        elif scan_id in self.cancelled:
            state = CANCELLED
        elif child is not None:
            # The child is gone, nothing marked it finished and nothing wrote a
            # reason. Killed from outside, or out of memory.
            state = FAILED
            error = "the scan process stopped without finishing; see log.txt in the scan folder"
        else:
            # No child in this process: either never started here, or started
            # before a restart. Partial work on disk is resumable, not failed.
            state = QUEUED if not rows else CANCELLED

        reply = {
            "id": scan_id,
            "exists": True,
            "status": state,
            "progress": len(rows),
            "total": total,
            "error": error,
            "request": {k: v for k, v in request.items() if k != "pdb"},
            "axis": axis,
            "started": _mtime(directory / "request.json"),
            "elapsed": round(
                (_mtime(directory / "finished") or time.time()) - (_mtime(directory / "request.json") or time.time()), 1),
        }
        if state == DONE or points:
            expected = (axis or {}).get("expected_period") or 0.0
            reply["points"] = sorted(rows, key=lambda row: (row["angle"], row.get("rise", 0.0)))
            reply["descriptors"] = descriptors(rows, expected) if rows else {"minima": [],
                                                                             "complete": False}
            reply["backend"] = _backend_detail(request.get("backend"))
        return reply

    def cancel(self, scan_id: str) -> dict:
        """Stop a running scan. What it has computed stays on disk."""
        child = self.children.get(scan_id)
        if child is not None and child.poll() is None:
            self.cancelled.add(scan_id)
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
        return self.status(scan_id)

    def list(self, limit: int = 30) -> list[dict]:
        """Finished and part-finished scans, newest first."""
        folders = [p for p in self.dir.iterdir()
                   if p.is_dir() and (p / "request.json").is_file()]
        folders.sort(key=lambda p: _mtime(p / "request.json") or 0, reverse=True)
        return [self.status(p.name) for p in folders[:limit]]


def _clean(payload: dict) -> dict:
    """The request, with everything the hash depends on normalised.

    Chain ids are sorted and the step is rounded before hashing, so the same
    question asked with the chains in a different order is the same scan rather
    than a second one that computes the same curve.
    """
    rotor = _chains(payload.get("rotor"), "rotor")
    axle = _chains(payload.get("axle"), "axle")
    overlap = sorted(set(rotor) & set(axle))
    if overlap:
        raise LandscapeError(f"chain {', '.join(overlap)} is in both the rotor and the axle")

    backend = str(payload.get("backend") or "geometric")
    if backend not in BACKENDS:
        raise LandscapeError(f"no scoring backend called {backend!r}; "
                             f"have {', '.join(sorted(BACKENDS))}")
    ready, why = BACKENDS[backend]().available()
    if not ready:
        raise LandscapeError(why)

    try:
        step = round(float(payload.get("step") or 5.0), 6)
    except (TypeError, ValueError) as error:
        raise LandscapeError("the step has to be a number of degrees") from error

    rise = payload.get("rise") or None
    if rise:
        rise = {k: float(rise[k]) for k in ("min", "max", "step") if k in rise}

    request = {
        "name": str(payload.get("name") or "assembly")[:80],
        "rotor": sorted(rotor),
        "axle": sorted(axle),
        "step": step,
        "rise": rise,
        "backend": backend,
    }
    axis = payload.get("axis")
    if axis and axis.get("direction"):
        request["axis"] = {"direction": [float(v) for v in axis["direction"]][:3],
                           "point": [float(v) for v in (axis.get("point") or (0, 0, 0))][:3]}
    return request


def _chains(value, what: str) -> list[str]:
    if isinstance(value, str):
        value = [c for c in value.replace(",", " ").split() if c]
    chains = [str(c).strip() for c in (value or []) if str(c).strip()]
    if not chains:
        raise LandscapeError(f"no {what} chains were given")
    return chains


def _backend_detail(name) -> dict:
    for entry in backend_catalogue():
        if entry["id"] == name:
            return entry
    return {"id": name, "label": "score", "unit": "", "available": False, "why": "unknown backend"}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _mtime(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None
