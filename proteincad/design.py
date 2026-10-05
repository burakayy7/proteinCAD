"""Design runners.

A runner takes a job spec from the viewer and produces structures. There are two
kinds of job, and the same runners serve both:

    kind: binder   draw a backbone against the residues the user picked
    kind: fold     give that backbone a sequence, and check the sequence folds
                   back to it

The second is built here rather than in the browser (see `build_fold_spec`): it
needs the target exactly as it was sent to the model and the backbone exactly as
the model returned it, and both are already on this side.

Everything about *where* the work happens lives behind the runner interface, so
the browser, the job queue and the API do not change when the model does:

    MockRunner    local, no GPU. Places a real helical bundle where the volume
                  was drawn, so the whole workflow can be tested offline.
    HttpRunner    posts the spec to a GPU endpoint -- a Colab notebook, an EC2
                  box, anything that speaks the contract in notebooks/. The URL
                  comes from configuration, never from the code.
    Ec2Runner     the same, on an instance it starts first and lets stop again
                  when nothing needs it. Only the address differs, and only
                  because a stopped instance gets a new one each time.

Adding RFdiffusion locally means one more subclass whose `run` shells out to it
and reads the output directory. The contract it has to satisfy is small:

    run(spec, job) -> None, calling job.add_design(name, pdb, metrics) per result
                      and checking job.cancelled between designs.

The spec is documented in web/src/design/spec.js; the fields a binder runner
needs are target.pdb (a cropped target in scene coordinates), target.hotspots
(["A59", ...]), binder.contigs, binder.lengthMin/Max and run.numDesigns.
"""

from __future__ import annotations

import json
import math
import random
import socket
import time
import urllib.error
import urllib.request

from . import ec2, mock_design
from .colab_worker import (
    atom_lines, close_contacts, describe_options, engine_of, esm3_layout,
    esm3_option_problems, esm3_plan, heavy_atoms, mode_spec, mode_spec_for,
    option_problems, split_marked, thread_sequence, DEFAULT_MODE,
    ENGINES_BY_ID, ESM3_DEFAULT_MODE, ESM3_MODES_BY_ID, MODES_BY_ID,
)
from .structure import parse

# Chain ids a PDB file can carry, one column wide. Used to find the binder a
# name that does not collide with the target it is being joined to.
CHAIN_IDS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


class RunnerError(RuntimeError):
    """A run failed for a reportable reason -- a missing model, a rejected
    config, an unreachable endpoint. Worth showing the user; not worth a
    traceback, because nothing here is broken."""


class Runner:
    name = "runner"

    def run(self, spec: dict, job) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------- mock


