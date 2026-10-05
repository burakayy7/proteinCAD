# The model, and nothing else.
#
# This image carries the code and the dependency stack. It does NOT carry any
# weights: those are twelve gigabytes, they change on their own schedule, and
# keeping them in S3 is what lets a machine exist only while it is working. An
# image with them baked in would be about twenty-five gigabytes and would have
# to be rebuilt to add a checkpoint.
#
#     built:   python, torch, dgl, hydra, e3nn, transformers, the two checkouts,
#              and esm in a virtualenv of its own
#     mounted: /opt/models/RFdiffusion/models              checkpoints, ro
#              /opt/models/ProteinMPNN/vanilla_model_weights  ro
#              /models/esmfold                             folding weights, ro
#              /models/esm3                                an ESM3 hub cache, ro
#              /work                                       this job, rw
#
# ESM3 gets its own virtualenv rather than sharing this one. The torch here is
# pinned to the exact build DGL's wheels were compiled against -- they load
# libraries named for it -- so a second model's requirements resolving against
# the same environment is a chance to move torch and break RFdiffusion. The
# venv has --system-site-packages, so it costs a few megabytes rather than
# another copy of torch, and the worker is told to use it.
#
# The build does not need a GPU. The dependency resolution in colab_worker.py
# probes DGL's index at build time rather than pinning a wheel here, because
# that index serves wheels it does not actually allow you to download and the
# right one depends on the torch that got installed -- which is exactly the
# problem that logic exists to solve. Repeating a pin here would go stale and
# fail in a way that names the wrong thing.

FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

# PIP_NO_CACHE_DIR is here for the image's size rather than for pip's manners.
# Every wheel pip downloads would otherwise be stored twice -- once unpacked
# into site-packages and once kept in ~/.cache/pip -- and both copies land in
# the layer that ran the install. On a stack with torch in it that is gigabytes
# of image for a cache nothing will ever read again.
#
# (Set as an ENV rather than a flag on each call so it covers every install in
# the build, including the ones colab_worker.py makes for itself. It is
# inherited at run time too, where it costs nothing: the container installs
# nothing.)
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    RFDIFFUSION_DIR=/opt/models/RFdiffusion \
    PROTEINMPNN_DIR=/opt/models/ProteinMPNN \
    USE_TF=0 \
    USE_JAX=0 \
    USE_TORCH=1 \
    TOKENIZERS_PARALLELISM=false

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev python3-venv git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# colab_worker.py is the whole model side of proteinCAD, unmodified: the same
# file the Colab notebook runs. Copied rather than pip-installed so that what
# runs here and what runs in a notebook are demonstrably the same bytes.
COPY proteincad/colab_worker.py /app/colab_worker.py
COPY deploy/aws/worker/model_runner.py /app/model_runner.py

# The checkouts. Small -- tens of megabytes each -- and in the image because
# the code and its dependencies belong together; only the weights are mounted.
RUN python3 -c "\
import sys; sys.path.insert(0, '/app'); \
from pathlib import Path; import colab_worker as cw; \
cw.clone(Path(cw.DEFAULT_RFDIFFUSION)); \
cw.clone(Path(cw.DEFAULT_PROTEINMPNN), cw.PROTEINMPNN_GIT, 'protein_mpnn_run.py')"

# ProteinMPNN checks its weights into its own repository, so the clone above
# brings a copy. Removed, because the mount brings another -- and two copies of
# a weights file where only one is mounted over is exactly how a machine ends
# up running weights nobody published and nobody can name.
RUN rm -rf "$PROTEINMPNN_DIR/vanilla_model_weights" \
           "$PROTEINMPNN_DIR/soluble_model_weights" \
           "$PROTEINMPNN_DIR/ca_model_weights" \
    && mkdir -p "$PROTEINMPNN_DIR/vanilla_model_weights"

# torch, dgl and the rest, chosen by the same resolver the notebook uses.
# `setup` reports rather than raises when something is only a warning -- no GPU
# at build time is one -- so its exit code is not the check. The import below
# is: if any of these is missing or built against the wrong torch, the build
# stops here rather than at the first job.
RUN python3 /app/colab_worker.py setup --weights none --skip-weights \
        --rfdiffusion "$RFDIFFUSION_DIR" --proteinmpnn "$PROTEINMPNN_DIR" \
    ; echo "setup finished with $?"

RUN python3 -c "\
import torch, dgl, hydra, e3nn, transformers; \
print('torch', torch.__version__, '| dgl', dgl.__version__, \
      '| transformers', transformers.__version__)"

# The second engine, in its own environment for the reason given at the top.
# --system-site-packages so it reuses this image's torch and CUDA stack unless
# `esm` insists on another, in which case only this venv moves.
#
# Its own step, and allowed to fail the build: `python3 -m venv` needs
# python3-venv installed (ensurepip lives there) and says so clearly when it is
# missing. Folded into the step below -- which ends in `; echo` so that setup
# can report rather than raise -- that message would be swallowed, and the
# failure would surface two layers later as `/opt/esm3/bin/python: not found`,
# naming the symptom and not the cause.
RUN python3 -m venv --system-site-packages /opt/esm3 \
    && /opt/esm3/bin/pip install --no-cache-dir --upgrade pip

RUN python3 /app/colab_worker.py setup --only esm3 --skip-weights \
        --esm3-python /opt/esm3/bin/python \
    ; echo "esm3 setup finished with $?"

# Checked separately, and allowed to fail the build on its own terms: an image
# that cannot import esm cannot serve ESM3 jobs, and finding that out here is
# the point of having a build.
#
# The assertion is the load-bearing part. ESM3 is installed without its
# dependency list precisely so that it inherits this image's torch instead of
# fetching a second one, and `import torch` succeeding does not show that it
# did -- a duplicate inside the venv would import just as happily and cost
# gigabytes. So the provenance is checked, not the import.
RUN /opt/esm3/bin/python -c "\
import torch, esm; from esm.models.esm3 import ESM3; \
assert '/opt/esm3/' not in torch.__file__, \
    'ESM3 has its own copy of torch at ' + torch.__file__ + \
    ' -- it should be inheriting the one in this image'; \
print('esm3 venv: esm', getattr(esm, '__version__', '?'), \
      '| torch', torch.__version__, 'from', torch.__file__)"

# Where the mounts will be, so a missing one is an empty directory rather than
# a container that will not start.
RUN mkdir -p /opt/models/RFdiffusion/models /models/esmfold /models/esm3 /work \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin model \
    && chown -R model:model /work

# The filesystem is read-only at run time, so everything that wants to write
# goes to /work. These are defaults; sqs_worker.py sets them again explicitly,
# because a container that is run by hand should behave the same way.
ENV HOME=/work/home \
    TMPDIR=/work/tmp \
    XDG_CACHE_HOME=/work/cache \
    TORCH_HOME=/work/cache/torch \
    MPLCONFIGDIR=/work/cache/mpl \
    PROTEINCAD_ESMFOLD_MODEL=/models/esmfold \
    PROTEINCAD_ESM3_HOME=/models/esm3 \
    PROTEINCAD_ESM3_PYTHON=/opt/esm3/bin/python \
    HF_HOME=/work/cache/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    CUDA_CACHE_DISABLE=1

# A sane default for anyone running this image by hand. The worker overrides
# it with --user set to its own uid, because the host account that owns the
# bind-mounted job directory is a system user and will not be 1000.
USER 1000
WORKDIR /work
ENTRYPOINT ["python3", "/app/model_runner.py"]
CMD ["/work/spec.json"]
