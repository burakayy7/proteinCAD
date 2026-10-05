"""GPU-side worker. Run this wherever the model lives.

This file is deliberately standalone -- no proteinCAD imports, standard library
only -- so it can be dropped into a Colab notebook, an EC2 box or a lab
workstation and run as-is:

    python colab_worker.py setup                    # install RFdiffusion
    python colab_worker.py --port 8000 --generator echo
    python colab_worker.py --port 8000 --generator rfdiffusion \\
        --rfdiffusion /content/RFdiffusion

`setup` fetches the checkouts, the weights and the dependencies, then proves the
result by importing the models in the interpreter that will run them. It is
idempotent: re-running it only does what is still missing.

Two stages, run separately and asked for separately, because the first is worth
looking at before paying for the second:

    kind: binder   RFdiffusion draws a backbone against the picked residues.
    kind: fold     ProteinMPNN gives that backbone a sequence, and ESMFold
                   folds the sequence on its own to see whether it comes back
                   to the shape it was designed for.

The second stage is the one that says whether the first produced a protein or
only a picture of one.

It serves the contract that proteincad/design.py's HttpRunner speaks:

    GET  /health                 -> {"status": "ok", "generator": "...",
                                     "busy": false, "idle": 12.0}
    POST /design         {spec}  -> {"job_id": "..."}
    GET  /design/{id}            -> {"status", "progress", "total",
                                     "designs": [{"name", "pdb", "metrics"}],
                                     "error"}
    POST /design/{id}/cancel     -> {"status": "cancelled"}

Set PROTEINCAD_WORKER_TOKEN (or pass --token) and the same value as
PROTEINCAD_COMPUTE_TOKEN on the app side; requests then need
`Authorization: Bearer <token>`. On a public tunnel, use one.

`busy` and `idle` on /health are for whoever is paying for the machine: on
Colab nothing reads them, and on a metered box the watchdog in deploy/aws uses
them to decide when to power it off. /health answers before the token check,
because that watchdog runs on the box itself with no token to hand.

Generators
----------
`echo`         returns the target crop back as a "design". Needs no GPU and no
               model; use it first to prove the connection works.
`rfdiffusion`  the real pipeline: both stages above, chosen by each job.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin

JOBS: dict = {}
JOBS_LOCK = threading.Lock()

# When this worker was last asked for anything.
#
# The machine underneath may be billing by the second (see deploy/aws), and
# whatever decides to turn it off needs to tell "nobody has asked for half an
# hour" apart from "a job is running and has nothing to say yet". Polling
# counts as being wanted: an app still watching a job is still using this.
# /health deliberately does not count, because the thing that watches for idle
# is itself a caller of /health.
LAST_ACTIVITY = time.time()


def touch() -> None:
    global LAST_ACTIVITY
    LAST_ACTIVITY = time.time()


def busy() -> bool:
    """Is there work on the card right now?"""
    return any(job.get("status") in ("queued", "running") for job in list(JOBS.values()))


# ------------------------------------------------------------- environment


def environment(repo: Path, base: dict | None = None) -> dict:
    """The environment RFdiffusion has to be run in.

    `run_inference.py` lives in `scripts/`, so Python puts *that* directory on
    sys.path -- not the repo root, where the `rfdiffusion` package actually is.
    `se3_transformer` is buried further down again, in `env/SE3Transformer`.
    Putting both on PYTHONPATH means neither package needs pip-installing, which
    removes two editable installs that could fail and keeps the checkout
    read-only.
    """
    env = dict(base if base is not None else os.environ)
    paths = [str(repo), str(repo / "env" / "SE3Transformer")]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    # RFdiffusion's checkpoints are pickled config objects, not bare tensors.
    # torch 2.6 made torch.load refuse those unless asked, and RFdiffusion
    # predates the change, so it dies with an UnpicklingError. This restores the
    # old default. Unknown to older torch, which simply ignores it.
    env.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
    return env


# Asked of the interpreter that will run RFdiffusion, which is not necessarily
# this one. Importing model_runners is the real test: it pulls in dgl,
# se3_transformer, hydra and e3nn, so one success clears almost everything.
PROBE = r'''
import importlib, json, sys
out = {"python": ".".join(str(v) for v in sys.version_info[:3]),
       "tag": "cp%d%d" % sys.version_info[:2], "modules": {}}
try:
    import torch
    out["torch"] = torch.__version__
    out["cuda_build"] = torch.version.cuda
    out["cuda"] = bool(torch.cuda.is_available())
    if out["cuda"]:
        out["gpu"] = torch.cuda.get_device_name(0)
except BaseException as error:
    out["torch"] = None
    out["cuda"] = False
    out["torch_error"] = "%s: %s" % (type(error).__name__, error)
for name in ("dgl", "e3nn", "hydra", "omegaconf", "opt_einsum", "pyrsistent",
             "scipy", "se3_transformer", "rfdiffusion",
             "rfdiffusion.inference.model_runners"):
    try:
        module = importlib.import_module(name)
        out["modules"][name] = "ok"
        if name == "dgl":
            out["dgl"] = getattr(module, "__version__", "unknown")
    except BaseException as error:
        out["modules"][name] = "%s: %s" % (type(error).__name__, error)
print("PROBE " + json.dumps(out))
'''


def probe(python: str, repo: Path, timeout: int = 600) -> dict:
    """Run PROBE in `python` and return what it found."""
    try:
        done = subprocess.run([python, "-c", PROBE], capture_output=True, text=True,
                              timeout=timeout, env=environment(repo))
    except Exception as error:
        return {"error": f"could not run {python}: {error}", "modules": {}}
    for line in reversed((done.stdout or "").splitlines()):
        if line.startswith("PROBE "):
            return json.loads(line[6:])
    tail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-5:]
    return {"error": f"{python} did not answer: " + " / ".join(tail), "modules": {}}


# ------------------------------------------------------------------- setup


class SetupError(RuntimeError):
    """Something the user has to know about, reported without a traceback."""


RFDIFFUSION_GIT = "https://github.com/RosettaCommons/RFdiffusion.git"

# Stage two. ProteinMPNN keeps its weights in the checkout (a few megabytes), so
# there is nothing to download beyond the clone, and it needs no package that
# RFdiffusion has not already pulled in.
PROTEINMPNN_GIT = "https://github.com/dauparas/ProteinMPNN.git"

# Where the checkouts go unless told otherwise. Colab's working directory, since
# that is where this runs most often; every path is a flag and an environment
# variable as well, so nothing here is baked into a deployment.
DEFAULT_RFDIFFUSION = os.environ.get("RFDIFFUSION_DIR", "/content/RFdiffusion")
DEFAULT_PROTEINMPNN = os.environ.get("PROTEINMPNN_DIR", "/content/ProteinMPNN")

# ESMFold predicts a structure from one sequence, with no alignment step. That
# is not a shortcut here, it is the only honest option: a de novo design has no
# homologues to align to, so a search-based predictor would be searching for
# nothing. Reached through transformers, which carries its own copy of the
# OpenFold helpers, so there is no second CUDA stack to install beside torch.
ESMFOLD_MODEL = "facebook/esmfold_v1"

# Pinned below transformers 5 to keep the import surface small. ESMFold is a
# quiet corner of that library and 5.x rebuilt the machinery underneath it: on
# 4.x the model file reaches numpy, torch and its own vendored OpenFold helpers,
# while on 5.x the same import also passes through `initialization`,
# `masking_utils`, `integrations` and `output_capturing`. Fewer moving parts
# under a model nobody is watching closely is worth the version.
#
# huggingface_hub is deliberately not named here even though it is used: it is
# transformers' own dependency, and pinning it separately is how you end up
# asking pip for a combination that cannot exist.
FOLD_DEPS = ("transformers<5,>=4.40", "accelerate")


def fold_environment(base: dict | None = None) -> dict:
    """The environment the predictor has to be run in.

    transformers looks for every framework it supports when it is imported, and
    Colab ships TensorFlow, so a process that wants nothing but torch loads a
    second deep-learning framework and a couple of gigabytes with it. That is
    free on a machine with memory to spare and fatal on one without: the load
    here needs most of what a Colab session has, and the kernel kills whichever
    process asks for the gigabyte that is not there.
    """
    env = dict(base if base is not None else os.environ)
    env.update(USE_TF="0", USE_JAX="0", USE_TORCH="1", TOKENIZERS_PARALLELISM="false")
    return env


# ------------------------------------------------------------------- esm3
#
# ESM3 is a second way to produce a backbone, and it works nothing like the
# first. RFdiffusion is one script with sixty keys, denoising coordinates from
# noise; ESM3 is a masked generative model over several *tracks* of the same
# protein -- sequence, structure, secondary structure, solvent accessibility,
# function -- that fills in whichever positions are masked, one track at a time,
# conditioning each pass on everything decoded so far.
#
# That difference is the whole reason it gets its own catalogue rather than
# another entry in RFdiffusion's mode table. There is no contig string and no
# Hydra override here. What a job carries instead is:
#
#     a prompt    an ESMProtein with some tracks given and the rest masked
#     a plan      the order to decode the masked tracks in, with how many
#                 iterative steps and how hot to sample each one
#
# The plan is the part that has no analogue in RFdiffusion, and it is not a
# nicety: generating structure from a sequence the model has just written is a
# different question from generating both at once, and the published recipes
# differ by exactly that ordering. So it is offered as data rather than fixed in
# code -- see ESM3_PLANS for the defaults each protocol starts from.
#
# Only the open model is reachable from here. EvolutionaryScale also serve
# larger ones over their own API, and that is a different thing to build: an
# account, a per-token bill and a network call out of the worker. This runs
# weights on a GPU the user is already paying for, like everything else here.
ESM3_MODEL = os.environ.get("PROTEINCAD_ESM3_MODEL", "esm3-sm-open-v1")

# `esm` is published by EvolutionaryScale and is 2.4 MB. Installed with its own
# dependency list it cost 6.3 GB of image -- almost as much as the whole
# RFdiffusion stack -- because that list names torchtext and torchvision, and
# resolving those brings a second copy of torch and its CUDA wheels into an
# environment that can already see one.
#
# So the package goes in without its dependencies and the dependencies are named
# here. That is a list this file now owns, which is a real cost: a dependency
# `esm` adds in a later release will be missing, and missing shows up as a
# ModuleNotFoundError rather than as anything about packaging. Two things make
# it affordable. The list was derived by walking every import in the chain that
# `from esm.models.esm3 import ESM3` actually reaches, not guessed; and the image
# build imports that exact symbol, so an omission fails the build at the step
# that caused it rather than a job three days later.
#
# Not here, on purpose:
#
#   torch, torchvision, torchtext   the first is inherited from the environment
#                                   RFdiffusion already needs; the other two are
#                                   declared by `esm` and never imported by it
#   flash_attn                      imported inside a try/except, so it is a
#                                   faster path when present and nothing when
#                                   not -- and building it costs more than the
#                                   attention it saves on a T4
#
# httpx is here and is the reason this list exists at all: `esm` does not declare
# it and cannot be imported without it, because anything under `esm.sdk` runs
# that package's __init__ and pulls in the Forge HTTP client.
ESM3_PACKAGE = "esm>=3.1.1"
ESM3_DEPS = (
    "httpx", "attrs", "biopython", "biotite>=1.0.0", "brotli", "cloudpathlib",
    "einops", "msgpack", "msgpack-numpy", "pandas", "tenacity", "zstd",
    # `esm` requires this bound, and the folding stage next door wants
    # >=4.40 -- which intersect, but not on the version the image happens to
    # have. Pinned inside ESM3's own environment so neither has to move.
    "transformers<4.48.2",
)

# The weights are public: 5.5 GB over 22 files, downloadable anonymously, with
# no token and no accepted licence needed to fetch them. Checked against the hub
# rather than assumed -- the repo was gated at release and is not now, which is
# the kind of fact worth re-reading rather than remembering.
#
# A token is still honoured when one is set, because anonymous hub downloads are
# rate-limited per address and a machine created for a job is a fresh address
# every time. It is an optimisation, never a requirement.
#
# The licence itself has not gone anywhere: EvolutionaryScale's terms are
# non-commercial. That is a thing to read before building a business on this,
# and not a thing the download enforces.
ESM3_REPO = "EvolutionaryScale/esm3-sm-open-v1"
ESM3_LICENCE = f"https://huggingface.co/{ESM3_REPO}"


def esm3_environment(base: dict | None = None) -> dict:
    """The environment ESM3 has to be run in.

    Same reasoning as fold_environment: the stack underneath it looks for every
    framework it can find at import, and on a machine with memory to spare that
    is merely wasteful while on Colab it is the gigabyte that gets the process
    killed.

    PROTEINCAD_ESM3_HOME is how the offline container reaches its weights, and
    it is a hub cache rather than a directory of files. ESMFold could be handed a
    plain directory because transformers takes a path where it takes a repo id;
    ESM3 resolves its own files by repo id internally, so what has to be in place
    is the cache layout it would have downloaded into.

    Which is why the cache directory and the hub's *home* are set separately. The
    weights arrive on a read-only mount, and huggingface_hub writes lock files
    under its home even when it has nothing to fetch -- so pointing both at the
    mount turns a model that is present into a permission error. HF_HUB_CACHE
    points at the blobs, read-only; HF_HOME is left to whatever writable path the
    caller chose.
    """
    env = dict(base if base is not None else os.environ)
    env.update(USE_TF="0", USE_JAX="0", USE_TORCH="1", TOKENIZERS_PARALLELISM="false")
    home = env.get("PROTEINCAD_ESM3_HOME", "").strip()
    if home:
        hub = Path(home) / "hub"
        env["HF_HUB_CACHE"] = str(hub if hub.is_dir() else home)
        env.setdefault("HF_HUB_OFFLINE", "1")
    return env


# RFdiffusion chooses its own checkpoint from the job -- hotspots select the
# complex model, inpainting selects InpaintSeq, fold conditioning selects the
# Fold models -- and choosing one that is not on disk is a crash a minute into
# the run, naming a path. All of them are listed so every protocol the panel
# offers has weights to run on. They are 484 MB each, which is small enough that
# `checkpoint_for` can work out what a job needs and fetch just that one on
# demand rather than anybody downloading 3.9 GB up front.
WEIGHTS = {
    "Base_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/6f5902ac237024bdd0c176cb93063dc4/Base_ckpt.pt",
    "Complex_base_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/e29311f6f1bf1af907f9ef9f44b8328b/Complex_base_ckpt.pt",
    "Complex_Fold_base_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/60f09a193fb5e5ccdc4980417708dbab/Complex_Fold_base_ckpt.pt",
    "InpaintSeq_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/74f51cfb8b440f50d70878e05361d8f0/InpaintSeq_ckpt.pt",
    "InpaintSeq_Fold_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/76d00716416567174cdb7ca96e208296/InpaintSeq_Fold_ckpt.pt",
    "ActiveSite_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/5532d2e1f3a4738decd58b19d633b3c3/ActiveSite_ckpt.pt",
    "Base_epoch8_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/12fc204edeae5b57713c5ad7dcb97d39/Base_epoch8_ckpt.pt",
    "Complex_beta_ckpt.pt":
        "http://files.ipd.uw.edu/pub/RFdiffusion/f572d396fae9206628714fb2ce00f72e/Complex_beta_ckpt.pt",
}

# What setup fetches when nothing is said: the two a binder run picks between,
# which is what the app does by default. The rest arrive when a job needs one.
CORE_WEIGHTS = ("Base_ckpt.pt", "Complex_base_ckpt.pt")

# The three sets of weights, as three things a person can be waiting for.
#
# On Colab and on a box you set up yourself these are files that are either
# there or not, and `setup` fetches them once. On a machine that is created for
# a job and destroyed after it, they are a download every time -- which makes
# them something the person pressing the button has to be told about, with a
# name, a size and a progress bar. So they get a table, like everything else in
# this file that the browser has to draw.
#
#     id       the prefix under weights/ in the bucket, and the button's value
#     bytes    roughly, for "1.4 GB of 3.9 GB" before the manifest is read
#     needs    what stops working without it, in the user's terms
MODELS = (
    {"id": "rfdiffusion", "label": "RFdiffusion", "bytes": 3_900_000_000,
     "help": "Draws the backbone. Every protocol needs it.",
     "needs": "designing anything"},
    {"id": "proteinmpnn", "label": "ProteinMPNN", "bytes": 100_000_000,
     "help": "Gives a backbone a sequence. Small, and quick to fetch.",
     "needs": "designing a sequence"},
    {"id": "esmfold", "label": "ESMFold", "bytes": 8_500_000_000,
     "help": "Folds that sequence on its own to see whether it comes back to "
             "the shape it was designed for. The big one.",
     "needs": "checking a sequence folds"},
    {"id": "esm3", "label": "ESM3-open", "bytes": 5_500_000_000,
     "help": "The other backbone generator. Writes sequence and structure "
             "together rather than a shape to be sequenced afterwards.",
     "needs": "designing with ESM3"},
)

MODELS_BY_ID = {model["id"]: model for model in MODELS}

# Stage two is a kind rather than a mode, so its models are named here instead
# of on an entry in the table below.
FOLD_MODELS = ("proteinmpnn", "esmfold")


def models_for(spec) -> list:
    """Which weights a job cannot run without.

    The one answer to that question. The panel greys out Run from it, the API
    reports it, and the worker fetches from it -- so a protocol that grows a
    dependency grows it in one place.

    Two axes decide it. `kind` says which stage the job is -- a backbone, or the
    sequence design and folding check that turn one into a protein -- and for a
    backbone, `engine` says which model draws it. A stage-two job is the same
    either way: what it needs is a backbone, not the thing that produced one.
    """
    spec = spec or {}
    if (spec.get("kind") or "binder") == "fold":
        return list(FOLD_MODELS)
    if engine_of(spec) == "esm3":
        return list(esm3_mode_spec(spec.get("mode")).get("models") or ("esm3",))
    return list(mode_spec(spec.get("mode")).get("models") or ("rfdiffusion",))

# DGL is the awkward dependency, for three separate reasons.
#
# It must not come from PyPI: the newest release there has no wheel for a
# current Python, so pip falls back to a source build of an ancient version and
# the failure surfaces as `cannot import name 'Mapping' from 'collections'` --
# which reads as a Python problem and is really a wrong-index problem. PyPI also
# only ever carried the CPU build, which is no use here.
#
# Its own index has CUDA wheels, but each is built against one torch minor
# version, so torch has to be a version DGL built for.
#
# And the index cannot be trusted: it lists wheels the bucket will not actually
# serve. Some objects return 403 AccessDenied while their neighbours download
# fine, so every candidate is probed before it is offered to pip. That is what
# `_reachable` is for, and it is not paranoia -- picking a listed-but-forbidden
# wheel is exactly how this failed in practice.
#
# The torch version here has to be exact, not just the right minor. A dgl wheel
# ships libraries named for the build it was made against --
# `libdgl_sparse_pytorch_2.6.0.so`, `libtensoradapter_pytorch_2.6.0.so` -- and
# loads them by that name at import. torch 2.6.1 would resolve to the same index
# and then fail to find its own libraries.
#
# Newest first. Only 2.6 has a wheel for Python 3.13 that is served at all.
DGL_STACKS = (
    ("2.6.0", "cu124"),
    ("2.6.0", "cu118"),
    ("2.4.1", "cu121"),
    ("2.4.1", "cu124"),
    ("2.3.1", "cu121"),
    ("2.2.2", "cu121"),
    ("2.1.2", "cu121"),
)

# Everything RFdiffusion and SE3Transformer import that is not torch or dgl.
# scipy, opt_einsum and numpy are already on Colab; named so a bare box works.
PURE_DEPS = ("hydra-core", "omegaconf", "pyrsistent", "e3nn", "opt_einsum", "scipy")

TORCH_INDEX = "https://download.pytorch.org/whl/"


def dgl_index(torch_version: str, cuda_tag: str) -> str:
    """Where DGL keeps the wheels built against this torch."""
    minor = ".".join(torch_version.split(".")[:2])
    return f"https://data.dgl.ai/wheels/torch-{minor}/{cuda_tag}/repo.html"


def pick_wheels(html: str, index: str, tag: str) -> list:
    """Every DGL wheel in this index built for Python `tag`, newest first.

    A list rather than a single choice, because the newest is not always one the
    server will part with. Absolute URLs: handing pip the exact wheel is what
    stops it wandering off to PyPI and installing the unusable CPU release.
    """
    found = []
    for href in re.findall(r'href="([^"]+\.whl)"', html):
        name = href.rsplit("/", 1)[-1]
        if f"-{tag}-" not in name:
            continue
        match = re.match(r"dgl-(\d+(?:\.\d+)*)", name)
        version = tuple(int(p) for p in match.group(1).split(".")) if match else (0,)
        found.append((version, urljoin(index, href)))
    return [url for _, url in sorted(found, reverse=True)]


def torch_wheel(version: str, cuda: str, tag: str) -> str:
    """The exact torch wheel a pinned stack needs, so it can be probed too."""
    return (f"{TORCH_INDEX}{cuda}/torch-{version}%2B{cuda}"
            f"-{tag}-{tag}-linux_x86_64.whl")


def fetch(url: str, timeout: int = 30) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except Exception:
        return None


def reachable(url: str, timeout: int = 20) -> bool:
    """Will this actually download? Being listed is not the same thing."""
    try:
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def resolve_dgl(tag: str, torch_version: str | None, cuda_build: str | None):
    """Find a (torch pin, dgl wheel) pair that this machine can actually install.

    Returns (pin, wheel url, index url), where a pin of None means the torch
    already installed is the right one and only dgl need be fetched -- worth
    having, since the alternative is a multi-gigabyte download. On failure the
    third value is instead the number of wheels that were listed but refused,
    which is what separates "none built for this Python" from "all forbidden".

    Nothing is returned unless it has been confirmed downloadable, and the torch
    match is exact: DGL resolves its own libraries by
    `f"libgraphbolt_pytorch_{torch.__version__.split('+')[0]}.so"`, so 2.6.1
    against a wheel built for 2.6.0 fails at import with nothing to suggest why.
    """
    installed = (torch_version or "").split("+")[0]
    installed_cuda = "cu" + cuda_build.replace(".", "") if cuda_build else None

    # A stack that is already installed goes first; sorting is stable, so the
    # rest keep their newest-first order.
    order = sorted(DGL_STACKS,
                   key=lambda stack: stack != (installed, installed_cuda))

    listed = 0
    for version, cuda in order:
        index = dgl_index(version, cuda)
        html = fetch(index)
        if not html:
            continue
        wheels = pick_wheels(html, index, tag)
        listed += len(wheels)
        here = (version, cuda) == (installed, installed_cuda)
        for wheel in wheels:
            if not reachable(wheel):
                continue
            if here:
                return None, wheel, index
            # The torch wheel is probed too: a dgl built against a torch this
            # Python has no build of is not a usable pair.
            if reachable(torch_wheel(version, cuda, tag)):
                return f"{version}+{cuda}", wheel, index
    return None, None, listed


def say(message: str) -> None:
    print(message, flush=True)


def stream(command: list, label: str) -> None:
    """Run something slow, showing its output as it goes.

    Shown rather than captured because these are multi-gigabyte downloads: with
    the output swallowed, the cell sits silent for minutes and looks hung. The
    tail is kept so a failure can still be reported in full -- hiding the output
    is how a missing dependency becomes a mystery an hour later.
    """
    say("  $ " + label)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    tail: list = []
    for line in process.stdout:
        line = line.rstrip()
        # pip redraws its progress bar with \r; keep one line, not thousands.
        print("    " + line, flush=True)
        tail.append(line)
        del tail[:-40]
    if process.wait() != 0:
        raise SetupError(f"{label} failed:\n" + "\n".join(tail[-15:]))


def pip(python: str, *args: str) -> None:
    stream([python, "-m", "pip", "install", *args], "pip install " + " ".join(args))


# Fetching the weights at setup rather than on the first job, so that a fold
# that is going to take eleven gigabytes of download says so in the cell where
# downloads are expected instead of appearing to hang halfway through a run.
PREFETCH = ("import sys;from huggingface_hub import snapshot_download;"
            "print(snapshot_download(sys.argv[1]))")


def prefetch(python: str, model: str) -> None:
    stream([python, "-c", PREFETCH, model], f"download {model}")


# Compiled against one exact torch and released in lockstep with it. Nothing
# here uses them; Colab ships them, and moving torch out from under them is what
# turns them into a trap.
TORCH_COMPANIONS = ("torchvision", "torchaudio")

COMPANION_PROBE = r'''
import importlib, importlib.util, json, sys
out = {}
for name in sys.argv[1:]:
    if importlib.util.find_spec(name) is None:
        continue
    try:
        out[name] = importlib.import_module(name).__version__
    except BaseException as error:
        out[name] = "broken: %s: %s" % (type(error).__name__, error)
print("COMPANIONS " + json.dumps(out))
'''


def companions(python: str) -> dict:
    """Which of torch's compiled siblings are installed, and whether they load."""
    try:
        done = subprocess.run([python, "-c", COMPANION_PROBE, *TORCH_COMPANIONS],
                              capture_output=True, text=True, timeout=300)
    except Exception:
        return {}
    for line in reversed((done.stdout or "").splitlines()):
        if line.startswith("COMPANIONS "):
            return json.loads(line[11:])
    return {}