class MockRunner(Runner):
    """Generates a placed helical bundle. Useful for everything except biology."""

    name = "mock"

    def __init__(self, delay: float = 0.6):
        self.delay = delay

    def run(self, spec: dict, job) -> None:
        if (spec.get("kind") or "binder") == "fold":
            return self.fold(spec, job)

        target = spec.get("target", {})
        volume = spec.get("volume")
        binder = spec.get("binder", {})
        count = int(spec.get("run", {}).get("numDesigns", 1) or 1)
        seed = int(spec.get("run", {}).get("seed", 0) or 0)

        centre, axis = site_frame(target, volume)
        low = int(binder.get("lengthMin", 60))
        high = max(low, int(binder.get("lengthMax", low)))

        for index in range(count):
            if job.cancelled:
                return
            if self.delay:
                time.sleep(self.delay)
            rng = random.Random(seed + index)
            length = rng.randint(low, high)
            residues = mock_design.helical_bundle(length, seed=seed + index)
            mock_design.orient_and_place(residues, axis, centre)
            pdb = mock_design.to_pdb(
                residues,
                chain_id="A",
                title=f"mock design {index + 1} against {target.get('name', 'target')}",
            )
            job.add_design(
                f"mock_{index + 1:02d}",
                pdb,
                {
                    "residues": len(residues),
                    "radius_of_gyration": round(mock_design.radius_of_gyration(residues), 2),
                    "note": "placeholder geometry, not a design",
                },
            )


    def fold(self, spec: dict, job) -> None:
        """Stage two, without a model: real geometry, invented chemistry.

        The contacts are measured off the actual coordinates, so the numbers
        that describe *where* the binder sits are true. The sequence is not a
        design and says so -- there is no way to fake that part usefully, and
        pretending otherwise is how a placeholder ends up in a slide.
        """
        subject = spec.get("complex", {})
        chains = [c for c in (subject.get("binderChains") or []) if c]
        lines = list(atom_lines(subject.get("pdb", "")))
        wanted = set(chains)
        binder = {chain: [line for line in lines if line[21] == chain] for chain in chains}
        if not chains or not all(binder.values()):
            raise RunnerError(f"the complex has no chains {', '.join(chains) or '(none named)'}")
        target = heavy_atoms(line for line in lines if line[21] not in wanted)

        run = spec.get("run", {})
        seed = int(run.get("seed", 0) or 0)
        for index in range(max(1, int(run.get("numDesigns", 1) or 1))):
            if job.cancelled:
                return
            if self.delay:
                time.sleep(self.delay)
            rng = random.Random(seed + index)
            threaded, parts = [], []
            for chain in chains:
                residues = len({line[22:27] for line in binder[chain]})
                sequence = "".join(rng.choice("AEKLRQVIDNSTFYGP") for _ in range(residues))
                parts.append(sequence)
                threaded += thread_sequence(binder[chain], sequence)
            pdb = "\n".join(threaded) + "\nEND\n"
            job.add_design(f"seq_{index + 1:02d}", pdb, {
                "source": "mock",
                "sequence": "/".join(parts),
                "contacts": close_contacts(heavy_atoms(atom_lines(pdb)), target),
                "note": "placeholder sequence, not a design",
            })


def site_frame(target: dict, volume: dict | None):
    """Where to put the new chain, and which way it should point.

    A drawn volume wins. Otherwise sit just off the picked residues, pointing
    away from the middle of the cropped target -- which is the direction a
    binder would approach from.
    """
    if volume and volume.get("centre"):
        axis = volume.get("axis") or [0.0, 0.0, 1.0]
        return tuple(volume["centre"]), tuple(axis)

    pdb = target.get("pdb", "")
    structure = parse(pdb, name=target.get("name", "target")) if pdb else None
    if not structure or not structure.coords:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)

    wanted = set(target.get("hotspots", []))
    picked = []
    for residue in structure.residues:
        if f"{residue.chain}{residue.seq}" not in wanted:
            continue
        for i in range(residue.start, residue.end + 1):
            if structure.atom_names[i] == "CA":
                picked.append(structure.coords[i])
    if not picked:
        picked = structure.coords

    site = mock_design.centroid(picked)
    middle = mock_design.centroid(structure.coords)
    outward = mock_design.sub(site, middle)
    if mock_design.norm(outward) < 1e-3:
        outward = (0.0, 0.0, 1.0)
    outward = mock_design.unit(outward)
    # Stand off far enough that the bundle is not inside the target.
    centre = mock_design.add(site, mock_design.scale(outward, 22.0))
    return centre, outward


# --------------------------------------------------------------------- http


