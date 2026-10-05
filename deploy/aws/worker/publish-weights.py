#!/usr/bin/env python3
"""Put the model weights in the bucket, once, with a manifest to check them by.

    deploy/aws/worker/build.sh weights

Normally run by CodeBuild rather than by you: twelve and a half gigabytes down
and the same back up is a long evening on a domestic connection and about
forty minutes on a build host in the same region as the bucket. It works fine
from a laptop too, if you would rather watch it:

    python3 -m venv .venv && . .venv/bin/activate
    pip install boto3 huggingface_hub
    python3 deploy/aws/worker/publish-weights.py --bucket proteincad-data-…

WHY THE WEIGHTS ARE IN S3 AT ALL
--------------------------------
The GPU machine is created for a job and destroyed after it, so it has no disk
that outlives anything. Keeping twelve gigabytes on a stopped instance's volume
cost sixteen dollars a month to avoid a download that takes half a minute over
a same-region link. This is that trade, made the other way round.

WHY THERE IS A MANIFEST
-----------------------
A checkpoint that is the right size and the wrong bytes loads without
complaint and produces designs that mean nothing. That is a far worse failure
than one that stops, so every file is hashed here and hashed again on the
machine before it is used.

WHAT GOES WHERE
---------------
    weights/manifest.json          sizes and SHA-256s, read by the worker
    weights/rfdiffusion/*.pt       the eight checkpoints, from the IPD
    weights/proteinmpnn/*.pt       vanilla_model_weights, from the checkout
    weights/esmfold/*              a flat snapshot of facebook/esmfold_v1
    weights/esm3/hub/*             a hub cache for the open ESM3 model (5.5 GB)

The ESMFold copy is flat on purpose -- `local_dir`, not a Hugging Face cache.
transformers takes a directory in place of a repo id, so the machine mounts it
read-only and hands the path over; there is no cache layout to reconstruct, no
symlinks to lose in transit, and nothing that wants to write a lock file.

ESM3 is the one model that cannot be published that way. It resolves its own
files by repo id rather than accepting a path, so what has to arrive on the
machine is the cache it would have downloaded into. A cache is normally a tree
of symlinks into a blob store, which does not survive an upload, so the one
published here is flattened: the snapshot's links are replaced by the files they
point at and the blob store is dropped. What is left is a cache layout made of
real files, which is what the hub reads offline.

Its weights are public -- 5.5 GB over 22 files, downloadable anonymously. A
HF_TOKEN is honoured when set, because anonymous hub downloads are rate-limited
per address, but nothing here requires one.

Idempotent: anything already in the bucket at the right size with the right
hash is left alone, so re-running after a failure resumes rather than restarts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from proteincad.colab_worker import (  # noqa: E402
    ESM3_LICENCE, ESM3_REPO, ESMFOLD_MODEL, MODELS, PROTEINMPNN_GIT, WEIGHTS,
)

PREFIX = "weights/"
CHUNK = 8 * 1024 * 1024


def say(message: str) -> None:
    print(message, flush=True)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def human(size: int) -> str:
    return f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB"


# --------------------------------------------------------------- collecting


def open_url(url: str):
    """Open a URL, preferring HTTPS where the host offers it.

    The IPD publishes its checkpoint URLs over plain HTTP, and the manifest
    this script writes is computed from whatever was downloaded -- so a
    tampered download would produce a manifest that agrees with it perfectly.
    Hashing protects the S3 copy from corruption on the way to a GPU machine;
    it does not protect this step. Asking for HTTPS first is what does, and
    files.ipd.uw.edu answers on it.
    """
    if url.startswith("http://"):
        secure = "https://" + url[len("http://"):]
        try:
            return urllib.request.urlopen(secure, timeout=120)
        except Exception:
            say(f"      ! {secure} did not answer; falling back to plain HTTP")
    return urllib.request.urlopen(url, timeout=120)


def fetch_url(url: str, path: Path) -> None:
    """Download one file, saying how it is going. These are half a gigabyte
    each and a silent five minutes reads as a hang."""
    if path.is_file() and path.stat().st_size > 1_000_000:
        say(f"    already here: {path.name} ({human(path.stat().st_size)})")
        return
    say(f"    fetching {path.name}")
    with open_url(url) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done, last = 0, -1
        with path.open("wb") as out:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                out.write(block)
                done += len(block)
                # Every five per cent, on its own line. A carriage-return
                # progress bar is right in a terminal and turns into sixty
                # lines of noise in a CloudWatch log, which is where this
                # usually runs.
                share = (done * 100 // total) if total else 0
                if total and share >= last + 5:
                    last = share - share % 5
                    say(f"      {human(done)} of {human(total)}  {last}%")


def collect_rfdiffusion(into: Path) -> None:
    """The eight checkpoints, from the URLs the worker already knows."""
    into.mkdir(parents=True, exist_ok=True)
    for name, url in sorted(WEIGHTS.items()):
        fetch_url(url, into / name)


def collect_proteinmpnn(into: Path) -> None:
    """The vanilla weights, which live inside the checkout rather than on a
    download page."""
    into.mkdir(parents=True, exist_ok=True)
    if any(into.glob("*.pt")):
        say("    already here")
        return
    if not shutil.which("git"):
        raise SystemExit("git is needed to fetch ProteinMPNN's weights")
    with tempfile.TemporaryDirectory() as staging:
        say(f"    cloning {PROTEINMPNN_GIT}")
        done = subprocess.run(["git", "clone", "--depth", "1", PROTEINMPNN_GIT, staging],
                              capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit("git clone failed:\n" + (done.stdout + done.stderr)[-800:])
        source = Path(staging) / "vanilla_model_weights"
        if not source.is_dir():
            raise SystemExit(f"no vanilla_model_weights in {PROTEINMPNN_GIT}")
        for weight in sorted(source.glob("*.pt")):
            shutil.copy2(weight, into / weight.name)
            say(f"    {weight.name} ({human(weight.stat().st_size)})")


def collect_esmfold(into: Path) -> None:
    """A flat snapshot, not a cache. See the note at the top."""
    if list(into.glob("*.safetensors")) or list(into.glob("*.bin")):
        say("    already here")
        return
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit(
            "huggingface_hub is needed to fetch the folding weights:\n"
            "  pip install huggingface_hub")
    say(f"    downloading {ESMFOLD_MODEL} — about 8.5 GB, once")
    # `local_dir` gives real files rather than links into a blob store: this
    # directory is about to be uploaded object by object and downloaded again
    # onto a machine that has never heard of a Hugging Face cache.
    snapshot_download(
        repo_id=ESMFOLD_MODEL,
        local_dir=str(into),
        # Other frameworks' copies of the same weights, which nothing here
        # loads and which would double the download.
        ignore_patterns=["*.msgpack", "*.h5", "*.onnx"],
    )


def collect_esm3(into: Path) -> None:
    """A hub cache made of real files. See the note at the top.

    Downloaded into a cache of its own and then flattened, rather than fetched
    with `local_dir` like ESMFold: the loader on the other end looks the model
    up by repo id, and a flat directory is not something it can be pointed at.
    """
    hub = into / "hub"
    if list(hub.glob("models--*/snapshots/*/*")):
        say("    already here")
        return
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit(
            "huggingface_hub is needed to fetch the ESM3 weights:\n"
            "  pip install huggingface_hub")
    say(f"    downloading {ESM3_REPO} — about 5.5 GB, once")
    with tempfile.TemporaryDirectory() as staging:
        try:
            snapshot_download(repo_id=ESM3_REPO, cache_dir=staging)
        except Exception as error:
            note = str(error)
            if any(k in note.lower() for k in ("401", "403", "gated", "awaiting")):
                raise SystemExit(
                    f"the hub refused {ESM3_REPO}: {note[:300]}\n"
                    f"These weights were public when this was written, so that reads as the "
                    f"repo having been gated since. Check {ESM3_LICENCE}, then set HF_TOKEN "
                    f"from an account allowed to read it.") from error
            raise
        # The repo directory, links resolved into real files. `blobs` is the
        # other end of those links, so once they are files it is a second copy
        # of everything and is left behind.
        hub.mkdir(parents=True, exist_ok=True)
        for repo in Path(staging).glob("models--*"):
            shutil.copytree(repo, hub / repo.name, symlinks=False,
                            ignore=shutil.ignore_patterns("blobs", "*.lock", ".locks"))
    landed = sorted(p for p in hub.rglob("*") if p.is_file())
    if not landed:
        raise SystemExit(f"nothing landed in {hub} — the download produced no files")
    say(f"    {len(landed)} file(s), {human(sum(p.stat().st_size for p in landed))}")


COLLECTORS = {
    "rfdiffusion": collect_rfdiffusion,
    "proteinmpnn": collect_proteinmpnn,
    "esmfold": collect_esmfold,
    "esm3": collect_esm3,
}


# ---------------------------------------------------------------- uploading


def already_there(s3, bucket: str, key: str, size: int, digest: str) -> bool:
    """Is this exact file already in the bucket?

    Checked by the hash we stored beside it rather than by ETag: a multipart
    ETag is not an MD5 of the object, and comparing sizes alone is what this
    whole manifest exists to distrust.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception:
        return False
    if int(head.get("ContentLength") or 0) != size:
        return False
    return (head.get("Metadata") or {}).get("sha256") == digest