def align_torch_extras(python: str, torch_version: str, attempt) -> list:
    """Repair what pinning torch broke on the way past.

    DGL forces an exact torch, and Colab's torchvision was compiled against the
    torch Colab shipped with. Once torch moves, torchvision's extension can no
    longer register its own operators -- and because `import transformers`
    reaches for torchvision, the failure surfaces as

        RuntimeError: operator torchvision::nms does not exist

    while the model being loaded is a protein folder. It reads as a problem with
    ESMFold and is really the wreckage of a step three sections earlier.

    Only touched when a companion is installed *and* actually broken, and
    repaired by naming the torch version and letting pip pick the sibling built
    for it: the wheels pin their torch exactly, so the resolver already knows the
    answer and there is no version table here to go stale.
    """
    state = companions(python)
    broken = sorted(name for name, value in state.items() if value.startswith("broken:"))
    cuda = torch_version.split("+")[1] if "+" in torch_version else ""
    # One at a time, not one command for all of them: the two are built for the
    # same torch but not always for the same Python, and asking for both at once
    # means a missing wheel for the one nobody needs takes the repair of the one
    # that is actually in the way down with it.
    for name in broken:
        say(f"  {name} was built against a different torch: {state[name][8:]}")
        attempt(name, pip, python, name, f"torch=={torch_version}",
                *(["--index-url", TORCH_INDEX + cuda] if cuda else []),
                "--extra-index-url", "https://pypi.org/simple")
    return broken


def clone(repo: Path, url: str = RFDIFFUSION_GIT,
          marker: str = "scripts/run_inference.py") -> None:
    """Put a checkout at `repo`, keeping anything already there.

    Cloned into a staging directory and merged in, rather than straight to the
    target, because the weights live inside the checkout: once ~1 GB has been
    downloaded into `models/`, git would refuse the directory as non-empty and
    the only way forward would be to delete the weights and fetch them again.
    """
    if (repo / marker).is_file():
        say(f"  checkout already at {repo}")
        return
    if not shutil.which("git"):
        raise SetupError("git is not installed, so the checkout cannot be fetched")

    staging = repo.parent / (repo.name + ".clone")
    shutil.rmtree(staging, ignore_errors=True)
    repo.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(["git", "clone", "--depth", "1", url, str(staging)],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise SetupError("git clone failed:\n" + (done.stdout + done.stderr)[-1000:])
        repo.mkdir(parents=True, exist_ok=True)
        kept = 0
        for item in staging.iterdir():
            target = repo / item.name
            if target.exists():
                kept += 1           # whatever is already there wins, weights included
                continue
            item.replace(target)
        say(f"  cloned into {repo}" + (f" (kept {kept} existing entries)" if kept else ""))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def human(size: int) -> str:
    """Sizes the way a person reads them. A weight file truncated to a few
    hundred bytes -- an error page saved as a .pt -- has to not print as `0 MB`."""
    return f"{size / 1e6:.0f} MB" if size >= 1e6 else f"{size} bytes"


def download(url: str, path: Path) -> None:
    """Fetch unless an intact copy is already there.

    Weights are ~480 MB each and a cut-off download leaves a file that looks
    present but fails much later, inside torch.load. Comparing against the
    declared length is what makes re-running this cell trustworthy.
    """
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            expected = int(response.headers.get("Content-Length") or 0)
    except Exception as error:
        raise SetupError(f"cannot reach {url}: {error}") from error

    if path.is_file() and (not expected or path.stat().st_size == expected):
        say(f"  {path.name} already complete ({human(path.stat().st_size)})")
        return
    if path.is_file():
        say(f"  {path.name} is {human(path.stat().st_size)}, expected "
            f"{human(expected)} — fetching again")

    part = path.with_suffix(path.suffix + ".part")
    say(f"  downloading {path.name} ({human(expected)})")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, open(part, "wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    except Exception as error:
        part.unlink(missing_ok=True)
        raise SetupError(f"download of {path.name} failed: {error}") from error
    if expected and part.stat().st_size != expected:
        size = part.stat().st_size
        part.unlink(missing_ok=True)
        raise SetupError(f"{path.name} came back as {size} bytes, not {expected}")
    part.replace(path)


def weights_wanted(choice) -> list:
    """Which checkpoints to fetch up front.

    `core` is the default because a binder run only ever picks between two of
    them and 3.9 GB of weights is a slow first cell for capabilities most jobs
    never reach. Nothing is lost by it: a job that needs another one says so and
    the worker fetches it then.
    """
    text = str(choice or "core").strip()
    if text == "all":
        return sorted(WEIGHTS)
    if text in ("core", ""):
        return list(CORE_WEIGHTS)
    if text == "none":
        return []
    names, unknown = [], []
    for name in re.split(r"[,\s]+", text):
        if not name:
            continue
        (names if name in WEIGHTS else unknown).append(name)
    if unknown:
        raise SetupError(f"no such checkpoint: {', '.join(unknown)}. "
                         f"Known: {', '.join(sorted(WEIGHTS))}")
    return names


def run_setup(args) -> int:
    repo = Path(args.rfdiffusion).expanduser()
    python = args.python
    # Which halves of the pipeline to build. Both by default; separately when
    # one is already in place and only the other is wanted.
    only = getattr(args, "only", None) or "all"
    backbone, folding = only in ("all", "rfdiffusion"), only in ("all", "fold")
    # ESM3 is deliberately not part of `all`. It is a second backbone engine
    # rather than a missing piece of the first, and 5.5 GB that most runs of
    # `setup` have no use for is not something to fetch by default.
    esm3 = only == "esm3"
    troubles: list = []

    def attempt(what, function, *arguments):
        """Run a step, remember a failure, and carry on.

        Steps here are largely independent -- the dependencies that are not dgl
        do not care whether dgl worked -- so stopping at the first failure just
        hides the rest of the picture. That is exactly what happened in practice:
        one unreachable wheel aborted the run and left e3nn and hydra
        uninstalled, so the next report blamed three things instead of one.
        """
        try:
            function(*arguments)
            return True
        except SetupError as error:
            say(f"  ! {what}: {error}")
            troubles.append(f"{what}: {error}")
            return False

    before = probe(python, repo, timeout=300)
    if before.get("error"):
        raise SetupError(before["error"])
    tag = before["tag"]
    say(f"python {before['python']} ({tag}), torch {before.get('torch')}, "
        f"cuda build {before.get('cuda_build')}\n")

    models = repo / "models"
    pin, pinned = None, False
    if backbone:
        say("=== 1/5  RFdiffusion checkout")
        attempt("checkout", clone, repo)

        say("\n=== 2/5  model weights")
        models.mkdir(parents=True, exist_ok=True)
        for name in weights_wanted(getattr(args, "weights", None)):
            attempt(f"download {name}", download, WEIGHTS[name], models / name)

        say("\n=== 3/5  dependencies")
        pin, pinned = setup_rfdiffusion_deps(python, before, tag, attempt, troubles, say)

    # After torch has settled, whether it moved in this run or an earlier one:
    # anything compiled against the torch that used to be here is broken now,
    # and the failure it produces names something else entirely.
    settled = (pin if pinned else None) or before.get("torch")
    if settled:
        align_torch_extras(python, settled, attempt)

    if folding:
        say("\n=== 4/5  sequence design and the folding check")
        attempt("ProteinMPNN", clone, Path(getattr(args, "proteinmpnn", None)
                                           or DEFAULT_PROTEINMPNN).expanduser(),
                PROTEINMPNN_GIT, "protein_mpnn_run.py")
        attempt("folding dependencies", pip, python, *FOLD_DEPS,
                *([f"torch=={settled}"] if settled else []))
        model = getattr(args, "esmfold_model", None) or ESMFOLD_MODEL
        if getattr(args, "skip_weights", False):
            say(f"  not fetching {model}; the first fold job will download it")
        else:
            say(f"  fetching {model} — about 8.5 GB, once")
            attempt(f"download {model}", prefetch, python, model)

    if esm3:
        say("\n=== 4/5  ESM3")
        where = getattr(args, "esm3_python", "") or python
        # The torch pin is RFdiffusion's, and it only applies where RFdiffusion
        # lives. Carrying it into an interpreter of ESM3's own would impose a
        # constraint for the sake of a model that is not installed there.
        pinned_torch = [f"torch=={settled}"] if settled and where == python else []
        if where != python:
            say(f"  installing into {where}")
        # The package first and alone, so pip cannot reach its dependency list.
        attempt("esm3 package", pip, where, "--no-deps", ESM3_PACKAGE)
        attempt("esm3 dependencies", pip, where, *ESM3_DEPS, *pinned_torch)
        model = getattr(args, "esm3_model", None) or ESM3_MODEL
        if getattr(args, "skip_weights", False):
            say(f"  not fetching {model}; the first job will download it")
        elif Path(model).expanduser().exists():
            say(f"  {model} is a local path; nothing to fetch")
        else:
            say(f"  fetching {model} — about 5.5 GB, once")
            attempt(f"download {model}", prefetch_esm3, where, model)

    say("\n=== 5/5  checking it can actually run")
    return report_setup(args, python, repo, models, troubles, attempt, backbone, folding,
                        esm3=esm3)


# ESM3's weights are not a Hugging Face repo id the hub will snapshot blindly:
# the loader resolves several files -- the model, the structure encoder and
# decoder, the function tokeniser -- and which ones depend on the variant. So
# the fetch is done by asking the library to load it, once, and letting its own
# resolver pull what it needs into the cache the next run will read.
PREFETCH_ESM3 = ("import sys;from esm.models.esm3 import ESM3;"
                 "ESM3.from_pretrained(sys.argv[1]);print('fetched')")


def prefetch_esm3(python: str, model: str) -> None:
    done = subprocess.run([python, "-c", PREFETCH_ESM3, model],
                          capture_output=True, text=True, env=esm3_environment())
    if done.returncode != 0:
        tail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-4:]
        hint = ""
        if any("401" in line or "403" in line or "gated" in line.lower()
               or "awaiting" in line.lower() for line in tail):
            # These download anonymously today, so an authorisation error means
            # the repo has been gated since this was written rather than that a
            # token was forgotten. The two have different fixes.
            hint = (f" — these weights were public when this was written, so that reads as "
                    f"the repo having been gated since. Check {ESM3_LICENCE} and set "
                    "HF_TOKEN from an account allowed to read it")
        raise SetupError(f"could not fetch {model}: " + " / ".join(tail) + hint)


def setup_rfdiffusion_deps(python, before, tag, attempt, troubles, say):
    """torch, dgl and the rest, in the one order that works. Returns the torch
    pin that was chosen and whether it actually installed."""
    pin, wheel, where = resolve_dgl(tag, before.get("torch"), before.get("cuda_build"))
    pinned = False
    if not wheel:
        listed = where or 0
        troubles.append(
            f"no installable DGL for Python {before['python']} ({tag}): "
            + (f"its index lists {listed}, but the server refuses to serve any of them"
               if listed else "no index has a build for this Python")
            + ". RFdiffusion cannot run without dgl. Python 3.10-3.12 has far more "
              "choices than 3.13 — that is the way out if this persists."
        )
        say(f"  ! {troubles[-1]}")
    else:
        say(f"  dgl from {where}")
        if pin and not before.get("torch"):
            say(f"  no torch here yet; installing {pin}")
        elif pin:
            say(f"  dgl has no build for torch {before.get('torch')}; pinning torch {pin}")
        else:
            say(f"  keeping torch {before.get('torch')}, which dgl built against")
        # PyPI stays in the search path for torch's own dependencies, which the
        # CUDA index does not necessarily carry. There is no ambiguity about
        # torch itself: PyPI has no `+cu124` build to be confused with.
        pinned = not pin or attempt(
            "torch", pip, python, f"torch=={pin}",
            "--index-url", TORCH_INDEX + pin.split("+")[1],
            "--extra-index-url", "https://pypi.org/simple")

        # dgl's wheel links against libtorch, so it has to be reinstalled
        # whenever torch moves -- a dgl that imported fine a moment ago will not
        # survive the change. Naming the torch version in the same command stops
        # pip treating dgl's own loose torch requirement as licence to move it
        # straight back.
        if not pinned:
            # Installing it anyway would bind it to the torch that is still here,
            # which is precisely the one dgl has no build for.
            say("  skipping dgl: it has to match the torch that would not install")
        elif pin or before["modules"].get("dgl") != "ok":
            if pin:
                subprocess.run([python, "-m", "pip", "uninstall", "-y", "dgl"],
                               capture_output=True, text=True)
            attempt("dgl", pip, python, wheel, f"torch=={pin or before.get('torch')}")

    # After torch, never before: e3nn requires torch, so installing it first on a
    # machine without one would drag in whatever torch PyPI offers today -- which
    # is not the version dgl's bundled libraries are named for. Naming the version
    # again here stops e3nn's own requirement from moving it afterwards -- but
    # only the one actually installed, or pip is handed a constraint nothing meets.
    settled = (pin if pinned else None) or before.get("torch")
    attempt("dependencies", pip, python, *PURE_DEPS,
            *([f"torch=={settled}"] if settled else []))
    return pin, pinned


def report_setup(args, python, repo, models, troubles, attempt, backbone, folding,
                 esm3: bool = False) -> int:
    """Prove the result, then say plainly which stages can and cannot run."""
    after = probe(python, repo, timeout=900)
    if after.get("error"):
        raise SetupError(after["error"])

    # DGL's wheels are compiled against numpy 1. On a numpy 2 host the import
    # fails deep in the C extension, which reads as a DGL problem but is not.
    broken = after["modules"].get("dgl", "")
    if broken != "ok" and re.search(r"numpy|_ARRAY_API|multiarray", broken, re.I):
        say("  dgl was built against numpy 1; pinning numpy below 2 and retrying")
        if attempt("numpy", pip, python, "numpy<2"):
            after = probe(python, repo, timeout=900)

    say(f"  python      {after['python']}")
    say(f"  torch       {after.get('torch')} (cuda build {after.get('cuda_build')})")
    say(f"  gpu         {after.get('gpu') or 'none visible'}")
    say(f"  dgl         {after.get('dgl')}")
    for name, state in after["modules"].items():
        say(f"  {'ok  ' if state == 'ok' else 'FAIL'}  {name}"
            + ("" if state == "ok" else f"  — {state}"))
    say(f"  weights     {', '.join(sorted(p.name for p in models.glob('*.pt'))) or 'NONE'}")

    mpnn = ProteinMPNN(getattr(args, "proteinmpnn", None) or DEFAULT_PROTEINMPNN, python)
    folder = EsmFolder(python, getattr(args, "esmfold_model", None) or ESMFOLD_MODEL)
    stage_two = mpnn.problems() + (folder.problems() if folding else [])
    if folding:
        say(f"  sequences   {'ok' if not mpnn.problems() else mpnn.problems()[0]}")
        say(f"  folding     {'ok' if not folder.problems() else folder.problems()[0]}")
        say(f"  transformers {folder.found.get('transformers') or 'not installed'}")

    engine = Esm3Generator(
        python=getattr(args, "esm3_python", "") or python,
        model=getattr(args, "esm3_model", None) or ESM3_MODEL) if esm3 else None
    esm3_problems = engine.problems() if engine else []
    if engine:
        say(f"  esm         {engine.found.get('esm') or 'not installed'}")
        say(f"  esm3        {'ok' if not esm3_problems else esm3_problems[0]}")

    failed = [n for n, s in after["modules"].items() if s != "ok"]
    if troubles:
        say("\nSteps that did not finish:")
        for trouble in troubles:
            say("  ! " + trouble.splitlines()[0])

    # Each stage is reported on its own. They fail for unrelated reasons and one
    # is useful without the other, so a single verdict would hide which of them
    # is actually available.
    if backbone and failed:
        say("\nBackbones are not ready: " + ", ".join(failed) + " cannot be imported. "
            "The reason is printed beside each one.")
    elif backbone:
        say("\nBackbones are ready.")
    if folding and stage_two:
        say("Sequences and folding are not ready:")
        for problem in stage_two:
            say("  ! " + problem)
    elif folding:
        say("Sequences and folding are ready.")
    if esm3 and esm3_problems:
        say("ESM3 is not ready:")
        for problem in esm3_problems:
            say("  ! " + problem)
    elif esm3:
        say("ESM3 is ready.")

    if not after.get("cuda"):
        say("\nNo GPU is visible. Set Runtime > Change runtime type > GPU, then run "
            "this cell again.")
        return 1
    # dgl and RFdiffusion's imports are checked for every run because the probe
    # is one script, but they are only a verdict on the engine that needs them.
    # An ESM3 install on a machine that has never had RFdiffusion is finished and
    # working, and reporting it as failed because dgl is absent would be wrong
    # about the only thing it was asked to do.
    if (backbone and failed) or (folding and stage_two) or (esm3 and esm3_problems):
        return 1
    say("\nStart the worker with --generator "
        + ("esm3." if esm3 and not backbone else
           "rfdiffusion,esm3." if esm3 else "rfdiffusion."))
    return 0


# ---------------------------------------------------------------- geometry
#
# Small and by hand, because this file has to stay importable on a machine where
# nothing has been installed yet -- the setup code above runs before numpy is
# guaranteed to be there, and the diagnostics have to work when it is not.


def centroid(points: list) -> tuple:
    n = len(points)
    return tuple(sum(p[i] for p in points) / n for i in range(3))


def jacobi(matrix: list, sweeps: int = 60) -> tuple:
    """Eigenvalues and eigenvectors of a small symmetric matrix.

    Cyclic Jacobi rotations: short enough to carry here, and exact enough for
    the 4x4 below. Returns (values, vectors), vectors[k] going with values[k].
    """
    n = len(matrix)
    a = [row[:] for row in matrix]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(sweeps):
        if sum(a[i][j] ** 2 for i in range(n) for j in range(i + 1, n)) < 1e-22:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) < 1e-18:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
                sign = 1.0 if theta >= 0 else -1.0
                t = sign / (abs(theta) + (theta * theta + 1.0) ** 0.5)
                c = 1.0 / (t * t + 1.0) ** 0.5
                s = t * c
                for k in range(n):                      # columns
                    akp, akq = a[k][p], a[k][q]
                    a[k][p], a[k][q] = c * akp - s * akq, s * akp + c * akq
                for k in range(n):                      # and rows
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k], a[q][k] = c * apk - s * aqk, s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p], v[k][q] = c * vkp - s * vkq, s * vkp + c * vkq
    return [a[i][i] for i in range(n)], [[v[i][k] for i in range(n)] for k in range(n)]