class HttpRunner(Runner):
    """Hands the job to a GPU endpoint and waits for it.

    The contract, deliberately tiny so a notebook can implement it:

        POST {url}/design        {spec}  -> {"job_id": "..."}
        GET  {url}/design/{id}           -> {"status": "...", "progress": n,
                                             "designs": [{"name","pdb","metrics"}],
                                             "error": "..."}

    `Authorization: Bearer <token>` is sent when a token is configured.
    """

    name = "remote"

    def __init__(self, url: str, token: str = "", timeout: int = 120, poll: float = 3.0, name: str = "remote"):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.poll = poll
        self.name = name

    # A tunnel in front of the worker answers even when the worker does not, so
    # gateway codes have to be read as "nothing is serving", not "network down".
    GATEWAY_CODES = {502, 503, 504, 521, 522, 523, 530}

    def _request(self, path: str, payload=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(f"{self.url}{path}", data=data, headers=headers,
                                         method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            raise RunnerError(self._explain_http(error, path)) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, socket.gaierror):
                # The name is gone from DNS, which for a quick tunnel means the
                # tunnel itself has closed rather than anything being wrong here.
                raise RunnerError(
                    f"{self.url} no longer resolves — the tunnel has closed. Re-open it in "
                    "the notebook (start_tunnel()), then restart proteinCAD with the new URL "
                    "and token."
                ) from error
            raise RunnerError(
                f"cannot reach {self.url} ({reason}). Is the tunnel still open?"
            ) from error

    def _explain_http(self, error, path: str) -> str:
        code = error.code
        if code in self.GATEWAY_CODES:
            return (f"{self.url} answered {code}: the tunnel is up but nothing is serving "
                    "behind it. The worker process is not running — re-run the worker cell "
                    "in the notebook and check its output.")
        if code in (401, 403):
            return (f"{self.url} rejected the request ({code}). The token does not match the "
                    "one the worker was started with.")
        if code == 404:
            return (f"{self.url} has no {path}. Is that URL the proteinCAD worker, and not "
                    "something else?")
        detail = ""
        try:
            detail = error.read().decode()[:400]
        except Exception:
            pass
        return f"{self.url} returned {code}{': ' + detail if detail else ''}"

    def run(self, spec: dict, job) -> None:
        started = self._request("/design", spec)

        remote_id = started.get("job_id") or started.get("id")
        if not remote_id:
            raise RunnerError(f"endpoint did not return a job id: {started}")

        seen = 0
        hiccups = 0
        while True:
            if job.cancelled:
                try:
                    self._request(f"/design/{remote_id}/cancel", {})
                except Exception:
                    pass
                return

            time.sleep(self.poll)
            try:
                state = self._request(f"/design/{remote_id}")
                hiccups = 0
            except RunnerError:
                # Quick tunnels drop the odd request; only give up if it keeps up.
                hiccups += 1
                if hiccups >= 5:
                    raise
                continue

            job.stage = state.get("stage", "") or job.stage
            job.command = state.get("log", "") or job.command

            for design in state.get("designs", [])[seen:]:
                try:
                    job.add_design(
                        design.get("name", f"design_{seen + 1}"),
                        design.get("pdb", ""),
                        design.get("metrics", {}),
                    )
                except ValueError as error:
                    # The endpoint's fault, not ours: worth the message, not a
                    # traceback.
                    raise RunnerError(f"{self.url} returned something unusable: {error}") from error
                seen += 1

            status = state.get("status", "running")
            if status in ("done", "complete", "finished"):
                return
            if status in ("failed", "error"):
                raise RunnerError(state.get("error") or "the endpoint reported a failure")


# ---------------------------------------------------------------------- ec2


class Ec2Runner(Runner):
    """The same HTTP contract, on a box this app turns on and off.

    A GPU instance bills by the second whether or not it is computing, so it is
    kept stopped and the job that needs it wakes it. From the job's point of
    view this is HttpRunner with a slow first request: the spec, the polling and
    the results are identical, which is the point -- nothing else in the app
    knows the difference between a design run on Colab and one run here.

    The lifecycle lives in ec2.py; this is only the join between it and the
    queue. See proteincad/ec2.py for what stops the instance again.
    """

    name = "ec2"

    def __init__(self, machine, token: str = "", timeout: int = 120, poll: float = 3.0):
        self.machine = machine
        self.token = token
        self.timeout = timeout
        self.poll = poll

    def run(self, spec: dict, job) -> None:
        try:
            with self.machine.session(job) as url:
                # Built per run rather than held: a stopped instance comes back
                # with a different address every time, and a runner that
                # remembered the old one would spend the job talking to nothing.
                HttpRunner(url, token=self.token, timeout=self.timeout,
                           poll=self.poll, name=self.name).run(spec, job)
        except ec2.Cancelled:
            # The user cancelled while the box was booting. The queue reads a
            # clean return plus job.cancelled as cancelled; an exception here
            # would file it as a failure instead.
            return
        except ec2.Ec2Error as error:
            raise RunnerError(str(error)) from error


def gpu_machine(runners: dict):
    """The instance this deployment starts for itself, if it has one."""
    return getattr(runners.get("ec2"), "machine", None)


def build_runners(config) -> dict:
    """Whatever this deployment can actually run."""
    runners: dict = {"mock": MockRunner()}
    if config.compute_url:
        runners["remote"] = HttpRunner(
            config.compute_url,
            token=config.compute_token,
            timeout=config.compute_timeout,
        )
    machine = ec2.from_config(config)
    if machine is not None:
        runners["ec2"] = Ec2Runner(machine, token=config.ec2_token,
                                   timeout=config.compute_timeout)
        # Before any job asks for it: if the instance is already running --
        # left up by a previous run of this app, or started by hand -- nothing
        # else is going to notice and turn it off.
        machine.watch()
    return runners


# ------------------------------------------------------------------ checks


def count_atoms(pdb: str) -> int:
    """Coordinate lines, counted without splitting a possibly huge string."""
    first = 1 if pdb.startswith(("ATOM", "HETATM")) else 0
    return first + pdb.count("\nATOM") + pdb.count("\nHETATM")


# ------------------------------------------------------------- stage two


def free_chain(taken) -> str:
    """A chain id the target is not already using.

    The binder comes back from RFdiffusion as chain A and the target crop very
    often has a chain A of its own; joining them without checking would merge
    two proteins into one chain and ProteinMPNN would design across the join.
    """
    for candidate in CHAIN_IDS:
        if candidate not in taken:
            return candidate
    raise ValueError("the target already uses every chain id a PDB file has")


def renumber(lines: list) -> list:
    """Serials counted from 1 across a join. Two files each starting at 1 give
    the complex two atom 1s, which is legal-looking and confuses any reader that
    indexes by serial rather than by residue."""
    out, serial = [], 1
    for line in lines:
        if line == "TER":
            out.append("TER")
            continue
        out.append(f"{line[:6]}{serial:5d}{line[11:]}")
        serial += 1
    return out


def build_fold_spec(job, index: int, run: dict | None = None) -> dict:
    """The stage-two job for one finished backbone.

    Assembled on this side because everything it needs is already here: the
    target exactly as it was sent to the model, and the backbone exactly as the
    model returned it. The browser only has to say which design it means.

    How many chains the binder is, is not anybody's decision. RFdiffusion writes
    one output chain per contig block and keeps the original chain ids, so a
    crop that falls across five target fragments comes back as five target
    chains beside the binder. What separates the two is the B-factor marker the
    model writes, never the chain count.
    """
    if (job.spec.get("kind") or "binder") != "binder":
        raise ValueError("only a backbone job can be taken on to sequence design")
    path = job.design_path(index)
    if path is None or not path.is_file():
        raise ValueError(f"job {job.id} has no design {index}")

    stored = path.read_text()
    generated, given = split_marked(stored)
    if not generated:
        raise ValueError("that design has no atoms")

    # A protocol whose input is the molecule being redesigned has no counterpart
    # to join, and joining the input anyway would hand the sequence designer a
    # second copy of the same protein. See the note beside `subject` in the mode
    # tables.
    subject = bool(mode_spec_for(job.spec).get("subject"))
    target_lines = ([] if subject
                    else list(atom_lines(job.spec.get("target", {}).get("pdb", ""))))
    if given:
        # The design could not be placed, so it came back whole. Its own copy of
        # the target is the one that matches its coordinates; the crop sits
        # hundreds of angstroms away, and joining that instead would hand
        # ProteinMPNN a binder nowhere near the surface it was designed against.
        joined = renumber(generated + ["TER"] + given)
        chains = sorted({line[21] for line in generated})
    elif target_lines:
        taken = {line[21] for line in target_lines}
        renamed = {}
        for chain in sorted({line[21] for line in generated}):
            renamed[chain] = free_chain(taken)
            taken.add(renamed[chain])
        joined = renumber(target_lines + ["TER"]
                          + [line[:21] + renamed[line[21]] + line[22:] for line in generated])
        chains = sorted(renamed.values())
    elif not subject and mode_spec_for(job.spec)["target"] == "required":
        # Read through the engine, not the mode id: `generate` is not in
        # RFdiffusion's table, so looking it up there falls back to binder --
        # which requires a target -- and an ESM3 design from nothing could not be
        # taken on to stage two at all.
        raise ValueError("that job did not keep the target it was run against")
    else:
        # No counterpart is a protocol, not an omission: an unconditional
        # monomer is designed against nothing at all, and an inverse-folding run
        # is designed against itself. The design is the whole complex, so every
        # chain in it is one to design a sequence for.
        joined = renumber(generated)
        chains = sorted({line[21] for line in generated})

    target = job.spec.get("target", {})
    settings = dict(run or {})
    return {
        "version": 1,
        "kind": "fold",
        "model": settings.pop("model", None) or job.spec.get("model", "mock"),
        "complex": {"pdb": "\n".join(joined) + "\nEND\n", "binderChains": chains},
        # Carried so the worker can say how much of the new chain touches the
        # residues that were picked, as opposed to the target in general.
        "target": {"name": target.get("name", ""), "hotspots": target.get("hotspots", [])},
        "source": {"job": job.id, "design": index, "name": job.designs[index]["name"]},
        "run": {
            # numDesigns is the sequence count, so every existing counter --
            # the queue's cap, the progress bar, the worker's total -- means
            # the same thing it meant for a backbone job.
            "numDesigns": int(settings.get("numDesigns", 8) or 8),
            "foldTop": int(settings.get("foldTop", 2) or 2),
            "samplingTemp": float(settings.get("samplingTemp", 0.1) or 0.1),
            "seed": int(settings.get("seed", 0) or 0),
        },
    }


def validate_fold_spec(spec: dict) -> dict:
    subject = spec.get("complex")
    if not isinstance(subject, dict) or not subject.get("pdb"):
        raise ValueError("spec.complex.pdb is required")
    chains = subject.get("binderChains")
    if not isinstance(chains, list) or not chains:
        raise ValueError("spec.complex.binderChains must name at least one chain")
    if any(not isinstance(c, str) or len(c) != 1 for c in chains):
        raise ValueError("every entry in spec.complex.binderChains must be a single chain id")
    if count_atoms(subject["pdb"]) == 0:
        raise ValueError("the complex contains no atoms")
    run = spec.setdefault("run", {})
    run["numDesigns"] = max(1, min(int(run.get("numDesigns", 8) or 8), 64))
    run["foldTop"] = max(1, min(int(run.get("foldTop", 2) or 2), run["numDesigns"]))
    spec.setdefault("model", "mock")
    return spec


def validate_esm3_spec(spec: dict) -> dict:
    """The same job, checked against ESM3's idea of an input.

    Nothing is shared with the RFdiffusion path except the envelope: there is no
    contig to check, the length can come from a typed prompt or from the
    structure instead of from the range, and what would otherwise fail is a
    tensor shape rather than a rejected override. So the layout is built here --
    on the server, before anything is queued -- because building it is the check:
    a motif that does not fit in the length asked for cannot be laid out, and
    finding that out now costs nothing while finding it out on a GPU costs the
    wait for one.
    """
    name = spec.get("mode") or ESM3_DEFAULT_MODE
    if name not in ESM3_MODES_BY_ID:
        raise ValueError(f"spec.mode '{name}' is not an ESM3 protocol: "
                         f"{', '.join(ESM3_MODES_BY_ID)}")
    mode = ESM3_MODES_BY_ID[name]
    spec["mode"] = mode["id"]

    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    has_target = bool(target.get("pdb"))
    run = spec.setdefault("run", {})
    typed = str(run.get("sequencePrompt") or "").strip()

    if mode["target"] == "required" and not has_target:
        raise ValueError(f"spec.target.pdb is required for {mode['label'].lower()}")
    if mode["hotspots"] == "required" and not target.get("hotspots"):
        raise ValueError("spec.target.hotspots is required: pick the residues to keep")
    if mode["id"] == "predict" and not has_target and not typed:
        raise ValueError("structure prediction needs a sequence: type one into the "
                         "sequence prompt, or pick a structure to take it from")

    if has_target:
        if len(target["pdb"]) > 40_000_000:
            raise ValueError("the structure sent is too large; reduce the crop radius")
        if count_atoms(target["pdb"]) == 0:
            raise ValueError("the structure sent contains no atoms")

    # A length range is only load-bearing when nothing else says how long the
    # design is. A typed prompt states it, and the modes that take a whole
    # structure read it off that structure.
    if not typed and mode["id"] not in ("inverse", "predict", "resample"):
        binder = spec.get("binder") or {}
        low = int(binder.get("lengthMin", 0) or 0)
        high = int(binder.get("lengthMax", 0) or 0)
        if low < 4 or high < low:
            raise ValueError("spec.binder.lengthMin/lengthMax are not usable")
        if high > 2048:
            raise ValueError("ESM3 is not useful above about 2048 residues")

    run.setdefault("numDesigns", 1)
    bad = esm3_option_problems(run)
    if bad:
        raise ValueError("; ".join(bad))

    # Both of these raise ValueError with a message meant for a person, which is
    # exactly what this function contracts to do.
    esm3_plan(spec)
    esm3_layout(spec)

    spec.setdefault("model", "mock")
    return spec


def validate_spec(spec) -> dict:
    """Reject anything malformed before it reaches a queue or a GPU.

    What a job must carry depends on which protocol it is. A binder needs a
    target and hotspots; an unconditional monomer needs neither and would be
    wrong to send them, because hotspots are what select RFdiffusion's complex
    checkpoint. The mode says which, and the modes are the same table the panel
    drew itself from.

    Which table, is the engine's decision. The two share some mode ids and mean
    different things by them, so the engine is read first and never inferred
    from the mode.
    """
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    if (spec.get("kind") or "binder") == "fold":
        return validate_fold_spec(spec)

    asked = str(spec.get("engine") or "").strip().lower()
    if asked and asked not in ENGINES_BY_ID:
        raise ValueError(f"spec.engine '{asked}' is not an engine this build has: "
                         f"{', '.join(ENGINES_BY_ID)}")
    spec["engine"] = engine_of(spec)
    if spec["engine"] == "esm3":
        return validate_esm3_spec(spec)

    name = spec.get("mode") or DEFAULT_MODE
    if name not in MODES_BY_ID:
        raise ValueError(f"spec.mode '{name}' is not a protocol this build knows: "
                         f"{', '.join(MODES_BY_ID)}")
    mode = mode_spec(name)
    spec["mode"] = mode["id"]

    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    has_target = bool(target.get("pdb"))
    if mode["target"] == "required" and not has_target:
        raise ValueError(f"spec.target.pdb is required for {mode['label'].lower()}")
    if mode["hotspots"] == "required" and not target.get("hotspots"):
        raise ValueError("spec.target.hotspots is required: pick a design site")

    if has_target:
        if len(target["pdb"]) > 40_000_000:
            raise ValueError("cropped target is too large; reduce the crop radius")
        if count_atoms(target["pdb"]) == 0:
            raise ValueError("cropped target contains no atoms")

    binder = spec.get("binder") or {}
    contigs = str(binder.get("contigs") or "").strip()
    low = int(binder.get("lengthMin", 0) or 0)
    high = int(binder.get("lengthMax", 0) or 0)
    # A length range is how the contig gets built when there is not one already.
    # Partial diffusion has no range by definition -- the contig is the input,
    # at its own length -- and fold conditioning takes its shape from scaffold
    # files, so neither is required to have one.
    if mode["contigs"] in ("binder", "motif", "length") or not contigs:
        if low < 4 or high < low:
            raise ValueError("spec.binder.lengthMin/lengthMax are not usable")
        if high > 1000:
            raise ValueError("binder length above 1000 residues is not sensible")

    run = spec.setdefault("run", {})
    run.setdefault("numDesigns", 1)
    bad = option_problems(run)
    if bad:
        raise ValueError("; ".join(bad))

    spec.setdefault("model", "mock")
    return spec


def design_options() -> dict:
    """What this build can be asked for -- protocols, settings, checkpoints.

    Served rather than duplicated in the browser: the table lives in
    colab_worker.py beside the command builder that reads it, so the panel and
    the command line cannot drift apart.
    """
    return describe_options()


def estimate_residues(volume_cubic_angstrom: float) -> int:
    """Same conversion the viewer uses, kept here so a runner can check it."""
    return max(4, int(math.ceil(volume_cubic_angstrom / 133.0)))
