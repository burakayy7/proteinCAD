#!/usr/bin/env python3
"""Runs one job, inside the container, with nothing else to reach.

The container has no network, no credentials and a read-only filesystem. All it
gets is /work: a spec on the way in, coordinates and a progress file on the way
out. Everything AWS happens on the other side of that directory, in
proteincad/sqs_worker.py.

    /work/spec.json        the job, written by the worker
    /work/progress.json    rewritten as the run goes, read by the heartbeat
    /work/designs/*.pdb    what came out
    /work/result.json      what happened

The model itself is colab_worker.py, unmodified -- the same file the Colab
notebook runs. This is an adapter around it, not a second implementation: it
builds the generator, hands it a job dict of exactly the shape the worker's own
HTTP handler builds, and writes the result out as files.

This container never downloads anything. The weights are published once by
deploy/aws/worker/publish-weights.py and fetched from S3 by the worker on the
host, which is the only process here with credentials or a network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/app")

import colab_worker  # noqa: E402


def write(path: Path, payload) -> None:
    """Write a small JSON file the reader may be looking at right now.

    Through a temporary file and a rename, because the heartbeat on the other
    side of this mount reads progress.json on its own schedule and a half
    written file is a crash there rather than a stale number.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(path)


class Arguments:
    """What colab_worker.build_generator expects, filled in for this image.

    The paths are the image's own; RFDIFFUSION_DIR and PROTEINMPNN_DIR are set
    in the Dockerfile and honoured by colab_worker's defaults, so they are read
    from the environment here rather than repeated.

    Both backbone engines are built, not one: the image carries the code for
    both and which of them a job wants is in the spec, not in how the container
    was started. An engine whose weights are not on the mount fails that job
    with a message naming them -- which is the right outcome, and a far better
    one than a container that would have run it refusing to because it was
    launched for the other engine.
    """

    generator = os.environ.get("PROTEINCAD_GENERATOR", "rfdiffusion,esm3")
    folder = os.environ.get("PROTEINCAD_FOLDER", "esmfold")
    python = sys.executable
    esmfold_model = os.environ.get("PROTEINCAD_ESMFOLD_MODEL", colab_worker.ESMFOLD_MODEL)
    esm3_model = os.environ.get("PROTEINCAD_ESM3_MODEL", colab_worker.ESM3_MODEL)
    rfdiffusion = colab_worker.DEFAULT_RFDIFFUSION
    proteinmpnn = colab_worker.DEFAULT_PROTEINMPNN
    extra: list = []
    no_binder_defaults = False


def check_weights(spec: dict) -> str:
    """Are the weights this job needs actually on the mount?

    Worth asking first. With no network the download the model would attempt
    fails a minute into the run, deep inside a loader, naming a URL -- which
    reads as a broken model rather than a box that was set up without one file.
    """
    if (spec.get("kind") or "binder") != "binder":
        return ""

    if colab_worker.engine_of(spec) == "esm3":
        # A cache rather than a file, so what is checked for is a snapshot with
        # something in it. The repo name is not checked against the spec's
        # chosen variant on purpose: a local path is a legitimate variant, and
        # one that is a path is checked by the loader itself.
        home = Path(os.environ.get("PROTEINCAD_ESM3_HOME", "/models/esm3"))
        if any((home / "hub").glob("models--*/snapshots/*/*")) or any(home.glob("*.pth")):
            return ""
        return (f"this job needs the ESM3 weights and there is no cache at {home}. "
                "They are not fetched by default, so a deployment can be complete without "
                "them: publish them with `deploy/aws/worker/build.sh weights esm3` and the "
                "machine fetches them for the job as it does the rest.")

    run = spec.get("run") or {}
    hotspots = (spec.get("target") or {}).get("hotspots") or []
    wanted = colab_worker.checkpoint_for(run, hotspots)
    models = Path(colab_worker.DEFAULT_RFDIFFUSION) / "models"
    if (models / wanted).is_file():
        return ""
    present = sorted(p.name for p in models.glob("*.pt")) if models.is_dir() else []
    return (f"this job needs the {wanted} checkpoint and it is not on the weights mount. "
            f"Present: {', '.join(present) or 'nothing'}. Run fetch-weights on the box.")


def run(spec_path: Path) -> int:
    work = spec_path.parent
    spec = json.loads(spec_path.read_text())
    designs_dir = work / "designs"
    designs_dir.mkdir(exist_ok=True)

    job = {
        "spec": spec,
        "status": "queued",
        "progress": 0,
        "stage": "queued",
        "total": int((spec.get("run") or {}).get("numDesigns", 1) or 1),
        "designs": [],
        "error": "",
        "cancel": False,
        "created": time.time(),
    }

    missing = check_weights(spec)
    if missing:
        write(work / "result.json", {"status": "failed", "error": missing, "designs": []})
        print(missing, file=sys.stderr)
        return 1

    # The run takes minutes and says what it is doing through the job dict.
    # Copying that out on a timer is what turns it into the progress line the
    # panel shows; the worker reads this file, never this process.
    stop = threading.Event()

    def report():
        while not stop.wait(5):
            write(work / "progress.json", {
                "stage": job.get("stage", ""),
                "progress": int(job.get("progress", 0) or 0),
                "command": job.get("log", ""),
            })

    watcher = threading.Thread(target=report, daemon=True)
    watcher.start()
    try:
        colab_worker.run_job(colab_worker.build_generator(Arguments()), job)
    finally:
        stop.set()
        watcher.join(timeout=2)

    designs = []
    for index, design in enumerate(job["designs"]):
        name = "design_%03d.pdb" % index
        (designs_dir / name).write_text(design.get("pdb") or "")
        designs.append({"name": design.get("name", "design_%d" % (index + 1)),
                        "file": "designs/" + name,
                        "metrics": design.get("metrics") or {}})

    write(work / "result.json", {
        "status": job["status"],
        "error": job["error"],
        "log": job.get("log", ""),
        "designs": designs,
    })
    write(work / "progress.json", {"stage": "", "progress": len(designs)})
    # Non-zero only when the run itself broke. "The model ran and produced
    # nothing" is a finished job with an explanation in result.json, and the
    # worker reads that; exiting non-zero here would hide it behind a status
    # code.
    return 0 if job["status"] in ("done", "cancelled", "failed") else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="run one proteinCAD job in this container")
    parser.add_argument("spec", nargs="?", default="/work/spec.json")
    args = parser.parse_args(argv)
    return run(Path(args.spec))


if __name__ == "__main__":
    raise SystemExit(main())