def superpose(mobile: list, fixed: list) -> tuple:
    """The rigid motion that best puts `mobile` onto `fixed`, and what is left.

    Returns (rotate, shift, rmsd), where `rotate` is a 3x3 matrix and a mobile
    point maps into the fixed frame as `rotate . point + shift`.

    Horn's quaternion method: the best rotation is the leading eigenvector of a
    4x4 built from the cross-covariance. A quaternion is a rotation by
    construction, so unlike a hand-rolled Kabsch this cannot quietly come back
    as a reflection when the points are nearly coplanar -- which a short binder,
    often a flat sheet or a single helix, very nearly is.
    """
    n = min(len(mobile), len(fixed))
    if n < 3:
        raise ValueError("at least three points are needed to superpose")
    cm, cf = centroid(mobile[:n]), centroid(fixed[:n])
    p = [[point[i] - cm[i] for i in range(3)] for point in mobile[:n]]
    q = [[point[i] - cf[i] for i in range(3)] for point in fixed[:n]]
    s = [[sum(p[k][i] * q[k][j] for k in range(n)) for j in range(3)] for i in range(3)]

    horn = [
        [s[0][0] + s[1][1] + s[2][2], s[1][2] - s[2][1], s[2][0] - s[0][2], s[0][1] - s[1][0]],
        [s[1][2] - s[2][1], s[0][0] - s[1][1] - s[2][2], s[0][1] + s[1][0], s[0][2] + s[2][0]],
        [s[2][0] - s[0][2], s[0][1] + s[1][0], -s[0][0] + s[1][1] - s[2][2], s[1][2] + s[2][1]],
        [s[0][1] - s[1][0], s[0][2] + s[2][0], s[1][2] + s[2][1], -s[0][0] - s[1][1] + s[2][2]],
    ]
    values, vectors = jacobi(horn)
    w, x, y, z = vectors[values.index(max(values))]
    rotate = [
        [w * w + x * x - y * y - z * z, 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), w * w - x * x + y * y - z * z, 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), w * w - x * x - y * y + z * z],
    ]
    shift = tuple(cf[i] - sum(rotate[i][j] * cm[j] for j in range(3)) for i in range(3))

    total = 0.0
    for point, target in zip(mobile[:n], fixed[:n]):
        moved = [sum(rotate[i][j] * point[j] for j in range(3)) + shift[i] for i in range(3)]
        total += sum((moved[i] - target[i]) ** 2 for i in range(3))
    return rotate, shift, (total / n) ** 0.5