def publish(bucket: str, region: str, only=None, keep=None) -> int:
    import boto3
    from boto3.s3.transfer import TransferConfig

    s3 = boto3.client("s3", region_name=region or None)
    transfer = TransferConfig(max_concurrency=16,
                              multipart_chunksize=32 * 1024 * 1024,
                              multipart_threshold=32 * 1024 * 1024)

    staging = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="proteincad-weights-"))
    staging.mkdir(parents=True, exist_ok=True)
    say(f"staging in {staging}")
    if not keep:
        say("  (pass --keep DIR to reuse the downloads next time)")

    manifest = {"version": 1, "models": {}}
    for model in MODELS:
        name = model["id"]
        if only and name not in only:
            continue
        say(f"\n=== {model['label']}")
        local = staging / name
        local.mkdir(parents=True, exist_ok=True)
        COLLECTORS[name](local)

        files, total = [], 0
        for path in sorted(p for p in local.rglob("*") if p.is_file()):
            if path.name.startswith("."):
                continue
            size = path.stat().st_size
            digest = sha256_of(path)
            key = f"{PREFIX}{name}/{path.name}"
            if already_there(s3, bucket, key, size, digest):
                say(f"    {path.name}: already in the bucket")
            else:
                say(f"    uploading {path.name} ({human(size)})")
                s3.upload_file(str(path), bucket, key, Config=transfer,
                               ExtraArgs={"Metadata": {"sha256": digest}})
            files.append({"key": key, "bytes": size, "sha256": digest})
            total += size

        if not files:
            raise SystemExit(f"nothing collected for {name}")
        manifest["models"][name] = {"label": model["label"], "bytes": total,
                                    "files": files}
        say(f"    {len(files)} file(s), {human(total)}")

    if only:
        # A partial run must not publish a manifest that forgets the rest.
        try:
            existing = json.loads(
                s3.get_object(Bucket=bucket, Key=PREFIX + "manifest.json")["Body"].read())
            merged = existing.get("models") or {}
            merged.update(manifest["models"])
            manifest["models"] = merged
        except Exception:
            say("\n! no existing manifest to merge into; this one covers only "
                + ", ".join(only))

    s3.put_object(Bucket=bucket, Key=PREFIX + "manifest.json",
                  Body=json.dumps(manifest, indent=1).encode(),
                  ContentType="application/json")

    grand = sum(m["bytes"] for m in manifest["models"].values())
    say(f"\nmanifest written. {len(manifest['models'])} model(s), {human(grand)} "
        f"— about ${grand / 1e9 * 0.023:.2f} a month in S3.")
    if not keep:
        say(f"\nThe staging copy is still at {staging}; delete it when you are happy.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="fetch the model weights and publish them to the bucket")
    parser.add_argument("--bucket", required=True,
                        help="the stack's Bucket output")
    parser.add_argument("--region", default="",
                        help="defaults to your configured region")
    parser.add_argument("--only", nargs="*", choices=sorted(COLLECTORS),
                        help="publish just these, and merge into the existing manifest")
    parser.add_argument("--keep", metavar="DIR",
                        help="stage the downloads here instead of a temp directory, "
                             "so a second run does not fetch them again")
    args = parser.parse_args(argv)
    try:
        return publish(args.bucket, args.region, args.only, args.keep)
    except KeyboardInterrupt:
        say("\ninterrupted — re-run it; anything already uploaded is skipped")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
