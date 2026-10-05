"""HTTP routes.

Add an endpoint by writing a function and decorating it:

    @route("POST", "/api/fold")
    def fold(request):
        sequence = request.json["sequence"]
        return {"pdb": my_model.predict(sequence)}

A handler receives a Request and returns either a JSON-serialisable object or a
Response for full control (text bodies, custom headers). Path parameters are
written as {name} and arrive in `request.params`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .analysis import analyse_structure, contacts, geometry
from .design import build_fold_spec, build_runners, design_options, gpu_machine, validate_spec
from .ec2 import Ec2Error
from .emdb import EmdbError, fetch_map, fetch_meta, normalise
from .landscape import LandscapeError, backend_catalogue
from .rcsb import FetchError, fetch
from .structure import parse


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    context: Any = None  # the server's Context: .config and .jobs

    @property
    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ApiError(f"invalid JSON body: {error}", 400) from error


@dataclass
class Response:
    body: bytes
    status: int = 200
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


Handler = Callable[[Request], Any]


class Router:
    """A routing table.

    There is more than one front end now. The local server serves everything
    under /api/ from the table below; the Lambda front end in cloud_api.py
    serves a smaller, authenticated set of its own. Each owns a Router, so
    importing one cannot register routes into the other -- which, with a single
    module-level list, it silently would.
    """

    def __init__(self, name: str = "api"):
        self.name = name
        self.routes: list[tuple[str, re.Pattern, Handler]] = []

    def route(self, method: str, pattern: str):
        """Register a handler. `pattern` may contain {name} path parameters."""
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

        def decorator(function: Handler) -> Handler:
            self.routes.append((method.upper(), regex, function))
            return function

        return decorator

    def dispatch(self, request: Request) -> Response:
        """Match a request against this table and run it."""
        matched_path = False
        for method, regex, handler in self.routes:
            match = regex.match(request.path)
            if not match:
                continue
            matched_path = True
            if method != request.method:
                continue
            request.params = match.groupdict()
            result = handler(request)
            if isinstance(result, Response):
                return result
            return Response(json.dumps(result).encode("utf-8"))

        status = 405 if matched_path else 404
        raise ApiError(f"no route for {request.method} {request.path}", status)


# The local server's table. `route` and `dispatch` below are its own methods, so
# every existing @route("GET", "/api/...") keeps working unchanged.
DEFAULT = Router("local")
ROUTES = DEFAULT.routes

# What this process actually loaded, against what is on disk now.
#
# The web files are read from disk on every request, so reloading the browser
# picks up a front-end change at once. Python is imported once and never
# reloads, so a change on this side leaves the old behaviour running behind the
# new interface -- which reads as the fix not working rather than as a process
# that needs restarting. Answering the question in /health is cheaper than
# remembering to ask it.
SOURCE = Path(__file__).resolve().parent


def _newest_source() -> float:
    try:
        return max((p.stat().st_mtime for p in SOURCE.glob("*.py")), default=0.0)
    except OSError:
        return 0.0


LOADED = _newest_source()


def stale() -> bool:
    """Has the code on disk moved on since this process imported it?"""
    # A second of slack: a file written while the process was starting is not a
    # change the process missed.
    return _newest_source() > LOADED + 1.0


route = DEFAULT.route
dispatch = DEFAULT.dispatch


# --------------------------------------------------------------------- routes


@route("GET", "/api/health")
def health(request: Request) -> dict:
    config = request.context.config
    cache = config.cache_dir
    cached = sorted(p.name for p in cache.glob("*.*")) if cache.is_dir() else []
    return {
        "status": "ok",
        "version": __version__,
        # True when the files on disk have moved on since this process started.
        # The front end reloads itself; this does not.
        "stale": stale(),
        "cached_structures": cached[:50],
        "cache_count": len(cached),
        "runners": request.context.jobs.runner_names(),
        **config.public(),
    }


@route("GET", "/api/structure/{pdb_id}")
def structure(request: Request) -> Response:
    """Serve a structure by id: local copy first, then the RCSB."""
    config = request.context.config
    pdb_id = request.params["pdb_id"]
    try:
        text, filename = fetch(pdb_id, config.cache_dir, *config.structure_dirs)
    except FetchError as error:
        raise ApiError(str(error), 404) from error
    return Response(
        text.encode("utf-8"),
        content_type="chemical/x-pdb" if filename.endswith((".pdb", ".ent")) else "chemical/x-cif",
        headers={"X-Structure-Filename": filename},
    )


@route("GET", "/api/structure/{pdb_id}/summary")
def structure_summary(request: Request) -> dict:
    """Parse a structure on the server and report what is in it."""
    config = request.context.config
    pdb_id = request.params["pdb_id"]
    try:
        text, filename = fetch(pdb_id, config.cache_dir, *config.structure_dirs)
    except FetchError as error:
        raise ApiError(str(error), 404) from error
    return analyse_structure(parse(text, name=filename))


@route("GET", "/api/map/{emdb_id}")
def emdb_map(request: Request) -> Response:
    """Serve an EMDB density map: the local copy first, then the EBI.

    The bytes go out still gzipped, exactly as they arrived. The browser reads
    CCP4 already -- it has to, so a static copy of the viewer can open a map
    with no server behind it -- and a second reader here would be a second place
    for the axis-order handling to be wrong.
    """
    config = request.context.config
    try:
        data, filename = fetch_map(request.params["emdb_id"], config.cache_dir)
    except EmdbError as error:
        raise ApiError(str(error), 404) from error
    return Response(
        data,
        content_type="application/gzip",
        # A map is tens of megabytes and an EMDB entry never changes once it is
        # released, so re-fetching it on every reload is pure waste.
        headers={"X-Map-Filename": filename, "Cache-Control": "public, max-age=86400"},
    )


@route("GET", "/api/map/{emdb_id}/meta")
def emdb_meta(request: Request) -> dict:
    """Title, recommended contour level and any fitted models.

    The contour level is the one worth having: a map shown at the wrong level is
    either a solid block or nothing at all, and this is the level the depositors
    chose to look at it at.
    """
    config = request.context.config
    try:
        identifier = normalise(request.params["emdb_id"])
    except EmdbError as error:
        raise ApiError(str(error), 400) from error
    return fetch_meta(identifier, config.cache_dir)


@route("POST", "/api/analyze")
def analyze(request: Request) -> dict:
    """Measure a set of atoms sent by the viewer.

    Body: {"elements": ["C", "N", ...], "coords": [x, y, z, x, y, z, ...]}
    or    {"pdb": "<file contents>"}

    This is the round trip a model will use: the browser posts a selection,
    the server answers with numbers (or, later, new coordinates).
    """
    payload = request.json
    if "pdb" in payload:
        parsed = parse(payload["pdb"], name=payload.get("name", "posted"))
        return analyse_structure(parsed)

    flat = payload.get("coords") or []
    if len(flat) % 3 != 0:
        raise ApiError("coords must be a flat list of x, y, z triples")
    coords = [(flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat), 3)]
    elements = payload.get("elements") or []
    result = geometry(elements, coords)
    result["name"] = payload.get("name", "selection")

    groups = payload.get("groups")
    if groups and len(groups) == len(coords):
        result["contacts"] = contacts(coords, groups, float(payload.get("cutoff", 4.0)))
    return result


# ---------------------------------------------------------------- design jobs


@route("POST", "/api/design")
def design(request: Request) -> dict:
    """Queue a design job built by the viewer.

    The body is the spec from web/src/design/spec.js: a cropped target, the
    hotspot residues to build against, a length range and how many designs to
    make. The response is the job, which the browser then polls.
    """
    try:
        spec = validate_spec(request.json)
    except ValueError as error:
        raise ApiError(str(error), 400) from error
    try:
        job = request.context.jobs.submit(spec)
    except ValueError as error:
        raise ApiError(str(error), 400) from error
    return job.to_dict()


@route("GET", "/api/design/options")
def design_option_catalogue(request: Request) -> dict:
    """Every protocol and setting RFdiffusion can be asked for.

    The panel builds itself from this rather than hardcoding a control per
    setting, so the table in colab_worker.py -- which is also what the command
    line is built from -- is the only place a capability is written down.
    """
    return design_options()


@route("POST", "/api/compute")
def compute(request: Request) -> dict:
    """Point this server at a different GPU endpoint, without restarting it.

    A Colab quick tunnel gets a new address and a new token every session, and
    until now the only way to follow it was to stop the server and start it
    again -- losing the job list, and retyping a command line for what is one
    line of configuration. Restarting a Colab session to free a GPU should not
    cost the app as well.

    Allowed by default only when the server is bound to loopback: on a shared
    address this is a way to make somebody else's server talk to yours.
    """
    config = request.context.config
    if not config.allow_remote_config:
        raise ApiError(
            "this server will not take a compute endpoint over the API. It is bound to "
            f"{config.host}, not loopback; start it with PROTEINCAD_ALLOW_REMOTE_CONFIG=1 "
            "if that is what you want.", 403)

    payload = request.json
    url = str(payload.get("url", "")).strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise ApiError("the compute url has to start with http:// or https://")
    config.compute_url = url
    config.compute_token = str(payload.get("token", "")).strip()
    # Rebuilt rather than mutated: build_runners is the one place that decides
    # what a configuration can run, and it stays that way.
    request.context.jobs.runners = build_runners(config)
    return {"runners": request.context.jobs.runner_names(), **config.public()}


# ------------------------------------------------------------------- the gpu


@route("GET", "/api/gpu")
def gpu(request: Request) -> dict:
    """Where the on-demand GPU is, and when it turns itself off.

    Answers `{"configured": false}` rather than 404 when this deployment has no
    instance of its own, so the panel can ask once and hide the section without
    having to tell a missing feature apart from a missing server.
    """
    machine = gpu_machine(request.context.jobs.runners)
    if machine is None:
        return {"configured": False}
    return machine.status()


@route("POST", "/api/gpu/start")
def gpu_start(request: Request) -> dict:
    """Warm it up before there is a job for it.

    Waking takes a minute or two, and that minute is better spent while a
    design is still being set up than after Run has been pressed. Returns at
    once and leaves the panel to poll; the start holds the instance up for a
    full idle window, so pressing this buys time to work in rather than a box
    that stops again while you think.
    """
    machine = _machine(request)
    machine.start_soon()
    return machine.status()


@route("POST", "/api/gpu/stop")
def gpu_stop(request: Request) -> dict:
    """Stop paying for it now, rather than at the end of the idle window."""
    machine = _machine(request)
    if machine.leases:
        raise ApiError("a job is using the GPU right now; cancel it first", 409)
    try:
        machine.stop("asked from the panel")
    except Ec2Error as error:
        raise ApiError(str(error), 502) from error
    return machine.status()


def _machine(request: Request):
    machine = gpu_machine(request.context.jobs.runners)
    if machine is None:
        raise ApiError(
            "this server has no GPU instance of its own. Set PROTEINCAD_EC2_INSTANCE to one "
            "and restart it -- see deploy/aws/README.md.", 404)
    return machine


@route("GET", "/api/jobs")
def jobs(request: Request) -> dict:
    return {"jobs": [job.to_dict() for job in request.context.jobs.list()]}


@route("GET", "/api/jobs/{job_id}")
def job_status(request: Request) -> dict:
    job = _job(request)
    return job.to_dict()


@route("POST", "/api/jobs/{job_id}/cancel")
def job_cancel(request: Request) -> dict:
    job = _job(request)
    request.context.jobs.cancel(job.id)
    return job.to_dict()


@route("GET", "/api/jobs/{job_id}/designs/{index}")
def job_design(request: Request) -> Response:
    """One design, as a PDB file ready to load straight into the viewer."""
    job = _job(request)
    try:
        index = int(request.params["index"])
    except ValueError as error:
        raise ApiError("design index must be a number", 400) from error

    path = job.design_path(index)
    if path is None or not path.is_file():
        raise ApiError(f"job {job.id} has no design {index}", 404)
    return Response(
        path.read_bytes(),
        content_type="chemical/x-pdb",
        headers={"X-Design-Name": job.designs[index]["name"]},
    )


@route("POST", "/api/jobs/{job_id}/designs/{index}/fold")
def job_fold(request: Request) -> dict:
    """Take one finished backbone on to the next stage.

    The browser sends only which design it means and how many sequences it
    wants; the spec is built here from the target and the backbone already on
    disk, so the sequence is designed against exactly what the model saw.
    """
    job = _job(request)
    try:
        index = int(request.params["index"])
    except ValueError as error:
        raise ApiError("design index must be a number", 400) from error
    try:
        spec = validate_spec(build_fold_spec(job, index, request.json))
        return request.context.jobs.submit(spec).to_dict()
    except ValueError as error:
        raise ApiError(str(error), 400) from error


def _job(request: Request):
    try:
        job_id = int(request.params["job_id"])
    except ValueError as error:
        raise ApiError("job id must be a number", 400) from error
    job = request.context.jobs.get(job_id)
    if job is None:
        raise ApiError(f"no job {job_id}", 404)
    return job


# ------------------------------------------------------- rotational landscape


@route("POST", "/api/landscape")
def landscape_scan(request: Request) -> dict:
    """Scan a two-component assembly through a turn about its shared axis.

    Body: the assembly as PDB text, which chains are the rotor and which are
    the axle, the step in degrees and the scoring backend. The reply is the
    scan -- which may already be finished, because a scan is named after its
    own contents and an identical request is answered from disk rather than
    computed again.
    """
    try:
        return request.context.scans.submit(request.json)
    except LandscapeError as error:
        raise ApiError(str(error), 400) from error


@route("GET", "/api/landscape/options")
def landscape_options(request: Request) -> dict:
    """What this build can score a landscape with, and why not, where it cannot.

    Asked once, so the panel can show the Rosetta option greyed out with the
    reason on it rather than offering a backend that fails when pressed.
    """
    return {"backends": backend_catalogue()}


@route("GET", "/api/landscape/{scan_id}")
def landscape_status(request: Request) -> dict:
    """One scan: progress while it runs, the curve and the descriptors when done.

    `?points=1` asks for the curve mid-scan, which is what draws a partial
    landscape instead of a progress bar.
    """
    scan_id = _scan_id(request)
    wants_points = request.query.get("points") in ("1", "true", "yes")
    state = request.context.scans.status(scan_id, points=wants_points)
    if not state.get("exists"):
        raise ApiError(f"no scan {scan_id}", 404)
    return state


@route("POST", "/api/landscape/{scan_id}/cancel")
def landscape_cancel(request: Request) -> dict:
    """Stop a scan. The angles already computed stay, and resubmitting resumes."""
    scan_id = _scan_id(request)
    state = request.context.scans.cancel(scan_id)
    if not state.get("exists"):
        raise ApiError(f"no scan {scan_id}", 404)
    return state


@route("GET", "/api/landscapes")
def landscape_list(request: Request) -> dict:
    return {"scans": request.context.scans.list()}


def _scan_id(request: Request) -> str:
    """A scan id is a hex digest, and nothing else gets to name a directory."""
    scan_id = request.params["scan_id"]
    if not re.fullmatch(r"[0-9a-f]{8,32}", scan_id):
        raise ApiError("a scan id is a hex digest", 400)
    return scan_id


@route("POST", "/api/session")
def session(request: Request) -> dict:
    """Accept the viewer's scene description.

    Nothing is persisted yet -- this exists so the front end already has a place
    to send "here is what the user is looking at" when a model needs context.
    """
    payload = request.json
    structures = payload.get("structures", [])
    return {
        "received": True,
        "structures": [s.get("name") for s in structures],
        "selection": payload.get("selection", []),
    }
