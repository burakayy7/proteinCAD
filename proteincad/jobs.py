"""A small job queue for work that takes minutes rather than milliseconds.

Design runs cannot be request/response: a GPU takes anywhere from seconds to an
hour, and the browser has to be able to close and come back. So the server owns
a queue, the browser polls it, and results are written to disk under the data
directory rather than held in memory.

One worker thread by default, because a GPU is a single resource -- raise
`workers` only if the runner really is parallel.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"


class Job:
    def __init__(self, job_id: int, spec: dict, directory: Path):
        self.id = job_id
        self.spec = spec
        self.dir = directory
        self.status = QUEUED
        self.progress = 0
        # Free-text detail from the runner: which design, which diffusion step.
        # Progress alone is too coarse -- a run can be minutes into its first
        # design with nothing to show, which reads as a hang.
        self.stage = ""
        # The command the runner actually built, when it reports one. With the
        # whole of RFdiffusion's configuration now settable, this is the only
        # way to tell a setting that was applied from one the endpoint was too
        # old to have heard of.
        self.command = ""
        self.total = int(spec.get("run", {}).get("numDesigns", 1) or 1)
        self.designs: list[dict] = []
        self.error = ""
        self.created = time.time()
        self.started = None
        self.finished = None
        self.cancel_requested = False

    @property
    def model(self) -> str:
        return self.spec.get("model", "?")

    @property
    def kind(self) -> str:
        """Which stage this job is: a backbone, or the sequence and fold check
        that turns one into a protein."""
        return self.spec.get("kind") or "binder"

    @property
    def cancelled(self) -> bool:
        return self.cancel_requested

    def add_design(self, name: str, pdb: str, metrics: dict | None = None) -> dict:
        """Store one result. The coordinates go to disk; only metadata stays.

        A result with no coordinates is refused rather than stored. Writing it
        would turn a runner that produced nothing into a job that reads as
        finished, and the first sign of trouble would be an empty file failing
        to open some minutes later -- which looks like a bug in the viewer.
        """
        from .design import count_atoms
        atoms = count_atoms(pdb)
        if not atoms:
            raise ValueError(
                f"{name} came back with no atoms, so it is not a design. This usually "
                "means the compute endpoint is not running the model it was asked for.")
        self.dir.mkdir(parents=True, exist_ok=True)
        index = len(self.designs)
        path = self.dir / f"design_{index:03d}.pdb"
        path.write_text(pdb)
        record = {"name": name, "file": path.name, "metrics": metrics or {}, "atoms": atoms}
        self.designs.append(record)
        self.progress = len(self.designs)
        return record

    def design_path(self, index: int) -> Path | None:
        if index < 0 or index >= len(self.designs):
            return None
        return self.dir / self.designs[index]["file"]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "model": self.model,
            "kind": self.kind,
            "mode": self.spec.get("mode") or ("fold" if self.kind == "fold" else "binder"),
            "source": self.spec.get("source"),
            "progress": self.progress,
            "stage": self.stage,
            "command": self.command,
            "total": self.total,
            "error": self.error,
            "created": self.created,
            "elapsed": round((self.finished or time.time()) - (self.started or self.created), 1),
            "target": self.spec.get("target", {}).get("name", ""),
            "hotspots": self.spec.get("target", {}).get("hotspots", []),
            "designs": [
                {"name": d["name"], "metrics": d["metrics"], "atoms": d["atoms"]}
                for d in self.designs
            ],
        }


class JobQueue:
    def __init__(self, runners: dict, jobs_dir: Path, max_designs: int = 32, workers: int = 1):
        self.runners = runners
        self.jobs_dir = Path(jobs_dir)
        self.max_designs = max_designs
        self.jobs: "OrderedDict[int, Job]" = OrderedDict()
        self.pending: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.next_id = 1
        self.threads = []
        self.worker_count = max(1, workers)

    def runner_names(self) -> list:
        return sorted(self.runners)

    def submit(self, spec: dict) -> Job:
        model = spec.get("model", "mock")
        if model not in self.runners:
            raise ValueError(f"no runner called {model!r}; available: {', '.join(self.runner_names())}")

        requested = int(spec.get("run", {}).get("numDesigns", 1) or 1)
        spec.setdefault("run", {})["numDesigns"] = max(1, min(requested, self.max_designs))

        with self.lock:
            job_id = self.next_id
            self.next_id += 1
            job = Job(job_id, spec, self.jobs_dir / f"job_{job_id:04d}")
            self.jobs[job_id] = job
        self.pending.put(job_id)
        self._ensure_workers()
        return job

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)

    def list(self, limit: int = 40) -> list:
        return list(self.jobs.values())[-limit:][::-1]

    def cancel(self, job_id: int) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status in (DONE, FAILED, CANCELLED):
            return False
        job.cancel_requested = True
        if job.status == QUEUED:
            job.status = CANCELLED
            job.finished = time.time()
        return True

    def _ensure_workers(self) -> None:
        with self.lock:
            self.threads = [t for t in self.threads if t.is_alive()]
            while len(self.threads) < self.worker_count:
                thread = threading.Thread(target=self._work, daemon=True, name="proteincad-worker")
                thread.start()
                self.threads.append(thread)

    def _work(self) -> None:
        while True:
            try:
                job_id = self.pending.get(timeout=30)
            except queue.Empty:
                return
            job = self.jobs.get(job_id)
            if job is None or job.cancel_requested:
                if job:
                    job.status = CANCELLED
                    job.finished = time.time()
                continue

            job.status = RUNNING
            job.started = time.time()
            try:
                runner = self.runners[job.model]
                runner.run(job.spec, job)
                if job.cancel_requested:
                    job.status = CANCELLED
                else:
                    job.status = DONE if job.designs else FAILED
                    if not job.designs:
                        job.error = job.error or "the runner returned no designs"
            except Exception as error:
                job.status = FAILED
                from .design import RunnerError
                if isinstance(error, RunnerError):
                    # The model or the endpoint failed, not us. The message is
                    # already the useful part; a traceback would only hide it.
                    job.error = str(error)
                    print(f"[job {job.id}] failed: {str(error).splitlines()[0]}")
                else:
                    job.error = f"{type(error).__name__}: {error}"
                    traceback.print_exc()
            finally:
                job.finished = time.time()