def close_contacts(here: list, there: list, cutoff: float = 4.5) -> int:
    """Atom pairs within `cutoff` of each other, one atom from each side.

    Bucketed onto a grid of `cutoff`-sized cells rather than compared pair by
    pair: a crop is thousands of atoms against a binder of hundreds, and the
    naive loop is seconds of the job spent on arithmetic.
    """
    grid: dict = {}
    for point in there:
        grid.setdefault(tuple(int(point[i] // cutoff) for i in range(3)), []).append(point)
    limit, found = cutoff * cutoff, 0
    for point in here:
        bx, by, bz = (int(point[i] // cutoff) for i in range(3))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for other in grid.get((bx + dx, by + dy, bz + dz), ()):
                        if sum((point[i] - other[i]) ** 2 for i in range(3)) <= limit:
                            found += 1
    return found


# ---------------------------------------------------------------------- pdb

# The genetic code's own table, needed to write a designed sequence back onto
# the backbone it was designed for.
THREE_LETTER = {
    "A": "ALA", "C": "CYS", "D": "ASP", "E": "GLU", "F": "PHE", "G": "GLY",
    "H": "HIS", "I": "ILE", "K": "LYS", "L": "LEU", "M": "MET", "N": "ASN",
    "P": "PRO", "Q": "GLN", "R": "ARG", "S": "SER", "T": "THR", "V": "VAL",
    "W": "TRP", "Y": "TYR",
}


def atom_lines(pdb_text: str):
    for line in pdb_text.splitlines():
        if line.startswith(("ATOM", "HETATM")) and len(line) >= 54:
            yield line


def coordinates(line: str) -> tuple:
    return float(line[30:38]), float(line[38:46]), float(line[46:54])


def with_coordinates(line: str, point) -> str:
    return f"{line[:30]}{point[0]:8.3f}{point[1]:8.3f}{point[2]:8.3f}{line[54:]}"


def residue_key(line: str) -> tuple:
    """Chain and residue number with its insertion code -- what identifies a
    residue in a file, as opposed to what identifies it in a contig."""
    return line[21], line[22:27].strip()


def heavy_atoms(lines) -> list:
    """Coordinates of everything that is not hydrogen. Contacts counted over
    hydrogens would depend on whether the file happens to have any, and an
    experimental structure usually does not while a prediction usually does."""
    return [coordinates(line) for line in lines
            if line[76:78].strip().upper() not in ("H", "D")
            and not line[12:16].strip().startswith("H")]


def alpha_carbons(lines) -> list:
    return [coordinates(line) for line in lines if line[12:16].strip() == "CA"]


def split_marked(pdb_text: str) -> tuple:
    """Atoms the model generated, and atoms it was handed.

    RFdiffusion marks its own work in the B-factor column: 1 for the motif it
    was given, 0 for everything it built. Reading that marker is the only
    reliable way to tell the two apart, because the chains cannot: RFdiffusion
    writes one output chain per contig block, keeping the original chain id, so
    a crop of five target fragments comes back as five target chains beside the
    binder. Counting chains and expecting one is how the target gets mistaken
    for more binder.

    A file with nothing marked is taken as generated throughout -- that is what
    a design already stripped of its target looks like.
    """
    generated, given = [], []
    for line in atom_lines(pdb_text):
        column = line[60:66].strip()
        (given if column and float(column) >= 0.5 else generated).append(line)
    return (generated, given) if generated else (given, [])


def thread_sequence(lines, sequence: str) -> list:
    """Write a sequence onto a backbone, residue by residue in file order.

    RFdiffusion returns every residue as glycine because it never chose any.
    Putting the designed letters back means the result is a protein a viewer
    can colour by residue type and a person can read, rather than a poly-glycine
    trace with the sequence stranded in a metrics field.
    """
    order, seen = [], {}
    for line in lines:
        key = residue_key(line)
        if key not in seen:
            seen[key] = len(order)
            order.append(key)
    out = []
    for line in lines:
        index = seen[residue_key(line)]
        name = THREE_LETTER.get(sequence[index:index + 1].upper())
        out.append(f"{line[:17]}{name:>3}{line[20:]}" if name else line)
    return out


# --------------------------------------------------------------- generators


class Generator:
    name = "generator"

    def generate(self, spec: dict, job: dict) -> None:
        raise NotImplementedError


class EchoGenerator(Generator):
    """Connection test: hands the input back, one 'design' at a time."""

    name = "echo"

    def generate(self, spec, job):
        kind = (spec.get("kind") or "binder").lower()
        # Echoing a file back is not sequence design. Saying so beats handing
        # back something shaped like a result: a fold job carries its
        # coordinates under `complex`, so the old code read an absent
        # `target.pdb`, echoed an empty string, and the app stored nothing at
        # all as a finished design.
        if kind != "binder":
            job["error"] = (
                f"this worker is running the echo generator, which only proves the "
                f"connection works — it cannot run a {kind!r} job. Restart it with "
                "--generator rfdiffusion; in the notebook that is the "
                "'Restart the worker with the models' cell.")
            return

        count = int(spec.get("run", {}).get("numDesigns", 1) or 1)
        pdb = spec.get("target", {}).get("pdb", "")
        if not any(atom_lines(pdb)):
            job["error"] = "the job carried no coordinates for the echo to hand back"
            return
        for index in range(count):
            if job["cancel"]:
                return
            time.sleep(1.0)
            job["designs"].append({
                "name": f"echo_{index + 1:02d}",
                "pdb": pdb,
                "metrics": {"note": "echo of the target, not a design"},
            })
            job["progress"] = len(job["designs"])


# ------------------------------------------- everything RFdiffusion can do
#
# RFdiffusion is one script with about sixty configuration keys, and every
# protocol in its README -- binder design, motif scaffolding, symmetric
# oligomers, partial diffusion, fold conditioning -- is that same script with
# different keys set. So offering "all of it" means offering the keys, not
# writing five pipelines.
#
# Three places have to agree about every key: the panel that offers it, the
# validator that accepts it, and the command line that passes it on. Those are
# in two languages, so the table is written once here, served to the browser
# over /api/design/options, and read back by the command builder below. Adding a
# capability is one entry, and the panel grows a control for it untouched.
#
#     key     what it is called in spec["run"] -- camelCase, like the rest
#     flags   the Hydra override(s) it becomes; two where one idea is two keys
#     type    how to read the value, and which control the panel draws
#     group   which part of the panel it belongs in
#     modes   the protocols it is offered for; absent means all of them
#
# `model.*` from base.yaml is deliberately absent: those are the network's own
# dimensions and the sampler overwrites them from whichever checkpoint it loads
# (`self._conf[cat][key] = self.ckpt["config_dict"][cat][key]`), so setting them
# changes nothing and reads as if it should. `contig_settings.*` is scratch the
# contig parser fills in. Everything else a person can meaningfully set is here.

def _option(key, flags, type="float", group="sampling", label=None, help="", **extra):
    return {
        "key": key,
        "flags": (flags,) if isinstance(flags, str) else tuple(flags),
        "type": type, "group": group, "label": label or key, "help": help,
        **extra,
    }


# The potentials RFdiffusion actually implements, with the arguments each takes
# from its own constructor. Offered as examples rather than as a form: the
# string form is what the README uses, what every paper's methods section
# quotes, and what a user will paste in.
POTENTIAL_EXAMPLES = (
    "type:monomer_ROG,weight:1,min_dist:15",
    "type:binder_ROG,weight:1,min_dist:15",
    "type:dimer_ROG,weight:1,min_dist:15",
    "type:binder_ncontacts,weight:1,r_0:8,d_0:4",
    "type:interface_ncontacts,weight:1,r_0:8,d_0:6",
    "type:monomer_contacts,weight:1,r_0:8,d_0:2",
    "type:olig_contacts,weight_intra:1,weight_inter:0.1",
    "type:substrate_contacts,weight:1,r_0:8,d_0:2,s:1",
)

RFDIFFUSION_GROUPS = (
    {"id": "motif", "label": "Motif and sequence",
     "help": "Which parts of the input are held fixed, and which of them keep "
             "their sequence as well as their shape."},
    {"id": "sampling", "label": "Sampling",
     "help": "How the reverse diffusion is run. Time is almost exactly "
             "proportional to the step count."},
    {"id": "model", "label": "Model",
     "help": "Which checkpoint runs, and how the input is presented to it."},
    {"id": "symmetry", "label": "Symmetry",
     "help": "Sample a symmetric assembly. The contig length must divide by the "
             "number of chains the symmetry has."},
    {"id": "potentials", "label": "Guiding potentials",
     "help": "Extra forces applied during denoising -- compactness, contact "
             "counts, staying on a substrate."},
    {"id": "scaffold", "label": "Fold conditioning",
     "help": "Condition on a secondary-structure and adjacency description "
             "rather than on coordinates. Needs scaffold files made in advance "
             "by RFdiffusion's own helpers/make_secstruc_adj.py."},
)

RFDIFFUSION_OPTIONS = (
    # ---------------------------------------------------- motif and sequence
    _option("inpaintSeq", "contigmap.inpaint_seq", "list", "motif", "inpaint seq",
            "Motif residues to keep the shape of but not the identity of, so "
            "ProteinMPNN is free to choose them: A1-10/A12.",
            placeholder="A1-10/A12"),
    _option("inpaintStr", "contigmap.inpaint_str", "list", "motif", "inpaint str",
            "Motif residues whose sequence is kept but whose structure is "
            "rebuilt.", placeholder="A1-10"),
    _option("provideSeq", "contigmap.provide_seq", "list", "motif", "provide seq",
            "Residue ranges whose sequence is given to the model. Partial "
            "diffusion only -- RFdiffusion asserts on that.",
            placeholder="1-100", modes=("partial",)),
    _option("length", "contigmap.length", "text", "motif", "total length",
            "Pin the total length of the design, when the contig leaves it "
            "free: 80-100.", placeholder="80-100"),

    # -------------------------------------------------------------- sampling
    _option("steps", "diffuser.T", "int", "sampling", "steps",
            "Diffusion steps. The model was trained at 50; fewer is "
            "proportionally faster and proportionally rougher.",
            min=2, max=200, step=5, common=True),
    _option("partialT", "diffuser.partial_T", "int", "sampling", "partial",
            "Partial diffusion: noise the input only this far and denoise back, "
            "giving variations on a structure you already have. Must not exceed "
            "steps, and the contig must have no length ranges in it.",
            min=1, max=200, step=1, modes=("partial",)),
    _option("finalStep", "inference.final_step", "int", "sampling", "final step",
            "Stop the reverse process early. 1 runs it to the end.",
            min=1, max=200, step=1),
    _option("noiseScale", ("denoiser.noise_scale_ca", "denoiser.noise_scale_frame"),
            "float", "sampling", "variety",
            "How much randomness is left in during denoising. 0 is the binder "
            "protocol's recommendation and gives the best hit rate, at the cost "
            "of a batch that looks like one idea; raise it to explore.",
            min=0, max=2, step=0.1, common=True),
    _option("finalNoiseScale",
            ("denoiser.final_noise_scale_ca", "denoiser.final_noise_scale_frame"),
            "float", "sampling", "final variety",
            "Where the noise scale ends up, when the schedule below is not "
            "constant.", min=0, max=2, step=0.1),
    _option("noiseSchedule",
            ("denoiser.ca_noise_schedule_type", "denoiser.frame_noise_schedule_type"),
            "choice", "sampling", "noise schedule",
            "How the noise scale travels from its start to its final value.",
            choices=("constant", "linear")),
    _option("deterministic", "inference.deterministic", "bool", "sampling",
            "reproducible",
            "Seed every design from its own index, so the same job run twice "
            "gives the same backbones. RFdiffusion has no seed key of its own: "
            "this is the whole of what it offers."),
    _option("designStartNum", "inference.design_startnum", "int", "sampling",
            "start number",
            "Number the designs from here. With 'reproducible' on this is the "
            "seed, so it is how you get a different reproducible batch.",
            min=0, step=1),

    # ----------------------------------------------------------------- model
    _option("checkpoint", "inference.ckpt_override_path", "choice", "model",
            "checkpoint",
            "Override the checkpoint RFdiffusion would have chosen. "
            "ActiveSite is the one for small motifs of a few residues; "
            "Complex_beta gives more diverse binder topologies.",
            choices=(), resolve="model"),
    _option("recenter", "inference.recenter", "bool", "model", "recenter",
            "Centre the input on the motif before diffusing."),
    _option("radius", "inference.radius", "float", "model", "radius",
            "Neighbour radius used when only neighbours are modelled.",
            min=1, max=50, step=1),
    _option("modelOnlyNeighbors", "inference.model_only_neighbors", "bool", "model",
            "neighbours only",
            "For a large symmetric assembly, model only the neighbouring "
            "subunits. Much less memory; an approximation."),
    _option("alignMotif", "inference.align_motif", "bool", "model", "align motif",
            "Put the motif back on its input coordinates in the output file. "
            "Off means the design comes back in the model's own frame."),
    _option("writeTrajectory", "inference.write_trajectory", "bool", "model",
            "write trajectory",
            "Write the denoising trajectory to traj/. Only useful on the GPU "
            "machine -- proteinCAD never reads it."),
    _option("emptyCachePerDesign", "inference.empty_cache_per_design", "bool",
            "model", "free VRAM each design",
            "Empty the CUDA cache between designs. Slower, and the thing to "
            "turn on when a long batch runs out of GPU memory partway."),
    _option("sidechainInput", "preprocess.sidechain_input", "bool", "model",
            "sidechain input",
            "Show the model the motif's side chains, not just its backbone."),

    # -------------------------------------------------------------- symmetry
    _option("symmetry", "inference.symmetry", "text", "symmetry", "symmetry",
            "c2..cN for cyclic, d2..dN for dihedral, or tetrahedral, "
            "octahedral, icosahedral. The contig length must divide by the "
            "number of chains.", placeholder="c4", modes=("symmetry",)),
    _option("symmetricSelfCond", "inference.symmetric_self_cond", "bool",
            "symmetry", "symmetric self-conditioning",
            "Symmetrise the self-conditioning input as well as the structure.",
            modes=("symmetry",)),
    _option("cyclic", "inference.cyclic", "bool", "symmetry", "cyclise",
            "Close the chain into a cycle -- a head-to-tail peptide macrocycle.",
            ),
    _option("cycChains", "inference.cyc_chains", "text", "symmetry",
            "cyclic chains",
            "Which chains to cyclise, as lowercase letters run together: abc.",
            placeholder="a"),

    # ------------------------------------------------------------ potentials
    _option("guidingPotentials", "potentials.guiding_potentials", "qlist",
            "potentials", "potentials",
            "One per line. The forms RFdiffusion implements are listed below; "
            "every number in them is optional and falls back to the default in "
            "its constructor.", examples=POTENTIAL_EXAMPLES, multiline=True),
    _option("guideScale", "potentials.guide_scale", "float", "potentials",
            "guide scale", "How hard the potentials pull.",
            min=0, max=50, step=1),
    _option("guideDecay", "potentials.guide_decay", "choice", "potentials",
            "guide decay",
            "How the pull fades as the structure settles.",
            choices=("constant", "linear", "quadratic", "cubic")),
    _option("oligIntraAll", "potentials.olig_intra_all", "bool", "potentials",
            "contacts within chains",
            "Apply the oligomer contact potential inside every chain.",
            modes=("symmetry",)),
    _option("oligInterAll", "potentials.olig_inter_all", "bool", "potentials",
            "contacts between chains",
            "Apply it between every pair of chains.", modes=("symmetry",)),
    _option("oligCustomContact", "potentials.olig_custom_contact", "text",
            "potentials", "custom contacts",
            "Per-chain contact string, attractive or repulsive: A:B,A:r:C.",
            placeholder="A:B,A:r:C", modes=("symmetry",)),
    _option("substrate", "potentials.substrate", "text", "potentials", "substrate",
            "Residue name of a ligand in the input to keep contact with. The "
            "ligand has to be in the file -- tick 'include ligands' above.",
            placeholder="LLK"),

    # ------------------------------------------------------ fold conditioning
    _option("scaffoldGuided", "scaffoldguided.scaffoldguided", "bool", "scaffold",
            "fold conditioned",
            "Condition on secondary structure and adjacency instead of "
            "coordinates.", modes=("scaffold",)),
    _option("targetPdb", "scaffoldguided.target_pdb", "bool", "scaffold",
            "use the target",
            "Fold-condition against the target as well as the scaffold.",
            modes=("scaffold",)),
    _option("scaffoldDir", "scaffoldguided.scaffold_dir", "text", "scaffold",
            "scaffold dir",
            "Directory of _ss.pt and _adj.pt scaffold files, on the GPU "
            "machine.", placeholder="/content/scaffolds", modes=("scaffold",)),
    _option("scaffoldList", "scaffoldguided.scaffold_list", "text", "scaffold",
            "scaffold list",
            "A file naming which scaffolds in that directory to use.",
            modes=("scaffold",)),
    _option("targetSs", "scaffoldguided.target_ss", "text", "scaffold",
            "target ss", "The target's _ss.pt file.", modes=("scaffold",)),
    _option("targetAdj", "scaffoldguided.target_adj", "text", "scaffold",
            "target adj", "The target's _adj.pt file.", modes=("scaffold",)),
    _option("maskLoops", "scaffoldguided.mask_loops", "bool", "scaffold",
            "mask loops",
            "Hide the scaffold's loop lengths so they can change. Off keeps "
            "them exactly.", modes=("scaffold",)),
    _option("systematic", "scaffoldguided.systematic", "bool", "scaffold",
            "systematic",
            "Walk the scaffold list in order instead of sampling from it.",
            modes=("scaffold",)),
    _option("ssMask", "scaffoldguided.ss_mask", "int", "scaffold", "ss mask",
            "Blank this many residues at each end of every secondary-structure "
            "element.", min=0, max=20, step=1, modes=("scaffold",)),
    _option("sampledInsertion", "scaffoldguided.sampled_insertion", "text",
            "scaffold", "insertion", "Residues to insert into loops: 0-5.",
            placeholder="0-5", modes=("scaffold",)),
    _option("sampledN", "scaffoldguided.sampled_N", "text", "scaffold",
            "extra N", "Residues to add at the N terminus: 0-5.",
            placeholder="0-5", modes=("scaffold",)),
    _option("sampledC", "scaffoldguided.sampled_C", "text", "scaffold",
            "extra C", "Residues to add at the C terminus: 0-5.",
            placeholder="0-5", modes=("scaffold",)),
    _option("contigCrop", "scaffoldguided.contig_crop", "text", "scaffold",
            "contig crop", "Crop the target to these residues before "
            "conditioning on it.", placeholder="A1-100", modes=("scaffold",)),
)

OPTIONS_BY_KEY = {option["key"]: option for option in RFDIFFUSION_OPTIONS}

# The protocols, as the README names them. A mode is not a code path -- it is a
# set of defaults, a statement about what the job needs from the scene, and a
# filter on which of the options above are worth showing. The run itself is the
# same script every time.
RFDIFFUSION_MODES = (
    {"id": "binder", "label": "Binder",
     "help": "Build a new chain against the residues you picked. The default, "
             "and the only one that uses hotspots.",
     "target": "required", "hotspots": "required", "contigs": "binder",
     "models": ("rfdiffusion",),
     "groups": ("sampling", "model", "potentials", "motif"),
     "defaults": {"noiseScale": 0}},
    {"id": "motif", "label": "Motif scaffolding",
     "help": "Keep the picked residues exactly as they are and build a new "
             "protein around them. No hotspots: the motif is the point, not a "
             "surface to bind.",
     "target": "required", "hotspots": "ignored", "contigs": "motif",
     "models": ("rfdiffusion",),
     "groups": ("motif", "sampling", "model", "potentials"),
     "defaults": {}},
    {"id": "monomer", "label": "Unconditional",
     "help": "No target at all. Sample a protein of the given length from "
             "nothing, which is the honest way to see what the model does.",
     "target": "none", "hotspots": "ignored", "contigs": "length",
     "models": ("rfdiffusion",),
     "groups": ("sampling", "model", "potentials"),
     "defaults": {"guidingPotentials": ["type:monomer_ROG,weight:1,min_dist:15"],
                  "guideScale": 2, "guideDecay": "quadratic"}},
    {"id": "symmetry", "label": "Symmetric oligomer",
     "help": "Sample a symmetric assembly. The length below is the whole "
             "assembly and has to divide by the number of chains.",
     "target": "none", "hotspots": "ignored", "contigs": "length",
     "models": ("rfdiffusion",),
     "groups": ("symmetry", "potentials", "sampling", "model"),
     "defaults": {"symmetry": "c4",
                  "guidingPotentials": ["type:olig_contacts,weight_intra:1,weight_inter:0.1"],
                  "guideScale": 2, "guideDecay": "quadratic",
                  "oligIntraAll": True, "oligInterAll": True}},
    {"id": "partial", "label": "Partial diffusion",
     "help": "Variations on a structure you already have: load a design back "
             "in, pick it, and it is noised partway and denoised again. The "
             "contig is fixed-length by definition, so the length range is "
             "ignored.",
     "target": "required", "hotspots": "ignored", "contigs": "keep",
     "models": ("rfdiffusion",),
     "groups": ("sampling", "motif", "model", "potentials"),
     "defaults": {"partialT": 20}},
    {"id": "scaffold", "label": "Fold conditioned",
     "help": "Condition on a fold described as secondary structure and "
             "adjacency rather than on coordinates. Needs scaffold files made "
             "in advance on the GPU machine by RFdiffusion's "
             "helper_scripts/make_secstruc_adj.py.",
     "target": "optional", "hotspots": "optional", "contigs": "manual",
     "models": ("rfdiffusion",),
     "groups": ("scaffold", "sampling", "model", "potentials"),
     "defaults": {"scaffoldGuided": True, "targetPdb": True, "maskLoops": False}},
)

MODES_BY_ID = {mode["id"]: mode for mode in RFDIFFUSION_MODES}
DEFAULT_MODE = RFDIFFUSION_MODES[0]["id"]

# Ours, not the user's: every one of these is derived from the job or from where
# the worker keeps its files, and letting a free-form override reach them turns
# a typo into a run against the wrong molecule in the wrong directory.
OWNED_FLAGS = (
    "inference.output_prefix", "inference.input_pdb",
    "inference.model_directory_path", "inference.num_designs",
    "contigmap.contigs", "ppi.hotspot_res", "inference.trb_save_ckpt_path",
    "scaffoldguided.target_path",
    # Ours because it is derived from the job's scratch directory, and because
    # pointing it anywhere else puts Hydra's logs on a read-only filesystem.
    "hydra.run.dir",
    # Same reason, for the schedule cache RFdiffusion writes beside its source.
    "inference.schedule_directory_path",
)


def mode_spec(name) -> dict:
    return MODES_BY_ID.get(str(name or DEFAULT_MODE), MODES_BY_ID[DEFAULT_MODE])


def available_checkpoints(models_dir=None) -> list:
    """Every checkpoint that could be chosen, present or not.

    Offered whether or not it is downloaded: the worker fetches a missing one
    when a job asks for it, and a menu that hid them would make a capability
    look absent when it is thirty seconds away.

    `present` is only reported when there is a models directory to look in. The
    app's own server has none -- the weights are on the GPU machine -- and a
    flag that says False because nobody looked is worse than no flag.
    """
    names = sorted(WEIGHTS)
    if not models_dir:
        return [{"name": name} for name in names]
    path = Path(models_dir)
    here = {p.name for p in path.glob("*.pt")} if path.is_dir() else set()
    return [{"name": name, "present": name in here} for name in names]


def _plain(entry: dict) -> dict:
    """A table row with its tuples as lists, ready for JSON."""
    return {key: (list(value) if isinstance(value, tuple) else value)
            for key, value in entry.items()}


def describe_options(models_dir=None) -> dict:
    """The catalogue, as the browser gets it."""
    options = []
    for option in RFDIFFUSION_OPTIONS:
        copy = {k: (list(v) if isinstance(v, tuple) else v) for k, v in option.items()}
        if copy["key"] == "checkpoint":
            copy["choices"] = [entry["name"] for entry in available_checkpoints(models_dir)]
        options.append(copy)
    return {
        "modes": [{k: (list(v) if isinstance(v, tuple) else v) for k, v in mode.items()}
                  for mode in RFDIFFUSION_MODES],
        # What the panel's download buttons are, and what stage two needs. Here
        # rather than in JavaScript for the same reason as everything else in
        # this table: one definition, read by the panel and by the worker.
        "models": [dict(model) for model in MODELS],
        "fold_models": list(FOLD_MODELS),
        "groups": [dict(group) for group in RFDIFFUSION_GROUPS],
        "options": options,
        "checkpoints": available_checkpoints(models_dir),
        "potentials": list(POTENTIAL_EXAMPLES),
        "owned": list(OWNED_FLAGS),
        # Stage one has two engines, and they share nothing: different
        # protocols, different settings, different idea of what an input is. So
        # each carries its own catalogue here rather than the two being merged
        # into one list the panel would have to filter.
        #
        # RFdiffusion's also stays at the top level, unprefixed, because that is
        # where every copy of the panel written before there was a second engine
        # looks for it -- and the viewer is a folder of files somebody may be
        # serving a cached copy of.
        "default_engine": DEFAULT_ENGINE,
        "engines": [
            dict(_plain(ENGINES_BY_ID["rfdiffusion"]),
                 modes=[_plain(mode) for mode in RFDIFFUSION_MODES],
                 groups=[dict(group) for group in RFDIFFUSION_GROUPS],
                 options=options,
                 potentials=list(POTENTIAL_EXAMPLES),
                 checkpoints=available_checkpoints(models_dir)),
            dict(_plain(ENGINES_BY_ID["esm3"]), **esm3_describe_options(models_dir)),
        ],
    }


def _as_list(value) -> list:
    """A list, however it was written. The panel sends one string per line and
    a hand-written spec sends a list; both mean the same thing."""
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = re.split(r"[,\n]", str(value))
    return [str(item).strip() for item in items if str(item).strip()]


def _render_option(option: dict, value, models_dir=None) -> list:
    kind = option["type"]
    if kind == "bool":
        text = "True" if value else "False"
    elif kind == "int":
        text = str(int(value))
    elif kind == "float":
        number = float(value)
        # 1 and 1.0 are the same number and different strings. Hydra takes
        # either; the command line is shown to the user, so prefer the short one.
        text = str(int(number)) if number == int(number) else str(number)
    elif kind in ("list", "qlist"):
        items = _as_list(value)
        if not items:
            return []
        text = "[" + ",".join(f'"{i}"' if kind == "qlist" else i for i in items) + "]"
    else:
        text = str(value).strip()
        if not text:
            return []
        if option.get("resolve") == "model" and models_dir is not None:
            text = str(Path(models_dir) / Path(text).name)
    return [f"{flag}={text}" for flag in option["flags"]]


def hydra_overrides(run: dict, models_dir=None) -> list:
    """The catalogue settings in this job, as run_inference.py arguments.

    Only keys that are present are passed. Absence means "leave RFdiffusion's
    own default alone", which is the only way a table like this can stay honest
    about a config file it does not own.
    """
    out: list = []
    for option in RFDIFFUSION_OPTIONS:
        if option["key"] not in run:
            continue
        value = run[option["key"]]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        out += _render_option(option, value, models_dir)
    for extra in _as_list(run.get("extra") or []):
        out.append(extra)
    return out


def option_problems(run: dict) -> list:
    """Whatever would be rejected, said here rather than a minute into a run.

    Hydra's own complaint about a bad override is accurate and arrives after the
    model has loaded; a number out of range is not complained about at all.
    """
    problems = []
    for key, value in (run or {}).items():
        option = OPTIONS_BY_KEY.get(key)
        if option is None or value is None or value == "":
            continue
        kind = option["type"]
        if kind in ("int", "float"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                problems.append(f"{option['label']}: '{value}' is not a number")
                continue
            low, high = option.get("min"), option.get("max")
            if low is not None and number < low:
                problems.append(f"{option['label']} cannot be below {low}")
            if high is not None and number > high:
                problems.append(f"{option['label']} cannot be above {high}")
        elif kind == "choice" and option.get("choices") and str(value) not in option["choices"]:
            # The checkpoint menu is filled in from the models directory, so an
            # empty choices list means "anything named in WEIGHTS".
            if key != "checkpoint" or Path(str(value)).name not in WEIGHTS:
                problems.append(f"{option['label']}: '{value}' is not one of "
                                f"{', '.join(option['choices']) or 'the known values'}")
        elif kind in ("text", "list") and "\n" in str(value):
            # A line break would split one override into two arguments, the
            # second of which is not an override at all. Only the settings read
            # as a list may carry them.
            problems.append(f"{option['label']} cannot contain a line break")

    steps = run.get("steps")
    partial = run.get("partialT")
    if partial and steps and int(partial) > int(steps):
        # RFdiffusion asserts this, five frames deep, after loading the model.
        problems.append(f"partial ({partial}) cannot be above steps ({steps}) — "
                        "partial diffusion starts partway along the same schedule")
    if run.get("provideSeq") and not partial:
        problems.append("provide seq only works with partial diffusion — set a partial "
                        "step count, or clear it")

    for extra in _as_list(run.get("extra") or []):
        if "=" not in extra:
            problems.append(f"override '{extra}' is not key=value")
            continue
        flag = extra.split("=", 1)[0].strip()
        if not re.match(r"^[+~]?[A-Za-z_][\w.]*$", flag):
            problems.append(f"override '{extra}' does not start with a config key")
        elif flag.lstrip("+~") in OWNED_FLAGS:
            problems.append(f"override '{flag}' is set from the job itself — "
                            "setting it here would run against something else")
    return problems


def checkpoint_for(run: dict, hotspots) -> str:
    """Which checkpoint RFdiffusion will choose for this job.

    Mirrors the ladder in model_runners.py, in its order -- a different order
    picks a different model -- so the worker can have the file in place before
    the run rather than crashing a minute in on a path that is not there.
    """
    override = str(run.get("checkpoint") or "").strip()
    if override:
        return Path(override).name
    scaffolded = bool(run.get("scaffoldGuided"))
    if run.get("inpaintSeq") or run.get("provideSeq") or run.get("inpaintStr"):
        return "InpaintSeq_Fold_ckpt.pt" if scaffolded else "InpaintSeq_ckpt.pt"
    if hotspots and not scaffolded:
        return "Complex_base_ckpt.pt"
    if scaffolded:
        return "Complex_Fold_base_ckpt.pt"
    return "Base_ckpt.pt"


# ------------------------------------------- everything ESM3-open can do
#
# The same contract as the table above -- written once, served to the browser,
# read back by the thing that runs the model -- for a model whose inputs are
# nothing like RFdiffusion's. See the note beside ESM3_MODEL for why.
#
# What a track is: one description of the same protein, position by position.
# ESM3 holds five of them at once and any of them may be partly given and partly
# masked. Giving sequence and asking for structure is folding; giving structure
# and asking for sequence is inverse folding; giving neither and asking for both
# is design. One model, and the protocol is which tracks you fill in.

ESM3_TRACKS = (
    {"id": "sequence", "label": "sequence", "prompt": "text", "generates": True,
     "help": "Amino acids, one letter each, with _ for a position the model "
             "should choose. | separates chains."},
    {"id": "structure", "label": "structure", "prompt": "coordinates", "generates": True,
     "help": "Backbone coordinates. Given as a structure from the scene; "
             "produced as the design itself."},
    {"id": "secondary_structure", "label": "secondary structure", "prompt": "text",
     "generates": True,
     "help": "DSSP's eight states (GHITEBSC) per position, _ where it does not "
             "matter. The cheapest way to ask for a fold shape without drawing one."},
    {"id": "sasa", "label": "solvent accessibility", "prompt": "spans", "generates": True,
     "help": "How exposed each position should be, in square angstroms. Low "
             "values bury a position, high values put it on the surface."},
    {"id": "function", "label": "function", "prompt": "spans", "generates": True,
     "help": "InterPro accessions or keywords over residue ranges, as a "
             "statement about what a stretch of the protein is for."},
)

ESM3_TRACK_IDS = tuple(track["id"] for track in ESM3_TRACKS)

# The decode order each protocol starts from, as `track:steps:temperature`
# strings -- the same form the plan box takes, so what a mode brings with it and
# what a user types are the same thing written the same way.
#
# Sequence before structure, in every plan that does both. The order is not a
# preference: ESM3 conditions each pass on everything decoded before it, so
# deciding the residues first and then asking what they fold into is a different
# question from deciding a shape first and fitting residues to it -- and the
# first is the one whose answer can be checked, because a sequence is what the
# folding check in stage two takes.
ESM3_PLANS = {
    "generate": ("sequence:8:0.7", "structure:8:0.0"),
    "motif": ("sequence:8:0.7", "structure:8:0.0"),
    "inverse": ("sequence:8:0.5",),
    "predict": ("structure:8:0.0",),
    "resample": ("sequence:8:0.7", "structure:8:0.0"),
}


def _esm3_option(key, type="float", group="plan", label=None, help="", **extra):
    """One ESM3 setting. No `flags`: nothing here becomes a command line.

    The browser's control builder reads type, group, label, help and the range
    hints and never looks for flags, so the panel draws these with the same code
    it draws RFdiffusion's with.
    """
    return {
        "key": key, "type": type, "group": group,
        "label": label or key, "help": help, **extra,
    }


ESM3_GROUPS = (
    {"id": "plan", "label": "Decode plan",
     "help": "Which masked tracks to fill in, in what order, and how hard to "
             "work at each. This is the setting with no RFdiffusion equivalent."},
    {"id": "prompt", "label": "Track prompts",
     "help": "What to tell the model about the protein before it starts. Every "
             "one of these is optional, and every one of them narrows what comes "
             "back."},
    {"id": "motif", "label": "Motif",
     "help": "What the picked residues keep. Shape, identity, or both."},
    {"id": "sampling", "label": "Sampling",
     "help": "How the iterative decode is sampled. Temperature at zero makes a "
             "run reproducible from its seed."},
    {"id": "model", "label": "Model",
     "help": "Which weights answer. Only the open model runs locally."},
)

ESM3_OPTIONS = (
    # ------------------------------------------------------------------ plan
    _esm3_option("plan", "list", "plan", "plan",
                 "One decode pass per line, as track:steps:temperature. Steps "
                 "is how many iterations the masked positions of that track are "
                 "filled in over -- one step decodes everything at once, which "
                 "is fast and worse. Leave empty to use the protocol's own plan.",
                 multiline=True, placeholder="sequence:8:0.7\nstructure:8:0.0"),
    _esm3_option("numSteps", "int", "plan", "steps",
                 "Decode steps for a plan line that does not name its own. "
                 "Cannot exceed the number of masked positions, and is clamped "
                 "to it rather than failing.",
                 min=1, max=512, common=True),
    # --------------------------------------------------------------- sampling
    _esm3_option("temperature", "float", "sampling", "temp",
                 "How hot to sample a plan line that does not name its own. "
                 "Zero takes the most likely token every time, which is what "
                 "you want for structure and rarely for sequence.",
                 min=0.0, max=2.0, step=0.05, common=True),
    _esm3_option("topP", "float", "sampling", "top p",
                 "Sample only from the most likely tokens that together carry "
                 "this much probability. Unset leaves the whole distribution.",
                 min=0.0, max=1.0, step=0.05),
    _esm3_option("fraction", "float", "sampling", "resample",
                 "What fraction of the input to mask again before decoding. "
                 "Partial resampling only: this is the dial between a copy of "
                 "the input and something unrelated to it.",
                 min=0.01, max=1.0, step=0.05, modes=("resample",)),
    # ---------------------------------------------------------------- prompts
    _esm3_option("sequencePrompt", "text", "prompt", "sequence",
                 "A sequence with _ where the model should choose. Its length "
                 "sets the design's length and overrides the range above. | "
                 "starts another chain -- the open model was trained on single "
                 "chains, so treat a multi-chain prompt as an experiment rather "
                 "than a protocol.",
                 placeholder="___________MKTAYIAKQ___________"),
    _esm3_option("secondaryStructure", "text", "prompt", "ss8",
                 "One DSSP state per position: G H I T E B S C, and _ for "
                 "unconstrained. Must be as long as the design. This is how to "
                 "ask for a fold -- three helices, a four-strand sheet -- "
                 "without drawing one.",
                 placeholder="__HHHHHHHHHHH____EEEEE__"),
    _esm3_option("sasa", "list", "prompt", "sasa",
                 "Exposure targets, one range per line as first-last:value in "
                 "square angstroms. Burying a stretch is how you ask for a core.",
                 multiline=True, placeholder="12-24:5\n40-48:90"),
    _esm3_option("function", "list", "prompt", "function",
                 "What a stretch is for, one per line as first-last:term. The "
                 "term is an InterPro accession (IPR000719) or a keyword the "
                 "function tokeniser knows.",
                 multiline=True, placeholder="30-70:IPR000719"),
    # ------------------------------------------------------------------ motif
    _esm3_option("keepSequence", "bool", "motif", "keep identity",
                 "Hold the picked residues' own amino acids, as well as their "
                 "shape. Off leaves the model free to rechoose them, which is "
                 "the right answer when the motif is a shape rather than a site.",
                 modes=("motif", "resample")),
    _esm3_option("keepStructure", "bool", "motif", "keep shape",
                 "Hold the picked residues' coordinates. Off asks for a protein "
                 "that has those residues somewhere, in an arrangement of its "
                 "own choosing.",
                 modes=("motif", "resample")),
    # ------------------------------------------------------------------ model
    _esm3_option("variant", "text", "model", "weights",
                 "Which ESM3 weights to load. The open model by name, or a path "
                 "to a local copy of it.",
                 placeholder=ESM3_MODEL),
    _esm3_option("conditionOnCoordinatesOnly", "bool", "model", "coords only",
                 "Condition structure on the coordinates themselves rather than "
                 "on the structure tokens they were encoded to. Slower, and "
                 "closer to the input."),
)

ESM3_OPTIONS_BY_KEY = {option["key"]: option for option in ESM3_OPTIONS}

# The protocols, which here are statements about which tracks are given.
#
#     target     what the job needs from the scene
#     hotspots   whether the picked residues mean anything
#     prompt     which tracks the input structure fills in
#     generates  which tracks come back, for the panel to say so
ESM3_MODES = (
    {"id": "generate", "label": "De novo", "engine": "esm3",
     "help": "Nothing given but a length. The model writes a sequence and then "
             "folds it, so what comes back is a protein rather than a shape "
             "waiting for one.",
     "target": "none", "hotspots": "ignored", "models": ("esm3",),
     "prompt": (), "generates": ("sequence", "structure"),
     "groups": ("plan", "prompt", "sampling", "model"),
     "defaults": {}},
    {"id": "motif", "label": "Motif scaffolding", "engine": "esm3",
     "help": "Keep the residues you picked and build a protein around them. "
             "The ESM3 answer to motif scaffolding: the motif is given on the "
             "sequence and structure tracks and everything else is masked.",
     "target": "required", "hotspots": "required", "models": ("esm3",),
     "prompt": ("sequence", "structure"), "generates": ("sequence", "structure"),
     "groups": ("motif", "plan", "prompt", "sampling", "model"),
     "defaults": {"keepSequence": True, "keepStructure": True}},
    {"id": "inverse", "label": "Inverse folding", "engine": "esm3",
     "help": "A sequence for a backbone you already have. Load a backbone -- "
             "one of RFdiffusion's, or anything else -- pick it, and ask what "
             "sequence folds to it.",
     "target": "required", "hotspots": "optional", "models": ("esm3",),
     "prompt": ("structure",), "generates": ("sequence",), "subject": True,
     "groups": ("plan", "sampling", "prompt", "model"),
     "defaults": {}},
    {"id": "predict", "label": "Structure prediction", "engine": "esm3",
     "help": "A structure for a sequence. Type the sequence into the prompt "
             "below, or pick a structure to take its sequence from.",
     "target": "optional", "hotspots": "ignored", "models": ("esm3",),
     "prompt": ("sequence",), "generates": ("structure",), "subject": True,
     "groups": ("prompt", "plan", "sampling", "model"),
     "defaults": {}},
    {"id": "resample", "label": "Partial resample", "engine": "esm3",
     "help": "Variations on a protein you already have: part of it is masked "
             "again and decoded. The ESM3 counterpart of partial diffusion, and "
             "the dial is the fraction masked.",
     "target": "required", "hotspots": "optional", "models": ("esm3",),
     "prompt": ("sequence", "structure"), "generates": ("sequence", "structure"),
     "subject": True,
     "groups": ("sampling", "plan", "motif", "prompt", "model"),
     "defaults": {"fraction": 0.3, "keepSequence": True, "keepStructure": True}},
)

# `subject` above marks the protocols where the structure the job carries *is*
# the thing being redesigned, rather than something the design sits against.
#
# The difference only shows up in stage two, and it shows up as a silent wrong
# answer: a sequence-design job is built by joining the design to the target it
# was run against, which is right for a binder and duplicates the molecule for
# an inverse-folding run -- ProteinMPNN would then design one protein in the
# presence of a second copy of itself, which is a different and much easier
# question than the one being asked.

ESM3_MODES_BY_ID = {mode["id"]: mode for mode in ESM3_MODES}
ESM3_DEFAULT_MODE = ESM3_MODES[0]["id"]

# Which generator draws the backbone. `kind` already says which stage a job is;
# this says which model answers stage one, and stage two does not have or need
# one -- a backbone is a backbone whichever model drew it, which is what lets an
# ESM3 design go through the same sequence design and folding check.
ENGINES = (
    {"id": "rfdiffusion", "label": "RFdiffusion",
     "help": "Denoises coordinates against the residues you picked. The binder "
             "protocol, and five others.",
     "kinds": ("binder",), "models": ("rfdiffusion",)},
    {"id": "esm3", "label": "ESM3-open",
     "help": "A masked generative model over sequence, structure, secondary "
             "structure, exposure and function. Writes sequence and structure "
             "together.",
     "kinds": ("binder",), "models": ("esm3",)},
)

ENGINES_BY_ID = {engine["id"]: engine for engine in ENGINES}
DEFAULT_ENGINE = ENGINES[0]["id"]


def engine_of(spec) -> str:
    """Which backbone generator this job is for.

    Absent means RFdiffusion, because every spec written before there was a
    second engine is one of its jobs and has to keep running unchanged.
    """
    name = str((spec or {}).get("engine") or "").strip().lower()
    return name if name in ENGINES_BY_ID else DEFAULT_ENGINE


def esm3_mode_spec(name) -> dict:
    return ESM3_MODES_BY_ID.get(str(name or ESM3_DEFAULT_MODE),
                                ESM3_MODES_BY_ID[ESM3_DEFAULT_MODE])


def modes_for_engine(name) -> tuple:
    """The protocol table belonging to one engine."""
    return ESM3_MODES if name == "esm3" else RFDIFFUSION_MODES


def mode_spec_for(spec) -> dict:
    """The mode entry for a spec, from whichever engine's table owns it.

    The two tables share some ids -- `motif` means motif scaffolding in both --
    so which table is read is decided by the engine and never by the id.
    """
    if engine_of(spec) == "esm3":
        return esm3_mode_spec((spec or {}).get("mode"))
    return mode_spec((spec or {}).get("mode"))


# ---------------------------------------------------- the plan, as a plan

SS8_STATES = "GHITEBSC"


def parse_plan_step(text: str, defaults: dict | None = None) -> dict:
    """One `track:steps:temperature` line, as a dict.

    Steps and temperature may be left off and are filled in from the run's own
    defaults, which is what makes a bare `structure` a usable line.
    """
    defaults = defaults or {}
    parts = [part.strip() for part in str(text).split(":")]
    track = parts[0]
    if track not in ESM3_TRACK_IDS:
        raise ValueError(f"'{track}' is not a track: {', '.join(ESM3_TRACK_IDS)}")
    step = {"track": track}
    if len(parts) > 1 and parts[1]:
        step["num_steps"] = int(float(parts[1]))
    elif defaults.get("numSteps"):
        step["num_steps"] = int(defaults["numSteps"])
    else:
        step["num_steps"] = 8
    if len(parts) > 2 and parts[2]:
        step["temperature"] = float(parts[2])
    elif defaults.get("temperature") is not None:
        step["temperature"] = float(defaults["temperature"])
    if len(parts) > 3:
        raise ValueError(f"'{text}' has more than track:steps:temperature in it")
    if step["num_steps"] < 1:
        raise ValueError(f"'{text}' asks for fewer than one step")
    if step.get("temperature", 0) < 0:
        raise ValueError(f"'{text}' asks for a negative temperature")
    return step


def esm3_plan(spec: dict) -> list:
    """The decode passes this job will actually make, in order.

    Resolved here rather than in the model script so that the panel can show it,
    the validator can reject it, and the job can record what it did -- the same
    reason RFdiffusion's command line is built on this side.
    """
    run = (spec or {}).get("run") or {}
    mode = esm3_mode_spec((spec or {}).get("mode"))
    written = _as_list(run.get("plan") or [])
    lines = written or ESM3_PLANS.get(mode["id"]) or ESM3_PLANS["generate"]
    return [parse_plan_step(line, run) for line in lines]


def _spans(values, what: str, numeric: bool) -> list:
    """`first-last:value` lines, as (first, last, value).

    Shared by the exposure and function prompts because they are the same
    shape: a residue range, and something to say about it.
    """
    out = []
    for entry in _as_list(values):
        head, _, tail = str(entry).partition(":")
        if not tail.strip():
            raise ValueError(f"{what} '{entry}' needs first-last:value")
        first, dash, last = head.partition("-")
        try:
            low = int(first)
            high = int(last) if dash else low
        except ValueError:
            raise ValueError(f"{what} '{entry}' does not start with a residue range") from None
        if low < 1 or high < low:
            raise ValueError(f"{what} '{entry}' is not a usable range")
        value = tail.strip()
        if numeric:
            try:
                value = float(value)
            except ValueError:
                raise ValueError(f"{what} '{entry}' needs a number after the colon") from None
            if value < 0:
                raise ValueError(f"{what} '{entry}' cannot be negative")
        out.append((low, high, value))
    return out


def esm3_option_problems(run: dict) -> list:
    """Whatever would be rejected, said before the weights are loaded.

    Loading ESM3 is most of a minute, and a prompt that is one position too long
    for the design fails after it -- inside the tokeniser, naming a tensor
    shape. Every check here is one of those, moved forward.
    """
    problems = []
    for key, value in (run or {}).items():
        option = ESM3_OPTIONS_BY_KEY.get(key)
        if option is None or value is None or value == "":
            continue
        kind = option["type"]
        if kind in ("int", "float"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                problems.append(f"{option['label']}: '{value}' is not a number")
                continue
            low, high = option.get("min"), option.get("max")
            if low is not None and number < low:
                problems.append(f"{option['label']} cannot be below {low}")
            if high is not None and number > high:
                problems.append(f"{option['label']} cannot be above {high}")

    for line in _as_list((run or {}).get("plan") or []):
        try:
            parse_plan_step(line, run or {})
        except ValueError as error:
            problems.append(f"plan: {error}")

    ss8 = str((run or {}).get("secondaryStructure") or "").strip().upper()
    if ss8:
        bad = sorted({c for c in ss8 if c not in SS8_STATES + "_"})
        if bad:
            problems.append(f"ss8: '{''.join(bad)}' is not one of {SS8_STATES} or _")

    sequence = str((run or {}).get("sequencePrompt") or "").strip().upper()
    if sequence:
        allowed = set(THREE_LETTER) | {"_", "|", "X"}
        bad = sorted({c for c in sequence if c not in allowed})
        if bad:
            problems.append(f"sequence prompt: '{''.join(bad)}' is not an amino acid, _ or |")
        if ss8 and len(ss8) != len(sequence.replace("|", "")):
            problems.append(f"ss8 is {len(ss8)} long and the sequence prompt is "
                            f"{len(sequence.replace('|', ''))} — they describe the same positions")

    for key, what, numeric in (("sasa", "sasa", True), ("function", "function", False)):
        try:
            _spans((run or {}).get(key) or [], what, numeric)
        except ValueError as error:
            problems.append(str(error))

    return problems


def esm3_describe_options(models_dir=None) -> dict:
    """The ESM3 catalogue, as the browser gets it."""
    return {
        "modes": [{k: (list(v) if isinstance(v, tuple) else v) for k, v in mode.items()}
                  for mode in ESM3_MODES],
        "groups": [dict(group) for group in ESM3_GROUPS],
        "options": [{k: (list(v) if isinstance(v, tuple) else v) for k, v in option.items()}
                    for option in ESM3_OPTIONS],
        "tracks": [dict(track) for track in ESM3_TRACKS],
        "plans": {mode: list(plan) for mode, plan in ESM3_PLANS.items()},
        "models": ["esm3"],
        "licence": ESM3_LICENCE,
        "default_model": ESM3_MODEL,
    }


class RFdiffusionGenerator(Generator):
    """Runs RFdiffusion's inference script over the spec.

    The spec already carries everything the CLI needs: a cropped target in the
    scene's coordinates, hotspot residues, and a contig string with the binder
    length range. Check the flag names against the version you installed -- they
    have moved between releases.
    """

    name = "rfdiffusion"

    def __init__(self, repo: str, python: str = sys.executable, extra: list | None = None,
                 binder_defaults: bool = True):
        self.repo = Path(repo)
        self.python = python
        self.extra = extra or []
        self.binder_defaults = binder_defaults
        self._report: dict | None = None
        # Deliberately not fatal. If this process exits, the tunnel is left with
        # nothing behind it and the app sees an opaque gateway error instead of
        # the actual problem. Better to serve, and report it properly.
        if not (self.repo / "scripts" / "run_inference.py").is_file():
            print(f"! no RFdiffusion at {self.repo} — jobs will fail until it is installed",
                  flush=True)

    def report(self) -> dict | None:
        """The last preflight, or None if none has finished yet.

        /health uses this rather than diagnose(): a probe imports torch, dgl and
        RFdiffusion and takes the better part of a minute, and a liveness check
        that hangs for a minute is worse than useless -- it makes a working
        worker look dead.
        """
        return self._report

    def diagnose(self) -> dict:
        """Preflight: the things that make run_inference.py exit 1 immediately.

        Packages are probed in `self.python` with RFdiffusion's own paths in
        place, so this reports on the environment that will really run the model
        rather than on whatever the worker happens to be running under.

        Always fresh. That is what lets a pip install in another Colab cell take
        effect on the next design without restarting the worker -- and so
        without changing the tunnel URL or the token.
        """
        models = self.repo / "models"
        weights = sorted(p.name for p in models.glob("*.pt")) if models.is_dir() else []
        report = {
            "repo": str(self.repo),
            "interpreter": self.python,
            "script": (self.repo / "scripts" / "run_inference.py").is_file(),
            "models_dir": models.is_dir(),
            "weights": weights,
        }
        report.update(probe(self.python, self.repo))

        problems = []
        if report.get("error"):
            problems.append(report["error"])
        if not report["script"]:
            problems.append(f"no RFdiffusion checkout at {self.repo} — "
                            "run: python colab_worker.py setup")
        if not weights:
            problems.append("no .pt weights in models/ — run: python colab_worker.py setup")
        if not report.get("cuda"):
            problems.append("no CUDA device — set Runtime > Change runtime type > GPU")

        # Collapse the import failures. On an unbuilt environment every one of
        # them fails at once, and eleven ModuleNotFoundErrors in the app's error
        # panel bury the single instruction that fixes all of them.
        modules = report.get("modules") or {}
        broken = {name: state for name, state in modules.items() if state != "ok"}
        if broken and len(broken) == len(modules):
            problems.append("none of RFdiffusion's dependencies are importable — "
                            "run: python colab_worker.py setup")
        elif broken:
            for name, state in list(broken.items())[:3]:
                problems.append(f"cannot import {name} ({state})")
            problems.append("run: python colab_worker.py setup — it installs these "
                            "and reports anything it cannot fix")

        report["problems"] = problems
        self._report = report
        return report

    @staticmethod
    def check_against_target(pdb_text: str, contigs: str, hotspots: list) -> list:
        """Do the contigs and hotspots describe the file being sent with them?

        RFdiffusion reads a chain as a single leading character -- `subcon[0]`
        for contigs, `(i[0], int(i[1:]))` for hotspots -- so a two-character
        chain name is not merely wrong, it is a ValueError raised a minute into
        the run, five frames deep in its own contig parser, naming a fragment of
        the chain id. mmCIF chain names like `BL` are ordinary; 7CGO has 219.

        Checked here in milliseconds, against the PDB itself, so the answer is
        about this job rather than about Python's int().
        """
        chains, residues = set(), set()
        for line in pdb_text.splitlines():
            if line.startswith(("ATOM", "HETATM")) and len(line) > 26:
                chains.add(line[21])
                residues.add(line[21] + line[22:26].strip())

        problems = []
        known = ", ".join(sorted(chains)) or "none"
        # A block is a whitespace-separated piece; within it, `/`-separated
        # fragments of one chain, ending in `0` for the chain break.
        for block in (contigs or "").split():
            for subcon in block.split("/"):
                if not subcon or subcon == "0":
                    continue                    # the chain break between blocks
                # RFdiffusion decides what a fragment *is* with one test --
                # `subcon[0].isalpha()` in contigs.py -- and anything failing it
                # is read as a number of residues to generate, whatever was
                # meant. So a chain whose name is a digit does not fail as a
                # missing chain; it fails as an impossible length.
                if not subcon[0].isalpha():
                    span = re.match(r"^(\d+)-(\d+)$", subcon)
                    if span and int(span.group(1)) > int(span.group(2)):
                        problems.append(
                            f"contig '{subcon}' reads as 'generate between {span.group(1)} and "
                            f"{span.group(2)} residues', which is impossible. RFdiffusion only "
                            f"treats a fragment as a chain when it starts with a letter, so this "
                            f"is what a chain called '{subcon[0]}' becomes")
                    elif not span and not subcon.isdigit():
                        problems.append(
                            f"contig '{subcon}' is neither a length range nor a chain fragment")
                    continue
                match = re.match(r"^([A-Za-z]+)(\d+)-(\d+)$", subcon)
                if not match:
                    continue  # a form not judged here
                chain, first, last = match.group(1), int(match.group(2)), int(match.group(3))
                if len(chain) > 1:
                    problems.append(
                        f"contig '{subcon}' names chain '{chain}', but RFdiffusion reads only "
                        f"the first character as the chain and then fails on "
                        f"'{chain[1:]}{first}'")
                    continue
                if chain not in chains:
                    problems.append(f"contig '{subcon}' names chain '{chain}', which is not in "
                                    f"the target (it has: {known})")
                    continue
                # Every number in the range has to exist. An experimental
                # structure is missing its disordered loops, so a range written
                # end-to-end across one names residues that were never there --
                # and RFdiffusion asserts on the first of them.
                absent = [n for n in range(first, last + 1) if f"{chain}{n}" not in residues]
                if absent:
                    shown = ", ".join(f"{chain}{n}" for n in absent[:3])
                    more = f" (+{len(absent) - 3} more)" if len(absent) > 3 else ""
                    problems.append(
                        f"contig '{subcon}' covers {shown}{more}, which {'are' if len(absent) > 1 else 'is'}"
                        f" not in the target — split the range around the gap")

        for spot in hotspots or []:
            text = str(spot)
            match = re.match(r"^([A-Za-z]+)(\d+)$", text)
            if match and len(match.group(1)) > 1:
                problems.append(f"hotspot '{text}' names a multi-character chain, which "
                                "RFdiffusion cannot read")
            elif not match:
                # `(i[0], int(i[1:]))` is how RFdiffusion splits these, so the
                # chain is whatever the first character is -- and a digit there
                # cannot be told apart from the residue number behind it.
                problems.append(
                    f"hotspot '{text}' is not a single-letter chain followed by a residue number"
                    + (f"; '{text[0]}' is not a usable chain name" if text[:1].isdigit() else ""))
            elif text not in residues:
                problems.append(f"hotspot '{text}' is not in the target (chains: {known})")
        return problems

    atom_lines = staticmethod(atom_lines)
    centroid = staticmethod(centroid)

    def place_on_target(self, design_pdb: str, target_pdb: str):
        """Put a design back where it was designed to go.

        RFdiffusion centres its input on the kept motif's centre of mass and
        never undoes it -- `xyz = xyz - self.motif_com` in diffusion.py, and
        motif_com appears nowhere else -- so a design comes back rigidly
        displaced, floating near the origin instead of sitting on the residues it
        was built against. Since the motif is held at its input coordinates
        throughout, that displacement is a pure translation, and the two
        centroids recover it exactly.

        Exactly is checked rather than assumed: if the motif does not land back
        on the target, the design is returned untouched and says so.

        The motif is matched to the target by chain and residue number rather
        than by position in the file. RFdiffusion keeps both for the blocks it
        was given -- `self.idx_pdb += [contig_ref[1] ...]` and `self.chain_idx +=
        [list(chain_ids)[0]] ...` -- so the names line the two files up directly.
        Matching by order instead meant the two lists had to come out the same
        length, and a single residue missing a CA anywhere in a five-fragment
        crop was enough to refuse a design that was perfectly placeable.

        What comes back is the binder alone. The target is already in the scene
        -- a second copy of it would only fight with the first.
        """
        target_ca = {}
        for line in atom_lines(target_pdb):
            if line[12:16].strip() == "CA":
                target_ca[residue_key(line)] = coordinates(line)

        binder, motif = split_marked(design_pdb)
        pairs = [(coordinates(line), target_ca[residue_key(line)])
                 for line in motif
                 if line[12:16].strip() == "CA" and residue_key(line) in target_ca]

        if not binder or len(pairs) < 3:
            return design_pdb, {"placed": False, "why": (
                f"only {len(pairs)} of the design's target residues could be matched "
                f"back to the file it was sent")}

        here, there = centroid([b for _, b in pairs]), centroid([a for a, _ in pairs])
        shift = tuple(here[i] - there[i] for i in range(3))
        off = max(sum((a[i] + shift[i] - b[i]) ** 2 for i in range(3)) ** 0.5
                  for a, b in pairs)
        if off > 1.0:
            return design_pdb, {"placed": False, "why": (
                f"the target would land {off:.1f} A from where it was sent, so this is "
                "not the simple offset it should be")}

        moved = [with_coordinates(line, [coordinates(line)[i] + shift[i] for i in range(3)])
                 for line in binder]
        return "\n".join(moved) + "\nEND\n", {
            "placed": True,
            "residues": len({residue_key(line) for line in binder}),
            "matched": len(pairs),
            "fit_to_target": round(off, 3),
        }

    def fetch_checkpoint(self, run: dict, hotspots, models_dir: Path, job) -> str:
        """Make sure the checkpoint this job will pick is on disk.

        setup downloads the two a binder run chooses between, because fetching
        all eight is 3.9 GB of first cell for capabilities most jobs never reach.
        The other six are what the other protocols need, and each is 484 MB --
        small enough to fetch when something actually asks for it. Without this
        the whole of motif inpainting and fold conditioning fails a minute into
        a run, on a path.

        Returns a message if it could not be had, or "" if it is there.
        """
        name = checkpoint_for(run, hotspots)
        path = models_dir / name
        if path.is_file():
            return ""
        url = WEIGHTS.get(name)
        if not url:
            return (f"this job needs {name}, which is not a checkpoint this worker knows "
                    f"about. Put it in {models_dir} yourself, or pick another model.")
        job["stage"] = f"fetching {name} (484 MB, once)"
        print(f"[proteincad] {job['stage']}", flush=True)
        try:
            models_dir.mkdir(parents=True, exist_ok=True)
            download(url, path)
        except Exception as error:               # noqa: BLE001 - reported, not raised
            return (f"this job needs {name} and it could not be downloaded: {error}\n"
                    f"Fetch it yourself with: python colab_worker.py setup --weights {name}")
        return ""

    def generate(self, spec, job):
        report = self.diagnose()
        if report["problems"]:
            job["error"] = ("cannot run RFdiffusion here:\n"
                            + "\n".join("  ! " + problem for problem in report["problems"])
                            + f"\nlooked in {self.repo}, running {self.python}")
            return

        target = spec.get("target", {})
        binder = spec.get("binder", {})
        run = dict(spec.get("run", {}) or {})
        mode = mode_spec(spec.get("mode"))

        # Said now rather than a minute into a run. Hydra's own complaint about a
        # rejected override is accurate and arrives after the model has loaded;
        # a number out of range it never complains about at all.
        bad = option_problems(run)
        if bad:
            job["error"] = ("these settings would be rejected:\n"
                            + "\n".join("  ! " + problem for problem in bad))
            return

        work = Path(tempfile.mkdtemp(prefix="proteincad_"))
        try:
            models_dir = self.repo / "models"
            pdb_text = target.get("pdb", "") or ""
            # A job with no target is not a broken job -- an unconditional
            # monomer and a symmetric oligomer are both built from nothing but a
            # length. RFdiffusion falls back to its own bundled example when
            # input_pdb is null, so the flag is dropped rather than pointed at an
            # empty file.
            target_pdb = None
            if pdb_text.strip() and mode["target"] != "none":
                target_pdb = work / "target.pdb"
                target_pdb.write_text(pdb_text)
            out_prefix = work / "out" / "design"
            out_prefix.parent.mkdir(parents=True, exist_ok=True)
            # Made here rather than left to RFdiffusion, which reaches it by
            # `os.mkdir` -- one level only, so a missing parent is an error
            # rather than a directory.
            schedules = work / "schedules"
            schedules.mkdir(parents=True, exist_ok=True)

            contigs = str(binder.get("contigs") or "").strip()
            if not contigs and mode["contigs"] != "manual":
                low = int(binder.get("lengthMin", 60))
                high = int(binder.get("lengthMax", low))
                contigs = f"{low}-{high}"
            # Hotspots are the binder protocol's instrument. Sending them in a
            # mode that is not about an interface would silently select the
            # complex checkpoint, which is a different model answering a
            # different question.
            spots = list(target.get("hotspots", []) or []) if mode["hotspots"] in (
                "required", "optional") else []

            mismatched = self.check_against_target(pdb_text, contigs, spots)
            if mismatched:
                job["error"] = ("the job does not match the target sent with it:\n"
                                + "\n".join("  ! " + problem for problem in mismatched))
                return

            missing = self.fetch_checkpoint(run, spots, models_dir, job)
            if missing:
                job["error"] = missing
                return

            command = [
                self.python, str(self.repo / "scripts" / "run_inference.py"),
                f"inference.output_prefix={out_prefix}",
                f"inference.num_designs={int(run.get('numDesigns', 1) or 1)}",
                # Hydra makes an `outputs/<date>/<time>` directory for its own
                # logs, relative to the working directory -- which is the
                # checkout. That is fine on Colab, where the checkout is
                # writable, and fatal in a container with a read-only
                # filesystem: OSError: [Errno 30] Read-only file system:
                # 'outputs'. Pointing it at this job's scratch costs nothing
                # anywhere and is tidier everywhere -- the logs end up beside
                # the run they belong to instead of accumulating in the repo.
                f"hydra.run.dir={work / 'hydra'}",
                # The second thing RFdiffusion writes next to its own source.
                # It caches the IGSO3 schedules it computes -- minutes of work,
                # worth keeping -- in `<repo>/schedules`, creating the
                # directory if it is not there and writing a pickle per
                # (T, omega, sigma) combination into it. Three writes, all into
                # a checkout that is read-only in the container the hosted
                # deployment runs the model in:
                #
                #   OSError: [Errno 30] Read-only file system:
                #     '.../rfdiffusion/inference/../../schedules'
                #
                # RFdiffusion offers the key rather than making us mount over
                # the path, and reads it before falling back to its own source
                # directory, so this is its own answer to the question.
                f"inference.schedule_directory_path={schedules}",
                # Otherwise resolved relative to the package, which only works
                # when the weights sit inside the checkout we happen to be using.
                f"inference.model_directory_path={models_dir}",
            ]
            if target_pdb is not None:
                command.append(f"inference.input_pdb={target_pdb}")
                if run.get("scaffoldGuided") and run.get("targetPdb"):
                    # Fold conditioning reads the target from its own key, and
                    # the path is ours to fill in: it is the file we just wrote.
                    command.append(f"scaffoldguided.target_path={target_pdb}")
            if contigs:
                command.append(f"contigmap.contigs=[{contigs}]")
            if spots:
                command.append(f"ppi.hotspot_res=[{','.join(spots)}]")

            # Noise scale is the diversity dial, and it is not obvious which way
            # to turn it. Zero is what the binder-design protocol recommends --
            # it raises the in-silico success rate -- but it also makes the
            # reverse process deterministic given its starting point, so a batch
            # comes back looking like variations on one idea. Raise it for range,
            # lower it for hit rate. Only a default: whatever the job says wins.
            if run.get("noiseScale") is None and self.binder_defaults and mode["id"] == "binder":
                run["noiseScale"] = 0
            # Everything else is the catalogue, which is also what the panel drew
            # its controls from -- so a knob that is offered is a knob that is
            # passed, without a line here per knob.
            command += hydra_overrides(run, models_dir)
            command.extend(self.extra)

            job["log"] = " ".join(command)
            print("[proteincad] " + job["log"], flush=True)

            process = subprocess.Popen(
                command, cwd=str(self.repo), env=environment(self.repo),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            seen = set()
            output: list = []

            def collect():
                """Pick up designs as they land, so the viewer fills in as the run
                goes rather than all at once at the end.

                A .pdb is only taken once its .trb sibling exists. run_inference
                writes the trb immediately after the pdb, so that is a free
                signal the pdb is closed -- reading one mid-write would hand the
                viewer a truncated backbone. Trajectories go to a traj/
                subdirectory and so cannot be confused for results.
                """
                for path in sorted(out_prefix.parent.glob("design_*.pdb")):
                    if path.name in seen or not path.with_suffix(".trb").is_file():
                        continue
                    seen.add(path.name)
                    if target_pdb is None:
                        # Nothing to put it back on. A protocol that designs from
                        # nothing has no frame to return to, and the design is
                        # already in the only one it has.
                        pdb, placement = path.read_text(), {"placed": False,
                                                            "why": "designed from nothing"}
                    else:
                        pdb, placement = self.place_on_target(
                            path.read_text(), target_pdb.read_text())
                    if not placement["placed"] and target_pdb is not None:
                        print(f"[proteincad] {path.stem} could not be placed: "
                              f"{placement['why']}", flush=True)
                    job["designs"].append({
                        "name": path.stem,
                        "pdb": pdb,
                        "metrics": {"source": "rfdiffusion", **placement},
                    })
                    job["progress"] = len(job["designs"])

            while True:
                if job["cancel"]:
                    process.terminate()
                    return
                line = process.stdout.readline()
                if line:
                    text = line.rstrip()
                    if not any(noise in text for noise in self.NOISE):
                        self.note_progress(text, job, job.get("total", 1))
                        # A Timestep line carries the whole sequence behind the
                        # part worth reading; keep the front of it.
                        print(text[:200], flush=True)
                        output.append(text[:200])
                        del output[:-400]
                elif process.poll() is not None:
                    break
                collect()

            # The loop breaks out as soon as the process has gone, which can be
            # before the last design was ever looked for. Without this sweep the
            # final backbone of a run is silently dropped.
            collect()

            if process.returncode not in (0, None) and not job["designs"]:
                job["error"] = self.explain(process.returncode, output)
        finally:
            shutil.rmtree(work, ignore_errors=True)


    # torch prints this once per CUDA call made through an older API, which for
    # RFdiffusion is twice a diffusion step: hundreds of lines a run, enough to
    # push everything that matters out of both the notebook log and the tail kept
    # for reporting a failure. Nothing in it is ever actionable.
    NOISE = ("lazyInitCUDA is deprecated",)

    # Where a run has got to. Loading the model alone is the best part of a
    # minute, and without this the whole of it looks like nothing happening.
    STAGES = (
        ("Reading checkpoint", "loading the model"),
        ("Calculating IGSO3", "computing the noise schedule"),
        ("Using cached IGSO3", "loading the noise schedule"),
        ("Successful diffuser", "starting to sample"),
    )
    MAKING = re.compile(r"Making design\b.*_(\d+)\s*$")
    STEP = re.compile(r"Timestep (\d+),")

    def note_progress(self, text: str, job: dict, total: int) -> None:
        """Turn RFdiffusion's log into one line saying where the run is.

        Diffusion counts *down* -- `Timestep 50` is the first step, not the
        last -- so it is turned round here rather than shown raw.
        """
        for needle, stage in self.STAGES:
            if needle in text:
                job["stage"] = stage
                return
        making = self.MAKING.search(text)
        if making:
            job["design"] = int(making.group(1)) + 1
            job["first_t"] = None
            job["stage"] = f"design {job['design']} of {total}: starting"
            return
        step = self.STEP.search(text)
        if step:
            t = int(step.group(1))
            if not job.get("first_t"):
                job["first_t"] = t
            first = job["first_t"]
            job["stage"] = (f"design {job.get('design', 1)} of {total}: "
                            f"step {first - t + 1} of {first}")

    # Patterns worth translating, because the raw message buries the fix.
    SETUP = "run `python colab_worker.py setup` in a Colab cell"
    # Two needles can describe one fault, so they share a hint string and the
    # de-duplication below collapses them into a single line.
    PICKLE = ("torch 2.6 and newer refuse to unpickle RFdiffusion's checkpoints, which hold "
              "config objects rather than bare tensors. " + SETUP + ": it pins a torch version "
              "that does not, and sets the escape hatch for the ones that do.")
    NUMPY = "dgl's wheels are built against numpy 1 and numpy 2 is installed: !pip install 'numpy<2'"
    HINTS = (
        ("could not override", "RFdiffusion rejected a config override — this build does not have "
                              "that key. Restart the worker with --no-binder-defaults."),
        ("is not in struct", "RFdiffusion rejected a config override — see --no-binder-defaults."),
        ("no such file or directory: '/", "A path in the command does not exist on this machine."),
        ("weights only load failed", PICKLE),
        ("unpicklingerror", PICKLE),
        ("_array_api not found", NUMPY),
        ("numpy.core.multiarray failed to import", NUMPY),
        ("from 'collections'", "that is an ancient DGL, which pip falls back to when it cannot "
                               "find a real wheel. " + SETUP + " to get one from DGL's own index."),
        ("undefined symbol", "dgl and torch were built against different versions of each other. "
                             + SETUP + ": it pins the exact torch its libraries are named for."),
        ("libtorch", "dgl cannot find the torch build it was compiled against. " + SETUP + "."),
        ("out of memory", "The GPU ran out of memory — reduce the crop radius or the binder length."),
        ("contig", "The contig string was rejected. It is shown in the command above."),
    )

    def explain(self, returncode: int, output: list) -> str:
        """Exit codes are not diagnosable; the last lines of output are."""
        tail = [line for line in output[-25:] if line.strip()]
        joined = "\n".join(tail)
        # Deprecation warnings mention plenty of words that look like causes --
        # a FutureWarning from torch/cuda/__init__.py is not a CUDA problem.
        lowered = "\n".join(l for l in tail if "warning" not in l.lower()).lower()
        hints = [hint for needle, hint in self.HINTS if needle in lowered]

        missing = re.search(r"no module named ['\"]([\w.]+)['\"]", lowered)
        if missing:
            hints.append(f"{missing.group(1)} is not importable — {self.SETUP}, which installs "
                         "every dependency and says what it could not fix")

        parts = [f"run_inference.py exited with {returncode}"]
        if hints:
            parts.append("Likely cause: " + "\n  ".join(dict.fromkeys(hints)))
        # Kept separate from the hints above, which are read off this run's
        # output. These are standing facts about the machine, and they are what
        # the user acts on when the output itself is unrevealing.
        problems = self.diagnose()["problems"]
        if problems:
            parts.append("--- environment ---\n"
                         + "\n".join("  ! " + problem for problem in problems))
        if joined:
            parts.append("--- last output ---\n" + joined)
        return "\n".join(parts)


# ------------------------------------------------- stage two: sequence, fold


class ProteinMPNN:
    """Chooses the amino acids for a backbone.

    RFdiffusion returns a shape. Every residue in it is glycine, because the
    model was never asked to pick one, so what comes back is not yet a protein
    that could be ordered or expressed. ProteinMPNN is the step that picks --
    given the backbone *and* the target it sits against, so the interface
    residues are chosen for the surface they will actually touch.

    It costs seconds and a few megabytes, which is why it belongs in the loop
    rather than behind a separate opt-in.
    """

    def __init__(self, repo, python: str = sys.executable):
        self.repo = Path(repo)
        self.python = python

    @property
    def script(self) -> Path:
        return self.repo / "protein_mpnn_run.py"

    def problems(self) -> list:
        if not self.script.is_file():
            return [f"no ProteinMPNN at {self.repo} — run: python colab_worker.py setup"]
        return []

    def design(self, pdb_path: Path, chains: list, count: int, temperature: float,
               seed: int, work: Path, lengths: list | None = None) -> list:
        """Sequences for `chains`, everything else held as context. Best first.

        Several chains are as ordinary here as one: ProteinMPNN takes a
        space-separated list and designs all of them together, which is what
        makes it the right tool for a binder whose chain count is decided by the
        crop rather than by the user.
        """
        out = work / "mpnn"
        command = [
            self.python, str(self.script),
            "--pdb_path", str(pdb_path),
            # Designs these chains and keeps the rest fixed, which is exactly the
            # binder case: the target keeps its own sequence and is seen, not
            # rewritten.
            "--pdb_path_chains", " ".join(chains),
            "--out_folder", str(out),
            "--num_seq_per_target", str(count),
            "--sampling_temp", str(temperature),
            # ProteinMPNN reads 0 as "choose one for me", so a job left on the
            # default seed varies between runs where a backbone job would not.
            "--seed", str(seed),
            "--batch_size", "1",
        ]
        done = subprocess.run(command, cwd=str(self.repo), capture_output=True, text=True)
        fasta = out / "seqs" / (pdb_path.stem + ".fa")
        if not fasta.is_file():
            tail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-12:]
            raise SetupError("ProteinMPNN wrote no sequences:\n" + "\n".join(tail))
        return self.read_fasta(fasta.read_text(), lengths)

    @staticmethod
    def read_fasta(text: str, lengths: list | None = None) -> list:
        """(sequences, score) per design, best first -- one sequence per chain.

        ProteinMPNN writes one record per sample and opens the file with the
        input's own sequence for comparison -- that first record is not a design
        and is dropped. Score is the mean negative log likelihood of the
        sequence it chose, so lower is better.

        Chains within a record are joined by `/`. Current builds write only the
        designed ones, older builds wrote the context alongside, so when the
        count does not match the chains that were asked for, the pieces are
        picked out by length instead. Worth the lines: the failure otherwise is a
        sequence that quietly belongs to the target.
        """
        records, header, parts = [], None, []
        for line in text.splitlines():
            if line.startswith(">"):
                if header is not None:
                    records.append((header, "".join(parts)))
                header, parts = line[1:], []
            elif line.strip():
                parts.append(line.strip())
        if header is not None:
            records.append((header, "".join(parts)))

        designs = []
        for header, sequence in records[1:]:
            pieces = [piece for piece in sequence.split("/") if piece]
            if not pieces:
                continue
            if lengths and len(pieces) != len(lengths):
                wanted, picked = list(lengths), []
                for piece in pieces:
                    if len(piece) in wanted:
                        wanted.remove(len(piece))
                        picked.append(piece)
                if len(picked) == len(lengths):
                    pieces = picked
            score = re.search(r"\bscore=([-\d.]+)", header)
            designs.append((pieces, round(float(score.group(1)), 4) if score else None))
        designs.sort(key=lambda pair: (pair[1] is None, pair[1]))
        return designs


class NoFolder:
    """Stands in when no structure predictor is installed. Sequences still come
    back; only the check that they fold is missing, and it says so."""

    name = "none"

    def problems(self) -> list:
        return ["no structure predictor is installed on this worker"]

    def fold(self, sequences, work, note=None, stop=None):  # pragma: no cover - guarded
        raise SetupError("no structure predictor is installed")


# Asked of the interpreter that will fold, and asked the awkward way on purpose.
#
# `from transformers import EsmForProteinFolding` goes through the package's lazy
# loader, which catches whatever actually went wrong and re-raises it as
# "Could not import module 'EsmForProteinFolding'. Are this object's requirements
# defined correctly?" -- a sentence that names our model and discards the cause.
# Importing the module by its real path skips that, and walking __cause__ back
# turns the remaining chain into the answer.
FOLD_PROBE = r'''
import json, sys
out = {"python": ".".join(str(v) for v in sys.version_info[:3])}


def chain(error):
    links, seen = [], error
    while seen is not None and len(links) < 5:
        links.append("%s: %s" % (type(seen).__name__, seen))
        seen = seen.__cause__ or seen.__context__
    return " <- ".join(links)


try:
    import torch
    out["torch"] = torch.__version__
    out["cuda"] = bool(torch.cuda.is_available())
    if out["cuda"]:
        out["gpu"] = torch.cuda.get_device_name(0)
except BaseException as error:
    out["problem"] = "torch is not importable (%s)" % chain(error)
else:
    try:
        import transformers
        out["transformers"] = transformers.__version__
    except BaseException as error:
        out["problem"] = "transformers is not importable (%s)" % chain(error)
    else:
        try:
            from transformers.models.esm.modeling_esmfold import EsmForProteinFolding
            out["esmfold"] = "ok"
        except BaseException as error:
            out["problem"] = "ESMFold is not importable (%s)" % chain(error)
print("FOLD " + json.dumps(out))
'''

# Run in the model's own interpreter, one process for every sequence in the
# batch: loading ESMFold is most of the cost, so folding them one subprocess at
# a time would pay for it over and over.
FOLD_SCRIPT = r'''
import json, os, sys, torch
from transformers import AutoTokenizer
# By its real path, for the reason given beside FOLD_PROBE.
from transformers.models.esm.modeling_esmfold import EsmForProteinFolding

request = json.loads(open(sys.argv[1]).read())
device = "cuda" if torch.cuda.is_available() else "cpu"
# Said out loud because the two ways this step dies -- the card is full, or the
# kernel kills the process for taking the host's memory -- leave nothing behind
# that says which. The numbers before the attempt are the evidence.
where, free, total = "no GPU visible", 0, 0
if device == "cuda":
    free, total = torch.cuda.mem_get_info()
    where = "%.1f of %.1f GB free on the %s" % (
        free / 1e9, total / 1e9, torch.cuda.get_device_name(0))
try:
    where += ", %.1f GB of system memory free" % (
        os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES") / 1e9)
except (ValueError, OSError, AttributeError):
    pass
print("STAGE loading the structure predictor (%s)" % where, flush=True)

# Refused here rather than discovered two minutes in. The predictor needs about
# six gigabytes of card, and when it does not have them the failure arrives as
# an OOM on a two-megabyte allocation at the very end of loading -- which names
# the allocation that happened to be last and nothing about what took the rest.
if device == "cuda" and free < 5.5e9:
    holders = ""
    try:
        import subprocess
        holders = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30).stdout.strip().replace("\n", "; ")
    except Exception:
        pass
    raise SystemExit(
        "the GPU is full before loading starts: %.2f of %.2f GB free, and the predictor "
        "needs about 6. This is the card rather than system memory, so a high-RAM runtime "
        "does not help — something else is holding it: %s. Restarting the Colab runtime "
        "clears it." % (free / 1e9, total / 1e9,
                        holders or "nvidia-smi could not say what"))

tokenizer = AutoTokenizer.from_pretrained(request["model"])

# What must end up where: the ESM-2 stem in half precision, everything else in
# single. forward() copes with a half stem by exactly one line --
# `esm_s = esm_s.to(self.esm_s_combine.dtype)` -- and esm_s_combine is an
# attribute of the model, not of the trunk. Leave that parameter in half and the
# cast becomes a no-op, so half activations reach a single-precision trunk and
# it fails several layers down looking like anything but a precision problem.
#
# How to get there depends on the memory there is. The checkpoint is about
# 8.4 GB and the model built from it is another copy, so the published recipe --
# load single, halve the stem -- needs more than a plain Colab session has and
# the kernel kills it partway. Below that, the model is loaded half and
# everything except the stem is brought back up, which costs the trunk's weights
# a rounding to half and back. That is a real if small loss, so it is only done
# when the alternative is not finishing at all, and it says which it did.
free = 0
try:
    free = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
except (ValueError, OSError, AttributeError):
    pass
lean = 0 < free < 14e9

if lean:
    print("STAGE loading in half precision; there is not room for the full one", flush=True)
    model = EsmForProteinFolding.from_pretrained(
        request["model"], low_cpu_mem_usage=True, torch_dtype=torch.float16)
    for child_name, child in model.named_children():
        if child_name != "esm":
            child.float()
    # Parameters and buffers held by the model itself rather than by a child --
    # esm_s_combine is one of these, and it is the one that matters.
    for parameter in model.parameters(recurse=False):
        parameter.data = parameter.data.float()
    for buffer in model.buffers(recurse=False):
        if buffer.is_floating_point():
            buffer.data = buffer.data.float()
    if device != "cuda":
        # Half precision on a CPU is slower than single where it works at all.
        model.esm = model.esm.float()
else:
    model = EsmForProteinFolding.from_pretrained(request["model"], low_cpu_mem_usage=True)
    # Before the move, not after: the stem is most of the weights, so halving it
    # here keeps a couple of gigabytes off the card as well as out of the copy.
    model.esm = model.esm.half()

model = model.to(device).eval()
# Chunking trades speed for peak activation memory. Smaller when the card is
# tight, which is the case this keeps running into.
model.trunk.set_chunk_size(int(request.get("chunk") or (64 if free > 9e9 else 16)))
print("STAGE stem %s, trunk %s"
      % (next(model.esm.parameters()).dtype, next(model.trunk.parameters()).dtype), flush=True)

results = []
for index, sequence in enumerate(request["sequences"]):
    print("STAGE folding sequence %d of %d (%d residues)"
          % (index + 1, len(request["sequences"]), len(sequence)), flush=True)
    tokens = tokenizer([sequence], return_tensors="pt", add_special_tokens=False)
    with torch.no_grad():
        output = model(**{k: v.to(device) for k, v in tokens.items()})
    # output_to_pdb writes per-residue confidence into the B-factor column, so
    # the caller can read it off the file rather than depending on the shape of
    # a tensor that has moved between releases.
    results.append(model.output_to_pdb(output)[0])
    del output
    if device == "cuda":
        # One sequence's activations are no use to the next, and the next may be
        # longer.
        torch.cuda.empty_cache()
open(request["out"], "w").write(json.dumps(results))
print("STAGE folded", flush=True)
'''


# Distinctive on purpose: it is what a stray fold is found by.
FOLD_SCRIPT_NAME = "proteincad_fold_run.py"


def clear_strays() -> list:
    """Kill fold processes left behind by a job nobody is waiting for.

    A fold that has been abandoned still holds the card, and the next one then
    fails for want of the two megabytes it was short -- reporting the allocation
    that happened to be last and nothing about what took the rest. Restarting
    the Colab session clears it and costs the tunnel, the worker and the URL the
    app was pointed at; killing the process that is actually holding it costs
    nothing.

    Only this file's own script name is matched, so nothing else on the machine
    is touched.
    """
    try:
        found = subprocess.run(["pgrep", "-f", FOLD_SCRIPT_NAME],
                               capture_output=True, text=True, timeout=20)
    except Exception:
        return []                      # no pgrep here; nothing lost but the sweep
    mine = os.getpid()
    killed = []
    for line in (found.stdout or "").split():
        if not line.isdigit() or int(line) == mine:
            continue
        try:
            os.kill(int(line), signal.SIGTERM)
            killed.append(line)
        except OSError:
            pass                       # already gone, or not ours to signal
    return killed


class EsmFolder:
    """Predicts a structure from a sequence alone.

    No alignment step, which is the point: a de novo binder has no homologues,
    so anything that begins by searching for them begins with nothing. Folding
    the designed sequence and measuring how close it lands to the backbone it
    was designed for is the standard self-consistency check, and the number it
    produces is the first honest answer to "is this a real protein".
    """

    name = "esmfold"

    def __init__(self, python: str = sys.executable, model: str = ESMFOLD_MODEL):
        self.python = python
        self.model = model
        self.found: dict = {}
        self._problems: list | None = None

    def problems(self) -> list:
        """Cached: importing transformers costs a few seconds and the answer
        cannot change without a pip install, which restarts nothing here but is
        rare enough that a stale yes is not worth a probe on every job."""
        if self._problems is None:
            self._problems = self._check()
        return self._problems

    def _check(self) -> list:
        try:
            done = subprocess.run([self.python, "-c", FOLD_PROBE], capture_output=True,
                                  text=True, timeout=600, env=fold_environment())
        except Exception as error:
            return [f"could not check the structure predictor: {error}"]

        for line in reversed((done.stdout or "").splitlines()):
            if line.startswith("FOLD "):
                self.found = json.loads(line[5:])
                break
        else:
            tail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-3:]
            return [f"{self.python} could not be asked about ESMFold: " + " / ".join(tail)]

        if self.found.get("problem"):
            note = self.found["problem"][:500]
            # Nothing here uses torchvision. It is Colab's, it was compiled
            # against Colab's torch, and pinning torch for DGL left it unable to
            # register its own operators -- so the error names an image library
            # while the model failing to load is a protein folder.
            if "torchvision" in note or "torchaudio" in note:
                note += (" — that is torch's sibling left behind by the version DGL "
                         "needs, not ESMFold")
            return [note + " — run: python colab_worker.py setup --only fold"]
        if not self.found.get("cuda"):
            return ["no CUDA device — folding on a CPU takes hours"]
        return []

    def fold(self, sequences: list, work: Path, note=None, stop=None) -> list:
        """One PDB per sequence, in the order given."""
        stray = clear_strays()
        if stray:
            say(f"  freed the card: killed {len(stray)} fold process(es) nobody was "
                f"waiting for ({', '.join(stray)})")
            time.sleep(2.0)            # the driver needs a moment to reclaim it
        request = work / "fold.json"
        script = work / FOLD_SCRIPT_NAME
        out = work / "folded.json"
        request.write_text(json.dumps({
            "model": self.model, "sequences": list(sequences), "out": str(out),
        }))
        script.write_text(FOLD_SCRIPT)

        process = subprocess.Popen([self.python, str(script), str(request)],
                                   env=fold_environment(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        tail: list = []
        for line in process.stdout:
            line = line.rstrip()
            print("    " + line[:200], flush=True)
            tail.append(line[:200])
            del tail[:-40]
            if line.startswith("STAGE ") and note:
                note(line[6:])
            if stop and stop():
                # Left running, it would hold the card for the minutes it takes
                # to finish work nobody wants.
                process.terminate()
                raise SetupError("folding was cancelled")

        code = process.wait()
        if code == 0 and out.is_file():
            return json.loads(out.read_text())

        # The first line has to carry the reason, because it is the line the app
        # shows beside the design. Reporting the header and keeping the cause
        # underneath is how "folding failed:" reached a user with nothing at all
        # after the colon.
        #
        # Progress lines cannot be the reason -- "loading the predictor" is not
        # why it stopped -- but they stay in the output below, because the one
        # this step prints first carries how much memory there was to work with,
        # which is the evidence when the kernel kills it without a word.
        spoken = [line for line in tail if line.strip()]
        said = [line for line in spoken if not line.startswith("STAGE ")]
        if code in (-9, 137):
            reason = ("the predictor was killed, which on Colab means it ran out of system "
                      "memory while loading. A high-RAM runtime is the way past it")
        elif said:
            reason = said[-1]
        else:
            reason = f"the predictor exited with {code} and said nothing"
        raise SetupError(f"folding failed: {reason}"
                         + ("\n--- last output ---\n" + "\n".join(spoken[-10:]) if spoken else ""))


class FoldGenerator(Generator):
    """Stage two: turn a backbone into a protein, and check that it is one.

    Given a backbone sitting on its target, this designs a sequence for it and
    then folds that sequence on its own. If the prediction lands back on the
    backbone it was designed for, the design is self-consistent -- the sequence
    really does encode the shape that was drawn against the target. If it does
    not, the backbone was a shape nobody can build, and the number says so
    before anyone spends a month in a lab finding out.

    The two halves fail independently on purpose. Sequence design is cheap and
    almost always available; folding wants a real GPU and eleven gigabytes of
    weights. A worker that can only do the first still returns something useful.
    """

    name = "fold"

    def __init__(self, mpnn: ProteinMPNN, folder=None):
        self.mpnn = mpnn
        self.folder = folder or NoFolder()
        self._report: dict | None = None

    def report(self) -> dict | None:
        return self._report

    def diagnose(self) -> dict:
        # Asked first: it is what fills in the version report below.
        folding = self.folder.problems()
        self._report = {
            "proteinmpnn": str(self.mpnn.repo),
            "folder": self.folder.name,
            "versions": getattr(self.folder, "found", {}),
            # Only ProteinMPNN is load-bearing. A missing predictor is reported
            # on each design instead, where it belongs, rather than refusing a
            # job that would still have produced sequences.
            "problems": self.mpnn.problems(),
            "folding": folding,
        }
        return self._report

    def generate(self, spec, job):
        problems = self.mpnn.problems()
        if problems:
            job["error"] = "cannot design sequences here:\n" + "\n".join("  ! " + p for p in problems)
            return

        subject = spec.get("complex", {})
        pdb_text = subject.get("pdb", "")
        # However many chains the binder turned out to be. Nothing chooses that
        # number: RFdiffusion writes one chain per contig block, and the blocks
        # come from wherever the crop happened to fall. ProteinMPNN designs a
        # list of chains as readily as one, so there is nothing to restrict.
        chains = [c for c in (subject.get("binderChains") or []) if c]
        hotspots = set(spec.get("target", {}).get("hotspots") or [])
        run = spec.get("run", {})
        count = max(1, int(run.get("numDesigns", 8) or 8))
        top = max(1, min(count, int(run.get("foldTop", 2) or 2)))
        temperature = float(run.get("samplingTemp", 0.1) or 0.1)
        seed = int(run.get("seed", 0) or 0)

        wanted = set(chains)
        binder = [line for line in atom_lines(pdb_text) if line[21] in wanted]
        target = [line for line in atom_lines(pdb_text) if line[21] not in wanted]
        # Per chain and in the order asked for, because that is the order
        # ProteinMPNN answers in and the order the sequences are threaded back.
        backbones = {chain: alpha_carbons(l for l in binder if l[21] == chain)
                     for chain in chains}
        lengths = [len(backbones[chain]) for chain in chains]
        if not chains or min(lengths, default=0) < 3:
            present = ", ".join(sorted({l[21] for l in atom_lines(pdb_text)})) or "none"
            job["error"] = (f"the complex has no usable binder chain "
                            f"(asked for {', '.join(chains) or 'nothing'}; it holds {present})")
            return
        if not target:
            job["error"] = "the complex holds only the binder, so there is no target to design against"
            return

        # Only target atoms anywhere near the binder can touch it, and a crop is
        # mostly atoms that cannot.
        backbone = [point for chain in chains for point in backbones[chain]]
        middle = centroid(backbone)
        reach = max(sum((p[i] - middle[i]) ** 2 for i in range(3)) ** 0.5 for p in backbone) + 12.0
        nearby = [line for line in target
                  if sum((c - m) ** 2 for c, m in zip(coordinates(line), middle)) <= reach * reach]
        interface = heavy_atoms(nearby)
        picked = heavy_atoms(line for line in nearby
                             if f"{line[21]}{line[22:26].strip()}" in hotspots)

        work = Path(tempfile.mkdtemp(prefix="proteincad_fold_"))
        try:
            job["stage"] = (f"designing {count} sequences for {sum(lengths)} residues"
                            + (f" across {len(chains)} chains" if len(chains) > 1 else ""))
            complex_pdb = work / "complex.pdb"
            complex_pdb.write_text(pdb_text if pdb_text.endswith("\n") else pdb_text + "\n")
            try:
                sequences = self.mpnn.design(complex_pdb, chains, count, temperature, seed,
                                             work, lengths=lengths)
            except SetupError as error:
                job["error"] = str(error)
                return
            wrong = [parts for parts, _ in sequences
                     if [len(p) for p in parts] != lengths]
            if not sequences or wrong:
                job["error"] = (f"ProteinMPNN returned {len(sequences)} sequences, "
                                f"{len(wrong)} of them not {lengths} residues long — "
                                "what it designed is not what was asked for")
                return

            folded, why = [], ""
            if job["cancel"]:
                return
            if len(chains) > 1:
                # ESMFold predicts one chain from one sequence. Folding the
                # pieces separately would answer a different question from the
                # one being asked -- whether each chain holds its own shape, not
                # whether they hold it together -- so it is not done quietly.
                why = (f"the binder is {len(chains)} chains; the folding check predicts "
                       "one chain at a time")
            elif self.folder.problems():
                why = "; ".join(self.folder.problems())
            else:
                try:
                    folded = self.folder.fold([parts[0] for parts, _ in sequences[:top]], work,
                                              note=lambda stage: job.__setitem__("stage", stage),
                                              stop=lambda: bool(job.get("cancel")))
                except SetupError as error:
                    # The first line is the reason; the rest is the output it was
                    # read from, which belongs in the log rather than in a field
                    # the panel shows on one line.
                    why = str(error).splitlines()[0][:240]
                    print(f"[proteincad] {error}", flush=True)

            for index, (parts, score) in enumerate(sequences):
                if job["cancel"]:
                    return
                metrics = {
                    "source": "proteinmpnn" + (f"+{self.folder.name}" if index < len(folded) else ""),
                    "sequence": "/".join(parts),
                    "mpnn_score": score,
                }
                if index < len(folded):
                    pdb, extra = self.place_prediction(folded[index], binder)
                    metrics.update(extra)
                else:
                    threaded = []
                    for chain, sequence in zip(chains, parts):
                        threaded += thread_sequence(
                            [line for line in binder if line[21] == chain], sequence)
                    pdb = "\n".join(threaded) + "\nEND\n"
                    metrics["folded"] = False
                    if why:
                        metrics["why"] = why
                metrics["contacts"] = close_contacts(heavy_atoms(atom_lines(pdb)), interface)
                if hotspots:
                    metrics["hotspot_contacts"] = close_contacts(
                        heavy_atoms(atom_lines(pdb)), picked)
                job["designs"].append({
                    "name": f"seq_{index + 1:02d}", "pdb": pdb, "metrics": metrics,
                })
                job["progress"] = len(job["designs"])
                job["stage"] = f"scored {len(job['designs'])} of {len(sequences)}"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def place_prediction(prediction: str, backbone: list) -> tuple:
        """Put a prediction where the design it was made from sits.

        A predictor answers in its own frame -- it was given a sequence, not a
        position -- so what comes back is the right protein in the wrong place.
        Superposing it on the backbone it was designed for moves it onto the
        residues the user picked, and the RMSD of that fit is the measurement
        worth having: it is how far the sequence's own idea of its shape is from
        the shape that was drawn.
        """
        lines = list(atom_lines(prediction))
        predicted = alpha_carbons(lines)
        fixed = alpha_carbons(backbone)
        if len(predicted) != len(fixed) or len(fixed) < 3:
            return prediction, {"folded": True, "placed": False,
                                "why": f"predicted {len(predicted)} residues against {len(fixed)}"}

        rotate, shift, rmsd = superpose(predicted, fixed)
        moved = []
        for line in lines:
            point = coordinates(line)
            moved.append(with_coordinates(line, [
                sum(rotate[i][j] * point[j] for j in range(3)) + shift[i] for i in range(3)]))

        # output_to_pdb puts per-residue confidence in the B-factor column.
        # Releases have disagreed on whether it is a fraction or a percentage,
        # so it is read rather than assumed: nothing real scores 1.5.
        scores = [float(line[60:66]) for line in lines if line[12:16].strip() == "CA"]
        plddt = sum(scores) / len(scores) if scores else 0.0
        return "\n".join(moved) + "\nEND\n", {
            "folded": True,
            "placed": True,
            "plddt": round(plddt * 100 if plddt <= 1.5 else plddt, 1),
            "rmsd_to_backbone": round(rmsd, 2),
        }


# ------------------------------------------------------------------ pipeline


# ----------------------------------------------------------- running esm3


def esm3_residues(pdb_text: str, wanted=None) -> list:
    """Residues of a structure, in file order, as (chain, seq, lines).

    `wanted` is a set of `A59`-style labels -- what the viewer calls a picked
    residue -- and selects a subset in the file's own order rather than in the
    order they were clicked, because a motif is a stretch of chain and the model
    is going to be told so.
    """
    out: list = []
    index: dict = {}
    for line in atom_lines(pdb_text):
        chain, seq = residue_key(line)
        if wanted is not None and f"{chain}{seq}" not in wanted:
            continue
        key = (chain, seq)
        if key not in index:
            index[key] = len(out)
            out.append((chain, seq, []))
        out[index[key]][2].append(line)
    return out


def esm3_layout(spec: dict) -> dict:
    """Where everything the model is given sits in the chain it is asked for.

    This is the part of an ESM3 job with no RFdiffusion equivalent and the part
    worth testing, so it is worked out here -- on a machine with no GPU and no
    weights -- rather than inside the model script. What comes out is explicit
    enough that the script only has to copy rows into tensors.

        length      how long the designed chain is
        sequence    the sequence track as the model will see it, _ for masked
        motif       design position -> which input residue goes there
        ss8         the secondary structure track, _ for unconstrained
        sasa        exposure targets, as (first, last, value) over 1-based
        function    function terms, the same shape

    A motif is placed rather than described. RFdiffusion says `10-40/A17-29/5-15`
    and lets the contig parser sample the gaps; there is no contig here, so the
    spans are laid down in file order and the spare length is shared out evenly
    between the gaps -- ends included, so a single motif lands in the middle
    rather than at residue 1 with everything built off one side.
    """
    spec = spec or {}
    run = spec.get("run") or {}
    mode = esm3_mode_spec(spec.get("mode"))
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    binder = spec.get("binder") or {}

    typed = str(run.get("sequencePrompt") or "").strip().upper()
    hotspots = set(target.get("hotspots") or [])
    pdb = target.get("pdb") or ""

    # Which residues of the input are given to the model. A motif is the picked
    # ones; the modes that take a whole structure take all of it.
    if mode["id"] == "motif":
        source = esm3_residues(pdb, hotspots) if pdb else []
    elif mode["prompt"] and pdb:
        source = esm3_residues(pdb)
    else:
        source = []

    # Which tracks this protocol hands over is the table's own statement, and it
    # is the whole difference between the protocols: inverse folding gives the
    # structure and masks the sequence, structure prediction does the opposite.
    # Reading it from the table rather than from the mode id is what stops those
    # two from being the same job -- which is what they were when this filled in
    # every track it could find a value for, leaving the model with nothing to
    # generate and a plan whose passes had no masked positions to fill.
    gives_sequence = "sequence" in tuple(mode["prompt"])
    gives_structure = "structure" in tuple(mode["prompt"])

    flat = typed.replace("|", "") if typed else ""
    if typed:
        # A typed prompt is the whole statement of what the chain is: its length
        # is the design's length and its letters are the given positions.
        length = len(flat)
    elif mode["id"] in ("inverse", "predict", "resample") and source:
        length = len(source)
    else:
        low = int(binder.get("lengthMin", 0) or 0)
        high = int(binder.get("lengthMax", 0) or 0)
        length = max(low, 1) if high <= low else (low + high) // 2

    letters = list(flat if typed else "_" * length)
    motif: list = []

    if source and not typed:
        if mode["id"] in ("inverse", "predict", "resample"):
            places = list(range(len(source)))
        else:
            # Spans, in file order: a run of consecutive residues in one chain.
            spans: list = []
            for position, (chain, seq, _lines) in enumerate(source):
                number = int(re.sub(r"[^0-9-]", "", seq) or 0)
                if (spans and spans[-1]["chain"] == chain
                        and spans[-1]["last"] + 1 == number):
                    spans[-1]["last"] = number
                    spans[-1]["count"] += 1
                else:
                    spans.append({"chain": chain, "last": number, "count": 1,
                                  "at": position})
            kept = sum(span["count"] for span in spans)
            if kept > length:
                raise ValueError(
                    f"the {kept} picked residues do not fit in a {length}-residue design — "
                    "raise the length, or pick fewer")
            gaps = len(spans) + 1
            spare = length - kept
            share, extra = divmod(spare, gaps)
            places, cursor = [], 0
            for index, span in enumerate(spans):
                cursor += share + (1 if index < extra else 0)
                for offset in range(span["count"]):
                    places.append(cursor + offset)
                cursor += span["count"]

        for position, place in enumerate(places):
            chain, seq, lines = source[position]
            motif.append({"at": place, "chain": chain, "seq": seq})
            if gives_sequence and run.get("keepSequence", True):
                name = (lines[0][17:20].strip() if lines else "")
                one = next((k for k, v in THREE_LETTER.items() if v == name), "")
                if one:
                    letters[place] = one

    # Partial resampling: forget a fraction of the protein and write it again.
    # The dial between a copy of the input and something unrelated to it, which
    # is the whole protocol -- and it has to happen here, where the prompt is
    # built, because "masked" is a property of the prompt and not of the plan.
    #
    # The same positions on both tracks, so what comes back is a region
    # rewritten rather than a sequence fitted to coordinates it was not allowed
    # to move. Seeded, because a protocol whose output is a variation is one
    # somebody will want to reproduce.
    resampled: list = []
    if mode["id"] == "resample" and motif:
        fraction = float(run.get("fraction", 0.3) or 0.3)
        wanted = max(1, min(len(motif), int(round(fraction * length))))
        picker = random.Random(int(run.get("seed", 0) or 0))
        resampled = sorted(picker.sample(range(len(motif)), wanted))
        for index in reversed(resampled):
            letters[int(motif[index]["at"])] = "_"
            del motif[index]

    ss8 = str(run.get("secondaryStructure") or "").strip().upper()
    return {
        "length": length,
        "sequence": "".join(letters),
        "chainbreaks": [i for i, c in enumerate(typed) if c == "|"] if typed else [],
        "motif": motif,
        "keep_structure": gives_structure and bool(run.get("keepStructure", True)) and bool(motif),
        "resampled": len(resampled),
        "ss8": ss8,
        "sasa": _spans(run.get("sasa") or [], "sasa", True),
        "function": _spans(run.get("function") or [], "function", False),
    }


ESM3_PROBE = r'''
import json, sys
out = {"python": ".".join(str(v) for v in sys.version_info[:3])}


def chain(error):
    links, seen = [], error
    while seen is not None and len(links) < 5:
        links.append("%s: %s" % (type(seen).__name__, seen))
        seen = seen.__cause__ or seen.__context__
    return " <- ".join(links)


try:
    import torch
    out["torch"] = torch.__version__
    out["cuda"] = bool(torch.cuda.is_available())
    if out["cuda"]:
        out["gpu"] = torch.cuda.get_device_name(0)
except BaseException as error:
    out["problem"] = "torch is not importable (%s)" % chain(error)
else:
    try:
        import esm
        out["esm"] = getattr(esm, "__version__", "present")
        from esm.models.esm3 import ESM3
        from esm.sdk.api import ESMProtein, GenerationConfig
        out["esm3"] = "ok"
    except BaseException as error:
        out["problem"] = "the esm package is not importable (%s)" % chain(error)
print("ESM3 " + json.dumps(out))
'''

# One process per job rather than per design: loading the weights is most of the
# cost of a short run, so a subprocess for every design would pay it again each
# time. Same reasoning as the folding script next door.
ESM3_SCRIPT = r'''
import json, os, sys, math
import torch
from esm.models.esm3 import ESM3
from esm.sdk.api import ESMProtein, GenerationConfig

request = json.loads(open(sys.argv[1]).read())
device = "cuda" if torch.cuda.is_available() else "cpu"
where = "no GPU visible"
if device == "cuda":
    free, total = torch.cuda.mem_get_info()
    where = "%.1f of %.1f GB free on the %s" % (
        free / 1e9, total / 1e9, torch.cuda.get_device_name(0))
print("STAGE loading ESM3 (%s)" % where, flush=True)

model = ESM3.from_pretrained(request["model"]).to(device).eval()

layout = request["layout"]
length = int(layout["length"])

# The motif's own coordinates, read by the library rather than by us: the
# structure track is a (length, 37, 3) tensor in a fixed atom order, and copying
# whole rows out of a protein the library parsed is how to fill it without this
# script having an opinion about which index is CB.
motif_rows = None
if request.get("motif_pdb") and layout.get("keep_structure"):
    given = ESMProtein.from_pdb(request["motif_pdb"])
    motif_rows = given.coordinates

def fresh():
    """A prompt, built again for each design so a run cannot inherit the last
    one's decoded tracks."""
    coordinates = None
    if motif_rows is not None:
        coordinates = torch.full((length, motif_rows.shape[1], 3), float("nan"))
        for position, entry in enumerate(layout["motif"]):
            if position < motif_rows.shape[0]:
                coordinates[int(entry["at"])] = motif_rows[position]
    protein = ESMProtein(
        sequence=layout["sequence"],
        coordinates=coordinates,
    )
    if layout.get("ss8"):
        protein.secondary_structure = layout["ss8"]
    if layout.get("sasa"):
        values = [None] * length
        for first, last, value in layout["sasa"]:
            for index in range(int(first) - 1, min(int(last), length)):
                values[index] = float(value)
        protein.sasa = values
    if layout.get("function"):
        try:
            from esm.sdk.api import FunctionAnnotation
            protein.function_annotations = [
                FunctionAnnotation(label=str(term), start=int(first), end=int(last))
                for first, last, term in layout["function"]
            ]
        except BaseException as error:
            print("STAGE function prompt ignored: %s" % error, flush=True)
    return protein

results = []
for index in range(int(request["count"])):
    protein = fresh()
    for step in request["plan"]:
        track = step["track"]
        # More steps than there are masked positions is not a finer decode, it
        # is an error from inside the sampler. Clamped, and said out loud,
        # because a plan that asked for 8 and got 3 should not look like a plan
        # that asked for 3.
        masked = length
        if track == "sequence":
            masked = max(1, protein.sequence.count("_") if protein.sequence else length)
        steps = max(1, min(int(step.get("num_steps", 8)), masked))
        if steps != int(step.get("num_steps", 8)):
            print("STAGE %s: %d steps, clamped to the masked positions"
                  % (track, steps), flush=True)
        print("STAGE design %d of %s: %s over %d step(s)"
              % (index + 1, request["count"], track, steps), flush=True)
        # Built through the constructor rather than by assigning to the object
        # afterwards: a config class that is frozen, or that validates in
        # __post_init__, accepts the first and silently or loudly refuses the
        # second. Each optional field is added only if this version of the
        # library has it, so a field that has been renamed costs that setting
        # rather than the run.
        wanted = {"track": track, "num_steps": steps}
        if step.get("temperature") is not None:
            wanted["temperature"] = float(step["temperature"])
        if request.get("top_p") is not None:
            wanted["top_p"] = float(request["top_p"])
        if request.get("condition_on_coordinates_only") is not None:
            wanted["condition_on_coordinates_only"] = bool(
                request["condition_on_coordinates_only"])
        try:
            config = GenerationConfig(**wanted)
        except TypeError as error:
            known = {"track": track, "num_steps": steps}
            dropped = sorted(set(wanted) - set(known))
            print("STAGE this esm build does not take %s (%s); running without"
                  % (", ".join(dropped), error), flush=True)
            config = GenerationConfig(**known)
        protein = model.generate(protein, config)
        if getattr(protein, "error_msg", None):
            raise SystemExit("ESM3 refused the %s pass: %s" % (track, protein.error_msg))

    out_pdb = os.path.join(request["out_dir"], "esm3_%03d.pdb" % index)
    protein.to_pdb(out_pdb)

    def number(value):
        try:
            if value is None:
                return None
            if hasattr(value, "mean"):
                value = value.mean()
            value = float(value)
            return None if math.isnan(value) else round(value, 4)
        except BaseException:
            return None

    results.append({
        "pdb": out_pdb,
        "sequence": protein.sequence or "",
        "ptm": number(getattr(protein, "ptm", None)),
        "plddt": number(getattr(protein, "plddt", None)),
    })
    if device == "cuda":
        torch.cuda.empty_cache()

open(request["out"], "w").write(json.dumps(results))
print("STAGE done", flush=True)
'''

ESM3_SCRIPT_NAME = "proteincad_esm3_run.py"


class Esm3Generator(Generator):
    """Backbones from ESM3, as an alternative to RFdiffusion.

    What comes back is a protein rather than a shape: ESM3 writes the sequence
    and the structure on the same pass, so a design arrives with residues
    already chosen. That does not make stage two redundant -- the folding check
    is a second model's independent opinion of the same sequence, and that is
    the point of it -- but it does mean a design is readable straight away.
    """

    name = "esm3"

    def __init__(self, python: str = sys.executable, model: str = ESM3_MODEL,
                 work: Path | None = None):
        self.python = python
        self.model = model
        self.work = work
        self.found: dict = {}
        self._problems: list | None = None

    def problems(self) -> list:
        if self._problems is None:
            self._problems = self._check()
        return self._problems

    def _check(self) -> list:
        try:
            done = subprocess.run([self.python, "-c", ESM3_PROBE], capture_output=True,
                                  text=True, timeout=600, env=esm3_environment())
        except Exception as error:
            return [f"could not check ESM3: {error}"]

        for line in reversed((done.stdout or "").splitlines()):
            if line.startswith("ESM3 "):
                self.found = json.loads(line[5:])
                break
        else:
            tail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-3:]
            return [f"{self.python} could not be asked about ESM3: " + " / ".join(tail)]

        if self.found.get("problem"):
            return [self.found["problem"][:500]
                    + " — run: python colab_worker.py setup --only esm3"]
        problems = []
        if not self.found.get("cuda"):
            problems.append("no CUDA device — ESM3 on a CPU is minutes per design")
        return problems

    def diagnose(self) -> dict:
        problems = list(self.problems())
        report = {"problems": problems, "weights": []}
        for key in ("python", "torch", "cuda", "gpu", "esm"):
            if key in self.found:
                report[key] = self.found[key]
        return report

    def generate(self, spec, job) -> None:
        if self.problems():
            job["error"] = "; ".join(self.problems())
            return

        try:
            layout = esm3_layout(spec)
            plan = esm3_plan(spec)
        except ValueError as error:
            job["error"] = str(error)
            return

        run = spec.get("run") or {}
        count = max(1, int(run.get("numDesigns", 1) or 1))
        root = Path(self.work) if self.work else Path(tempfile.mkdtemp(prefix="proteincad-esm3-"))
        work = root / f"job-{job.get('id', uuid.uuid4().hex[:8])}"
        designs_dir = work / "out"
        designs_dir.mkdir(parents=True, exist_ok=True)

        # The motif as its own file, numbered in the order the layout places it,
        # so the script can read coordinates for row n and know it belongs at
        # layout["motif"][n]["at"] without matching residue numbers across two
        # files that number them differently.
        motif_pdb = ""
        target_pdb = (spec.get("target") or {}).get("pdb") or ""
        if layout["motif"] and layout["keep_structure"] and target_pdb:
            wanted = {(entry["chain"], entry["seq"]) for entry in layout["motif"]}
            order = {(entry["chain"], entry["seq"]): index
                     for index, entry in enumerate(layout["motif"])}
            lines, serial = [], 1
            for chain, seq, atoms in esm3_residues(target_pdb):
                if (chain, seq) not in wanted:
                    continue
                number = order[(chain, seq)] + 1
                for line in atoms:
                    lines.append(f"{line[:6]}{serial:5d}{line[11:21]}A{number:4d} {line[27:]}")
                    serial += 1
            motif_pdb = str(work / "motif.pdb")
            Path(motif_pdb).write_text("\n".join(lines) + "\nEND\n")

        request = work / "esm3.json"
        script = work / ESM3_SCRIPT_NAME
        out = work / "esm3-out.json"
        request.write_text(json.dumps({
            "model": str(run.get("variant") or self.model).strip() or self.model,
            "layout": layout,
            "plan": plan,
            "count": count,
            "top_p": run.get("topP"),
            "condition_on_coordinates_only": run.get("conditionOnCoordinatesOnly"),
            "out_dir": str(designs_dir),
            "out": str(out),
            "motif_pdb": motif_pdb,
        }))
        script.write_text(ESM3_SCRIPT)

        # Recorded the way RFdiffusion's command line is, and for the same
        # reason: with this much settable, the only way to tell a setting that
        # was applied from one this build never heard of is to show what ran.
        job["log"] = ("esm3 " + str(request.parent) + "\n  plan: "
                      + ", ".join(f"{s['track']}:{s['num_steps']}"
                                  f"{':' + str(s['temperature']) if 'temperature' in s else ''}"
                                  for s in plan)
                      + f"\n  length: {layout['length']}, motif: {len(layout['motif'])} residue(s)")

        process = subprocess.Popen([self.python, str(script), str(request)],
                                   env=esm3_environment(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, bufsize=1)
        tail: list = []
        for line in process.stdout:
            line = line.rstrip()
            print("    " + line[:200], flush=True)
            tail.append(line[:200])
            del tail[:-40]
            if line.startswith("STAGE "):
                job["stage"] = line[6:]
            if job["cancel"]:
                process.terminate()
                job["stage"] = "cancelled"
                return

        code = process.wait()
        if code != 0 or not out.is_file():
            spoken = [line for line in tail if line.strip()]
            said = [line for line in spoken if not line.startswith("STAGE ")]
            job["error"] = ("ESM3 failed: "
                            + (said[-1] if said else f"it exited with {code} and said nothing")
                            + ("\n--- last output ---\n" + "\n".join(spoken[-10:])
                               if spoken else ""))
            return

        self._collect(json.loads(out.read_text()), spec, layout, job)

    def _collect(self, results: list, spec: dict, layout: dict, job: dict) -> None:
        """Place what came back, and say how well it kept what it was given."""
        target_pdb = (spec.get("target") or {}).get("pdb") or ""
        anchors = {}
        if layout["motif"] and layout["keep_structure"] and target_pdb:
            for chain, seq, atoms in esm3_residues(target_pdb):
                for line in atoms:
                    if line[12:16].strip() == "CA":
                        anchors[(chain, seq)] = coordinates(line)

        for index, result in enumerate(results):
            if job["cancel"]:
                return
            pdb = Path(result["pdb"]).read_text() if Path(result["pdb"]).is_file() else ""
            lines = list(atom_lines(pdb))
            if not lines:
                continue

            metrics = {
                "source": "esm3",
                "length": layout["length"],
                "sequence": result.get("sequence") or "",
            }
            if result.get("ptm") is not None:
                metrics["ptm"] = result["ptm"]
            if result.get("plddt") is not None:
                metrics["plddt"] = result["plddt"]

            # Back into the scene. ESM3 answers in a frame of its own, so a
            # design is placed by putting the motif it was given back onto the
            # copy of that motif the user is looking at. How far the two are
            # apart afterwards is the measurement that says whether the model
            # actually honoured the motif, so it is reported rather than hidden
            # by the superposition that uses it.
            if anchors:
                mobile, fixed = [], []
                residues = esm3_residues(pdb)
                for entry in layout["motif"]:
                    place = int(entry["at"])
                    if place >= len(residues):
                        continue
                    _chain, _seq, atoms = residues[place]
                    here = next((coordinates(line) for line in atoms
                                 if line[12:16].strip() == "CA"), None)
                    there = anchors.get((entry["chain"], entry["seq"]))
                    if here and there:
                        mobile.append(here)
                        fixed.append(there)
                if len(mobile) >= 3:
                    rotate, shift, rmsd = superpose(mobile, fixed)
                    lines = [with_coordinates(line, [
                        sum(rotate[i][j] * coordinates(line)[j] for j in range(3)) + shift[i]
                        for i in range(3)]) for line in lines]
                    metrics["motif_rmsd"] = round(rmsd, 2)
                    metrics["placed"] = "superposed on the picked residues"
                else:
                    metrics["placed"] = "the model's own frame: too little motif to place it on"
            else:
                metrics["placed"] = "the model's own frame"

            job["designs"].append({
                "name": f"esm3_{index + 1:02d}",
                # Everything ESM3 returns is the model's own work, motif
                # included: the coordinates for a kept residue come back through
                # its structure track rather than copied across, so marking some
                # of them as given would be a claim about provenance that is not
                # true. Nothing marked is read as generated throughout, which is
                # what stage two then designs a sequence for.
                "pdb": "\n".join(lines) + "\nEND\n",
                "metrics": metrics,
            })
            job["progress"] = len(job["designs"])


class Pipeline(Generator):
    """One worker, several stages; the spec says which one it wants.

    Keeping them in one process matters: they share a GPU, and two workers would
    mean two tunnels, two tokens and two things to keep alive. Each stage checks
    its own environment, so a machine with RFdiffusion but no structure
    predictor runs backbone jobs normally and says precisely what is missing
    when asked for the rest.
    """

    def __init__(self, stages: dict):
        self.stages = stages
        self.name = "+".join(stage.name for stage in stages.values())
        self._report: dict | None = None

    @staticmethod
    def route(spec) -> str:
        """Which stage answers this job.

        Stage one has two engines and stage two has none, so the key is the kind
        except for a backbone, where it is the engine that draws it. Keeping
        RFdiffusion's route named `binder` means every spec written before there
        was a second engine still lands where it always did.
        """
        kind = (spec.get("kind") or "binder").lower()
        if kind != "binder":
            return kind
        engine = engine_of(spec)
        return "binder" if engine == DEFAULT_ENGINE else engine

    def generate(self, spec, job):
        route = self.route(spec)
        stage = self.stages.get(route)
        if stage is None:
            kind = (spec.get("kind") or "binder").lower()
            asked = (f"a backbone from {route}" if kind == "binder"
                     else f"a {kind!r} job")
            job["error"] = (f"this worker cannot run {asked}; it has: "
                            + ", ".join(sorted(self.stages))
                            + ". Restart it with --generator rfdiffusion,esm3 to serve both.")
            return
        stage.generate(spec, job)

    def report(self) -> dict | None:
        """The last full preflight, or None if none has finished. Same shape as
        diagnose(), so /health and the startup check read alike."""
        return self._report

    def diagnose(self) -> dict:
        merged: dict = {"stages": {}, "problems": []}
        for kind, stage in self.stages.items():
            if not hasattr(stage, "diagnose"):
                continue
            report = stage.diagnose()
            merged["stages"][kind] = report
            merged["problems"] += [f"{kind}: {problem}" for problem in report.get("problems", [])]
            # The environment summary the preflight prints belongs to whichever
            # stage actually probed one; the rest only report their own files.
            for key in ("python", "torch", "cuda", "cuda_build", "gpu", "dgl", "weights"):
                if key in report and key not in merged:
                    merged[key] = report[key]
        merged.setdefault("weights", [])
        self._report = merged
        return merged


def wanted_generators(value) -> list:
    """`--generator` as a list. One name, or several separated by commas.

    A list rather than a choice because the engines are not alternatives on the
    machine: a box with both sets of weights can serve both, and which one a
    design uses is the user's decision per job rather than the operator's at
    start-up.
    """
    names = [part.strip().lower() for part in str(value or "").replace(",", " ").split()]
    return [name for name in names if name] or ["echo"]


def build_generator(args) -> Generator:
    names = wanted_generators(args.generator)
    if "echo" in names:
        return EchoGenerator()

    folder = NoFolder() if args.folder == "none" else EsmFolder(
        python=args.python, model=args.esmfold_model)
    stages: dict = {}
    if "rfdiffusion" in names:
        stages["binder"] = RFdiffusionGenerator(
            args.rfdiffusion,
            python=args.python,
            extra=args.extra or [],
            binder_defaults=not args.no_binder_defaults,
        )
    if "esm3" in names:
        stages["esm3"] = Esm3Generator(
            # Its own interpreter, when it has been given one. RFdiffusion's
            # torch version is pinned hard -- DGL's wheels load libraries named
            # for the exact build they were made against -- and ESM3 has
            # requirements of its own that have no reason to agree with it.
            # Making them share an environment makes one engine's install a
            # chance to break the other; defaulting to the same interpreter
            # keeps the simple case simple, where they do happen to coexist.
            python=getattr(args, "esm3_python", None) or args.python,
            model=getattr(args, "esm3_model", None) or ESM3_MODEL,
        )
    # Stage two serves whichever engine drew the backbone, so it is here as soon
    # as anything is: an ESM3 design gets its sequence checked by the same two
    # models an RFdiffusion one does, which is the only way the numbers from the
    # two engines mean the same thing.
    stages["fold"] = FoldGenerator(ProteinMPNN(args.proteinmpnn, python=args.python), folder)
    return Pipeline(stages)


# ------------------------------------------------------------------- server


# A GPU is one resource, and the server hands out one job at a time -- but the
# worker answers each POST on its own thread, so a retry, a second browser tab
# or a job the app gave up on while the model kept going all put two things on
# the card at once. They do not run twice as fast; the second runs out of
# memory, and the message blames whatever it was loading rather than the job
# that was already there.
GPU = threading.Lock()


def run_job(generator: Generator, job: dict) -> None:
    if GPU.locked():
        job["stage"] = "waiting for the GPU — another job is using it"
        print(f"[proteincad] {job['stage']}", flush=True)
    with GPU:
        _run_job(generator, job)


def _run_job(generator: Generator, job: dict) -> None:
    job["status"] = "running"
    job["stage"] = "starting"
    job["started"] = time.time()
    touch()
    try:
        generator.generate(job["spec"], job)
        # A design with no coordinates is not a design. Sending one anyway turns
        # a generator that did nothing into a job that looks finished, and the
        # first sign of trouble is an empty file failing to load minutes later,
        # named after a generator nobody remembers choosing.
        empty = [d["name"] for d in job["designs"] if not any(atom_lines(d.get("pdb") or ""))]
        job["designs"] = [d for d in job["designs"] if any(atom_lines(d.get("pdb") or ""))]
        job["progress"] = len(job["designs"])
        if empty and not job["error"]:
            job["error"] = (f"{len(empty)} of the results came back with no atoms "
                            f"({', '.join(empty[:3])}) — the generator produced nothing usable")
        if job["cancel"]:
            job["status"] = "cancelled"
        elif job["error"]:
            job["status"] = "failed"
        else:
            job["status"] = "done" if job["designs"] else "failed"
            if not job["designs"]:
                job["error"] = "the generator produced nothing"
    except Exception as error:
        job["status"] = "failed"
        job["error"] = f"{type(error).__name__}: {error}"
        import traceback
        traceback.print_exc()
    finally:
        job["finished"] = time.time()
        touch()


def make_handler(generator: Generator, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "proteinCAD-worker"

        def log_message(self, fmt, *args):
            sys.stderr.write("  %s\n" % (fmt % args))

        def _authorised(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {token}"

        def _send(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                payload = {
                    "status": "ok",
                    "generator": generator.name,
                    # For whoever is paying for the machine: is there work on
                    # the card, and how long since anyone wanted anything.
                    # Answered before the token check because the process that
                    # reads them runs on the box itself, with no token to hand.
                    "busy": busy(),
                    "idle": round(time.time() - LAST_ACTIVITY, 1),
                }
                if hasattr(generator, "report"):
                    # Whatever the background preflight has finished, possibly
                    # nothing yet. Never probes from inside the handler.
                    payload["preflight"] = generator.report()
                return self._send(payload)
            if not self._authorised():
                return self._send({"error": "unauthorised"}, 401)
            if self.path.startswith("/design/"):
                job_id = self.path.split("/")[2]
                job = JOBS.get(job_id)
                if not job:
                    return self._send({"error": "no such job"}, 404)
                touch()
                return self._send({
                    "job_id": job_id,
                    "status": job["status"],
                    "progress": job["progress"],
                    "total": job["total"],
                    "stage": job.get("stage", ""),
                    # The command the model was actually run with. There is no
                    # other way to tell a setting that was ignored from one that
                    # was applied, and a worker one version behind the panel
                    # silently ignores whatever it has not heard of.
                    "log": job.get("log", ""),
                    "error": job["error"],
                    "designs": job["designs"],
                })
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            if not self._authorised():
                return self._send({"error": "unauthorised"}, 401)
            touch()
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"

            if self.path == "/shutdown":
                # Lets a notebook cell replace a running worker without having to
                # hunt for the process that is holding the port.
                self._send({"status": "stopping"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return None

            if self.path.endswith("/cancel"):
                job_id = self.path.split("/")[2]
                job = JOBS.get(job_id)
                if not job:
                    return self._send({"error": "no such job"}, 404)
                job["cancel"] = True
                return self._send({"status": "cancelled"})

            if self.path != "/design":
                return self._send({"error": "not found"}, 404)

            try:
                spec = json.loads(raw.decode())
            except json.JSONDecodeError as error:
                return self._send({"error": f"bad JSON: {error}"}, 400)

            job_id = uuid.uuid4().hex[:12]
            job = {
                "spec": spec, "status": "queued", "progress": 0, "stage": "queued",
                "total": int(spec.get("run", {}).get("numDesigns", 1) or 1),
                "designs": [], "error": "", "cancel": False, "created": time.time(),
            }
            with JOBS_LOCK:
                JOBS[job_id] = job
            threading.Thread(target=run_job, args=(generator, job), daemon=True).start()
            return self._send({"job_id": job_id, "status": "queued"})

    return Handler


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `setup` is checked by hand rather than with subparsers so that the plain
    # `--port ... --generator ...` form keeps working unchanged.
    if argv and argv[0] == "setup":
        parser = argparse.ArgumentParser(prog="colab_worker.py setup",
                                         description="install the models and their dependencies")
        parser.add_argument("--rfdiffusion", default=DEFAULT_RFDIFFUSION)
        parser.add_argument("--proteinmpnn", default=DEFAULT_PROTEINMPNN)
        parser.add_argument("--python", default=os.environ.get("RFDIFFUSION_PYTHON",
                                                               sys.executable),
                            help="interpreter to install into and run the models with")
        parser.add_argument("--only", choices=["all", "rfdiffusion", "fold", "esm3"],
                            default="all",
                            help="rfdiffusion installs backbone generation; fold installs "
                                 "sequence design and the folding check; esm3 installs the "
                                 "second backbone engine, which `all` leaves out because "
                                 "it is 5.5 GB most runs have no use for")
        parser.add_argument("--esmfold-model", default=ESMFOLD_MODEL)
        parser.add_argument("--esm3-model", default=ESM3_MODEL)
        parser.add_argument("--esm3-python",
                            default=os.environ.get("PROTEINCAD_ESM3_PYTHON", ""),
                            help="install ESM3 into this interpreter instead, keeping it "
                                 "out of the environment RFdiffusion's torch pin owns")
        parser.add_argument("--weights", default="core", metavar="WHICH",
                            help="which RFdiffusion checkpoints to fetch now: core (the two "
                                 "a binder run picks between), all (3.9 GB), none, or names "
                                 "from " + ", ".join(sorted(WEIGHTS)) + ". Anything not "
                                 "fetched is downloaded by the first job that needs it")
        parser.add_argument("--skip-weights", action="store_true",
                            help="do not fetch the folding weights now; the first fold job "
                                 "will spend about 8.5 GB of download on them instead")
        try:
            return run_setup(parser.parse_args(argv[1:]))
        except SetupError as error:
            print(f"\nsetup stopped: {error}", flush=True)
            return 1
        except KeyboardInterrupt:
            print("\ninterrupted", flush=True)
            return 1

    parser = argparse.ArgumentParser(description="proteinCAD GPU worker")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--generator", default=os.environ.get("PROTEINCAD_GENERATOR", "echo"),
                        metavar="WHICH",
                        help="which backbone engines to serve: echo (a connection test and "
                             "nothing else), rfdiffusion, esm3, or both separated by a comma. "
                             "Either engine also brings stage two with it — the sequence "
                             "design and folding check that turn a backbone into a protein")
    parser.add_argument("--rfdiffusion", default=DEFAULT_RFDIFFUSION,
                        help="path to an RFdiffusion checkout")
    parser.add_argument("--proteinmpnn", default=DEFAULT_PROTEINMPNN,
                        help="path to a ProteinMPNN checkout")
    parser.add_argument("--folder", default=os.environ.get("PROTEINCAD_FOLDER", "esmfold"),
                        choices=["esmfold", "none"],
                        help="how to check a designed sequence folds. `none` still returns "
                             "sequences, without the check")
    parser.add_argument("--esmfold-model", default=ESMFOLD_MODEL)
    parser.add_argument("--esm3-model", default=ESM3_MODEL,
                        help="which ESM3 weights to load: the open model by name, or a path "
                             "to a local copy")
    parser.add_argument("--esm3-python",
                        default=os.environ.get("PROTEINCAD_ESM3_PYTHON", ""),
                        help="interpreter to run ESM3 with, if it was installed somewhere "
                             "other than RFdiffusion's. Defaults to --python")
    parser.add_argument("--python", default=os.environ.get("RFDIFFUSION_PYTHON", sys.executable),
                        help="interpreter to run the models with. Defaults to this one; point it "
                             "at another environment if they were installed elsewhere. Pass "
                             "the same value to `setup`.")
    parser.add_argument("--token", default=os.environ.get("PROTEINCAD_WORKER_TOKEN", ""))
    parser.add_argument("--extra", nargs="*", help="extra arguments passed straight to run_inference.py")
    parser.add_argument("--no-binder-defaults", action="store_true",
                        help="drop the denoiser.noise_scale_* overrides, if this build rejects them")
    args = parser.parse_args(argv)

    generator = build_generator(args)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), make_handler(generator, args.token))
    except OSError as error:
        if error.errno in (48, 98):  # EADDRINUSE on macOS / Linux
            print(f"port {args.port} is already in use — an earlier worker is probably still "
                  f"running.\nStop it with:\n"
                  f"  curl -X POST -H 'Authorization: Bearer <token>' "
                  f"http://localhost:{args.port}/shutdown\n"
                  f"or:  fuser -k {args.port}/tcp")
            return 1
        raise
    httpd.daemon_threads = True
    print(f"proteinCAD worker ready on {args.host}:{args.port} using the {generator.name} generator")

    # Say up front what would make the first run fail -- but off the main thread.
    # The probe imports torch, dgl and RFdiffusion, which takes the best part of
    # a minute; doing it before serve_forever() would leave /health unanswered
    # for that long and make a healthy worker look like a dead one.
    def preflight():
        report = generator.diagnose()
        print(f"  python {report.get('python')}, torch {report.get('torch')}, "
              f"dgl {report.get('dgl')}, cuda {report.get('cuda')}"
              f"{', ' + report['gpu'] if report.get('gpu') else ''}")
        print(f"  weights: {', '.join(report['weights']) or 'NONE FOUND'}")
        for problem in report["problems"]:
            print(f"  ! {problem}")
        print("  preflight done", flush=True)

    if hasattr(generator, "diagnose"):
        print("  checking the environment in the background...", flush=True)
        threading.Thread(target=preflight, daemon=True).start()
    if not args.token:
        print("no token set: anyone who can reach this port can queue jobs")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
