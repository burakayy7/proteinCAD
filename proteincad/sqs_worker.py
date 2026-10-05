"""The GPU side: take one job off the queue, run it in a container, put the
results in the bucket.

Runs on the EC2 instance as a systemd service. Nothing reaches it from the
internet -- its security group has no inbound rules at all -- so the only way to
give it work is to put a message on the queue, and the only thing that can do
that is the submit Lambda, on behalf of a signed-in user.

    SQS  ──►  claim in DynamoDB  ──►  docker run  ──►  S3  ──►  DynamoDB

WHAT RUNS THE MODEL
-------------------
Not this process. This process owns the AWS credentials, and the model is
several hundred megabytes of other people's research code; those two should not
be the same process. So the model runs in a container with `--network=none` and
no credentials of any kind, talking to the outside world through one bind-
mounted directory. If a model run were ever made to execute something it
should not, it would find no network, no instance role, no metadata service and
a read-only filesystem.

RETRIES, AND WHY THERE ARE ALMOST NONE
--------------------------------------
A queue that retries is the usual default and the wrong one here. A job that
fails because the spec asks for something impossible will fail again the same
way, and each attempt costs an hour of a card that bills by the second. So:

    the container exits non-zero, or times out  ->  the job is failed and the
                                                    message is DELETED
    this process dies mid-job                   ->  the message is left, the
                                                    visibility timeout lapses,
                                                    SQS hands it back

Only the second is a retry, and it is the only one worth having. The dead
letter queue therefore collects exactly one kind of thing: a message that has
killed this worker twice, which is the thing worth looking at by hand.

WEIGHTS ARRIVE WHEN THEY ARE WANTED
-----------------------------------
This machine is created for a job and destroyed after one, so it starts with no
weights at all -- only a container image. Models come from the bucket on
demand: because somebody pressed Download, or because a job needs one and
nobody did. Each file is checked against a SHA-256 in weights/manifest.json
before it is used, because a truncated checkpoint that loads and produces
nonsense is a far worse failure than one that stops here.

That is what the machine record in DynamoDB is for. This process writes what it
is doing into it -- booting, ready, downloading, how many bytes of how many --
and the panel reads it. Nothing about a machine that does not exist most of the
time can be discovered by asking EC2.

STOPPING
--------
Nothing else turns this machine off, and nothing else can: no role in the
account has StopInstances or TerminateInstances. When the queue has been empty
and nothing has run for PROTEINCAD_IDLE_MINUTES, this process runs
`shutdown -h now`, and the launch template's InstanceInitiatedShutdownBehavior
turns that into a termination. The instance, its disk and its downloaded
weights all cease to exist, which is the point: idle costs nothing because
there is nothing.

    PROTEINCAD_QUEUE           queue url
    PROTEINCAD_TABLE           DynamoDB table
    PROTEINCAD_BUCKET          S3 bucket
    PROTEINCAD_IMAGE           the model image: a tag or, better, a digest
    PROTEINCAD_INSTANCE        this instance (default: ask the metadata service)
    PROTEINCAD_REGION          region for the clients
    PROTEINCAD_WORK            scratch directory        (/mnt/fast/work)
    PROTEINCAD_WEIGHTS         where models land        (/mnt/fast/weights)
    PROTEINCAD_JOB_TIMEOUT     seconds per job                        (3600)
    PROTEINCAD_JOB_MEMORY      container memory limit                 (12g)
    PROTEINCAD_IDLE_MINUTES    idle before shutting down; 0 never       (30)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from .cloud_api import (
    ABSENT, CANCELLED, DONE, DOWNLOADING, FAILED, GONE, QUEUED, READY, RUNNING, WANTED,
    _is_conditional, client, code_version, item_to_dict, job_key, machine_key,
    model_key, number, read_machine, set_fields, setting,
)
from .colab_worker import MODELS_BY_ID, models_for
from .design import count_atoms

# How long a heartbeat may be stale before another worker may take the job.
# Well inside the queue's visibility timeout, so a message is never handed back
# while the worker holding it is still alive.
STALE_AFTER = 180

# SQS long poll. This is also how long a Download press waits before anything
# happens: the loop services downloads, then blocks here, so a button pressed
# one moment after the block started is not noticed until it ends. Twenty
# seconds of a row that says "queued" and a bar that is not moving reads as a
# button that did nothing -- which is what it was. Five costs four times the
# ReceiveMessage calls on one worker that only exists while it is working,
# which is pennies a month, and is the difference between "queued" being a
# blink and being long enough to press again.
POLL_SECONDS = 5
HEARTBEAT = 30

# The dead-man switch, in minutes.
#
# One thing ends a GPU machine: this worker running `shutdown -h now`. Nothing
# in the account may terminate an instance, which is the property that makes
# the whole design safe -- and the hole in it is a machine where this process
# never starts. A boot that hangs somewhere its script did not anticipate
# leaves an instance with no worker, no shutdown and no way for the panel even
# to see it: invisible, immortal, and billing by the second.
#
# So the instance schedules its own death at boot, before anything that could
# hang, and a living worker keeps pushing it back. Ninety minutes is past any
# single job (the timeout is sixty) plus a boot, so a healthy machine always
# ends by one of the ordinary routes long before this fires.
DEADMAN_MINUTES = 90
# How often to push it back. Far enough inside the window that losing one push
# to a slow DynamoDB call or a long download changes nothing.
DEADMAN_EVERY = 600

# How often the machine record is updated while a model is arriving. A boto3
# progress callback fires per chunk -- thousands of times for eight gigabytes --
# and a DynamoDB write per chunk would cost more than the download.
PROGRESS_EVERY = 1.0

MANIFEST = "weights/manifest.json"


def say(message: str) -> None:
    print(f"[worker] {message}", flush=True)


def sha256_of(path: Path, block: int = 4 * 1024 * 1024) -> str:
    """Hash a file that may be eight gigabytes, without holding it in memory.

    Done after the download rather than during it because a multipart transfer
    writes its parts out of order, so there is no stream to hash as it goes.
    Reading it back off the instance store costs a few seconds and is the
    entire cost of knowing the weights are the weights.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------- the machine


def instance_id() -> str:
    """Which instance this is.

    Configured if the stack said so, and otherwise asked of the metadata
    service -- IMDSv2, because the instance requires it and the hop limit is
    one, which is also why no container can ask the same question.
    """
    configured = setting("INSTANCE")
    if configured:
        return configured
    try:
        token = urllib.request.urlopen(urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"}), timeout=2).read().decode()
        return urllib.request.urlopen(urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token}), timeout=2).read().decode().strip()
    except Exception as error:
        say(f"could not ask the metadata service which instance this is: {error}")
        return ""


def check_image(image: str) -> str:
    """Refuse to run anything but a pinned image.

    `latest` means "whatever was pushed most recently", which on a box that
    boots when a job arrives means a job can silently run code nobody chose. A
    digest is better than a tag and both are better than this.
    """
    image = (image or "").strip()
    if not image:
        raise SystemExit(
            "PROTEINCAD_IMAGE is not set. It is written at boot from the SSM parameter "
            "the image build pins, so an empty one means that build has never run: "
            "deploy/aws/worker/build.sh image")
    if image.endswith(":latest") or image == "latest":
        raise SystemExit(
            f"PROTEINCAD_IMAGE is {image!r}. This worker will not run `latest`: a machine "
            "created on demand would pick up whatever happened to be pushed last. "
            "deploy/aws/worker/build.sh image pins a digest.")
    return image


# ------------------------------------------------------------------ the work


class Worker:
    def __init__(self, run_container=None, sleep=time.sleep, clock=time.time,
                 shutdown=None, download=None, deadman=None):
        self.queue = setting("QUEUE")
        self.table = setting("TABLE")
        self.bucket = setting("BUCKET")
        self.image = check_image(setting("IMAGE"))
        self.instance = instance_id()
        self.work_root = Path(setting("WORK", "/mnt/fast/work"))
        self.weights = Path(setting("WEIGHTS", "/mnt/fast/weights"))
        self.timeout = number("JOB_TIMEOUT", 3600)
        self.memory = setting("JOB_MEMORY", "12g")
        self.idle_seconds = number("IDLE_MINUTES", 30) * 60

        # Injected so the checks can drive a whole job -- and a whole machine
        # lifetime -- without Docker, an instance, an hour to wait in, or the
        # ability to turn the test runner's computer off.
        self._run_container = run_container or self._docker
        self._download = download or self._s3_download
        self.shutdown = shutdown or self._poweroff
        self._reschedule = deadman or self._schedule_poweroff
        self._deadman_at = 0.0
        self._sleep = sleep
        self._clock = clock

        self._manifest = None
        self._model_locks: dict = {}
        self._locks_guard = threading.Lock()

        self.last_worked = clock()
        self.stopped = False

    # -- the loop ---------------------------------------------------------

    def run(self, rounds=None) -> None:
        say(f"draining {self.queue}")
        say(f"model image {self.image}")
        # Clears the boot stage as well as setting the state: a machine that is
        # ready is not still "downloading the model software".
        self.announce(READY, stage="", step=0, steps=0)
        self.reconcile_models()
        turn = 0
        while rounds is None or turn < rounds:
            turn += 1
            self.push_deadman()
            # Somebody may have pressed Download while nothing was queued, and
            # that is the whole point of the button: get the weights in place
            # before there is a job waiting on them.
            found = self.serve_downloads()
            # Asked to stand down. Checked here, between jobs, because that is
            # the only place it can be honoured without interrupting one -- and
            # shut_down() stays up if anything is waiting on the queue, so the
            # worst a retire can do to somebody else is make them wait for a
            # new machine rather than lose a design to this one.
            if self.asked_to_retire(found):
                self.shut_down("asked to retire -- shutting down")
                if self.stopped:
                    return
            message = self.receive()
            if message is not None:
                self.last_worked = self._clock()
                self.handle(message)
                self.last_worked = self._clock()
                continue
            if self.time_to_stop():
                self.shut_down()
                return

    def receive(self):
        answer = client("sqs").receive_message(
            QueueUrl=self.queue, MaxNumberOfMessages=1, WaitTimeSeconds=POLL_SECONDS,
            VisibilityTimeout=900)
        messages = answer.get("Messages") or []
        return messages[0] if messages else None

    def queue_depth(self) -> int:
        attributes = client("sqs").get_queue_attributes(
            QueueUrl=self.queue,
            AttributeNames=["ApproximateNumberOfMessagesVisible",
                            "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return (int(attributes.get("ApproximateNumberOfMessagesVisible", 0))
                + int(attributes.get("ApproximateNumberOfMessagesNotVisible", 0)))

    def time_to_stop(self) -> bool:
        """Idle long enough, and with nothing waiting.

        Both halves matter. A clock alone would stop the box while a message
        sat unread because a receive had just timed out; the queue alone would
        keep it up forever the first time a message was left in flight.
        """
        if not self.idle_seconds or self.stopped:
            return False
        if self._clock() - self.last_worked < self.idle_seconds:
            return False
        try:
            waiting = self.queue_depth()
        except Exception as error:
            # Cannot see the queue, so cannot know it is empty. Staying up
            # costs money; stopping with work waiting costs a job.
            say(f"cannot read the queue depth, staying up: {error}")
            return False
        if waiting:
            say(f"idle, but {waiting} message(s) are waiting")
            self.last_worked = self._clock()
            return False
        return True

    def shut_down(self, why: str = "") -> None:
        """Turn this machine off, which destroys it.

        There is no API call here and there is no permission for one. The
        launch template says InstanceInitiatedShutdownBehavior=terminate, so
        `shutdown -h now` is the whole mechanism: the instance, its root volume
        and every weight downloaded to it stop existing together. Idle costs
        nothing because idle *is* nothing.
        """
        minutes = self.idle_seconds // 60
        say(why or f"idle for {minutes} minutes and the queue is empty -- shutting down")

        # Said before going, so a browser window open on the panel is told the
        # machine has gone rather than working it out from a stale heartbeat
        # two minutes later.
        self.announce(GONE)

        # One last look. A message can arrive between the check that decided to
        # stop and this line; the five-minute rule in the stack catches the
        # gap, but not paying for it is better than catching it.
        try:
            if self.queue_depth():
                say("something arrived while shutting down -- staying up")
                self.announce(READY)
                return
        except Exception as error:
            say(f"could not re-check the queue, going anyway: {error}")

        self.stopped = True
        self.shutdown()

    def _poweroff(self) -> None:  # pragma: no cover - the real thing
        subprocess.run(["sudo", "/sbin/shutdown", "-c"], capture_output=True)
        subprocess.run(["sudo", "/sbin/shutdown", "-h", "now"], capture_output=True)

    def _schedule_poweroff(self) -> None:  # pragma: no cover - the real thing
        subprocess.run(["sudo", "/sbin/shutdown", "-c"], capture_output=True)
        subprocess.run(["sudo", "/sbin/shutdown", "-h", f"+{DEADMAN_MINUTES}",
                        "proteincad: no worker has reported in; ending this machine"],
                       capture_output=True)

    def push_deadman(self) -> None:
        """Move the scheduled shutdown further out, because we are still here.

        The instance booted with one already set. Every push is this process
        saying so again; if it stops saying so -- wedged, crashed, killed --
        the last one it set arrives on its own and the machine ends. That is
        the only path that does not depend on this worker being alive to take
        it.
        """
        if self._clock() - self._deadman_at < DEADMAN_EVERY:
            return
        self._deadman_at = self._clock()
        try:
            self._reschedule()
        except Exception as error:
            # Worth a line, never worth the job: a machine that cannot push the
            # switch back still runs what it has, and still ends at the switch.
            say(f"could not push the shutdown deadline back: {error}")

    # -- the machine record ----------------------------------------------

    def announce(self, state: str, **fields) -> None:
        """Say what this machine is doing. The panel reads only this."""
        try:
            client("dynamodb").update_item(
                TableName=self.table, Key=machine_key(),
                # `build` is which deployment this machine booted with. The
                # API compares it against its own; a machine created before a
                # deploy keeps running the code it started with, and until it
                # said so there was no way to tell that from a broken fix.
                **set_fields(state=state, heartbeat=self._clock(),
                             instance_id=self.instance, build=code_version(),
                             **fields))
        except Exception as error:
            # A machine that cannot write its own record is still a machine
            # that can run jobs. Losing the panel is not worth losing the work.
            say(f"could not update the machine record: {error}")

    def asked_to_retire(self, found: dict) -> bool:
        """Has somebody asked *this* machine to stand down?

        Matched against this instance, not merely present, for the same reason
        the model records are stamped: these rows outlive the machine. An
        unmatched flag left by the machine before would retire its replacement
        the moment it booted, and the one after that, for ever.
        """
        state = (found or {}).get("state") or {}
        asked = state.get("retire_instance", "")
        return bool(asked) and asked == self.instance

    def model_state(self, name: str, state: str, **fields) -> None:
        """Say where a model is, and *whose disk it is on*.

        These records outlive the machine. The weights do not -- they are on an
        instance store that is created and destroyed with the instance -- so a
        record without an instance id beside it is a claim that cannot be
        checked, and the panel believed one from a machine that no longer
        existed.
        """
        try:
            client("dynamodb").update_item(
                TableName=self.table, Key=model_key(name),
                **set_fields(state=state, instance_id=self.instance, **fields))
        except Exception as error:
            say(f"could not update {name}: {error}")

    # -- weights ----------------------------------------------------------

    def manifest(self) -> dict:
        """What is in the bucket, how big, and what it should hash to.

        Read once. It is the only thing standing between a half-downloaded
        checkpoint and a design that looks plausible and means nothing.
        """
        if self._manifest is None:
            self._manifest = json.loads(self.read(MANIFEST).decode("utf-8"))
        return self._manifest

    def model_dir(self, name: str) -> Path:
        return self.weights / name

    def have_model(self, name: str) -> bool:
        """Written only after every file has matched its hash."""
        return (self.model_dir(name) / ".complete").is_file()

    def _lock_for(self, name: str):
        with self._locks_guard:
            if name not in self._model_locks:
                self._model_locks[name] = threading.Lock()
            return self._model_locks[name]

    def reconcile_models(self) -> None:
        """Make the model records true of *this* disk, once, at startup.

        These records outlive the machine; the weights do not. A machine
        created for one job and destroyed after it starts with an empty
        instance store, and the records from the machine before it are still
        sitting there saying `ready`. The panel then offers weights that are
        nowhere, and Run goes green against them.

        Only a claim of presence is corrected. A model somebody asked for
        before this process restarted is still wanted, and a download that
        failed still failed -- overwriting either would lose a request or hide
        a reason. What cannot survive a new disk is `ready`.
        """
        try:
            found = read_machine()
        except Exception as error:
            # A machine that cannot read its own records can still run jobs.
            say(f"could not reconcile the model records: {error}")
            return

        for name in MODELS_BY_ID:
            record = (found.get("models") or {}).get(name) or {}
            if self.have_model(name):
                # Stamped with this machine, so the panel stops reading it as
                # some other machine's and reporting it absent.
                if (record.get("state") != READY
                        or record.get("instance_id") != self.instance):
                    self.model_state(name, READY)
            elif record.get("state") == READY:
                say(f"{name} is not on this disk; the record said it was")
                self.model_state(name, ABSENT, bytes=0, error="")

    def download_model(self, name: str) -> bool:
        """Fetch one model from the bucket and prove it arrived intact.

        Per-model lock rather than one global one: two models asked for at once
        should both make progress, and they compete for the network rather than
        for each other.
        """
        with self._lock_for(name):
            if self.have_model(name):
                self.model_state(name, READY)
                return True
            try:
                return self._fetch(name)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                say(f"{name} failed: {message}")
                self.model_state(name, FAILED, error=message)
                return False

    def _fetch(self, name: str) -> bool:
        entry = (self.manifest().get("models") or {}).get(name)
        if not entry:
            raise RuntimeError(
                f"the weights manifest has no {name!r}. Run "
                "deploy/aws/worker/publish-weights.py against this bucket.")

        files = entry.get("files") or []
        total = int(entry.get("bytes") or sum(int(f.get("bytes") or 0) for f in files))
        where = self.model_dir(name)
        where.mkdir(parents=True, exist_ok=True)

        label = MODELS_BY_ID.get(name, {}).get("label", name)
        say(f"downloading {label}: {len(files)} file(s), {total // 1_000_000} MB")
        self.model_state(name, DOWNLOADING, bytes=0, total=total, error="")

        done = [0]
        last = [0.0]

        def progress(chunk):
            done[0] += chunk
            if self._clock() - last[0] < PROGRESS_EVERY:
                return
            last[0] = self._clock()
            self.model_state(name, DOWNLOADING, bytes=done[0], total=total)

        for item in files:
            key = item["key"]
            target = where / Path(key).name
            part = target.with_suffix(target.suffix + ".part")
            self._download(key, part, progress)

            got = sha256_of(part)
            want = item.get("sha256", "")
            if want and got != want:
                # Deleted rather than kept: a file that is the right size and
                # the wrong bytes is the one failure mode that would otherwise
                # survive into a design and look like a result.
                part.unlink(missing_ok=True)
                raise RuntimeError(
                    f"{Path(key).name} did not match the manifest.\n"
                    f"  expected {want}\n  got      {got}\n"
                    "The copy in the bucket is damaged; re-run publish-weights.py.")
            part.replace(target)

        (where / ".complete").write_text(json.dumps({"at": self._clock(), "bytes": total}))
        self.model_state(name, READY, bytes=total, total=total, error="")
        say(f"{label} ready")
        return True

    def _s3_download(self, key: str, path: Path, progress) -> None:  # pragma: no cover
        from boto3.s3.transfer import TransferConfig

        client("s3").download_file(
            self.bucket, key, str(path),
            Config=TransferConfig(max_concurrency=16,
                                  multipart_chunksize=32 * 1024 * 1024,
                                  multipart_threshold=32 * 1024 * 1024),
            Callback=progress)

    def serve_downloads(self) -> dict:
        """Fetch anything somebody pressed the button for.

        Returns the machine record it read, because the loop wants it too and
        one read a turn is enough.

        A thread each, rather than one after another. They share a network
        card, so two together do not finish sooner in total -- but ProteinMPNN
        is a hundred megabytes and ESMFold is eight and a half gigabytes, and
        making the small one wait forty seconds behind the big one is forty
        seconds of somebody watching a row that says `queued`. The per-model
        locks are what make it safe; each writes to its own directory.
        """
        try:
            found = read_machine()
        except Exception as error:
            say(f"could not read the machine record: {error}")
            return {}
        self.announce(READY)

        wanted = [name for name, record in (found.get("models") or {}).items()
                  if record.get("state") == WANTED]
        if not wanted:
            return found
        if len(wanted) == 1:
            self.download_model(wanted[0])
            return found

        threads = [threading.Thread(target=self.download_model, args=(name,),
                                    name="download-" + name, daemon=True)
                   for name in wanted]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return found

    def ensure_models(self, spec: dict) -> list:
        """Everything this job needs, fetched if it is not here.

        Which is what makes the buttons optional rather than required: pressing
        Run with nothing downloaded works, it just spends the download now
        instead of earlier.
        """
        missing = []
        for name in models_for(spec):
            if self.have_model(name):
                self.model_state(name, READY)
                continue
            if not self.download_model(name):
                missing.append(name)
        return missing

    # -- one job ----------------------------------------------------------

    def handle(self, message) -> None:
        """Run one message, and decide what happens to it.

        Every failure inside here is the job's, and ends with the message
        deleted. A failure of this process is not caught, and ends with the
        message still on the queue -- which is the only retry there is.
        """
        try:
            body = json.loads(message["Body"])
            job_id = body["job_id"]
        except Exception as error:
            say(f"unreadable message, dropping it: {error}")
            return self.delete(message)

        job = self.claim(job_id)
        if job is None:
            say(f"job {job_id} was already taken or cancelled")
            return self.delete(message)

        work = self.work_root / job_id
        try:
            self.work(job_id, job, work, message)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.delete(message)

    def claim(self, job_id: str):
        """Take the job, if it is still there to take.

        `queued` is the normal case. The second half is the redelivery case:
        this worker's predecessor died, its heartbeat went stale, and SQS has
        handed the message back. A job whose worker is still alive has a fresh
        heartbeat and cannot be taken.
        """
        stale = self._clock() - STALE_AFTER
        fields = set_fields(status=RUNNING, started=self._clock(),
                            heartbeat=self._clock(), stage="starting")
        fields["ExpressionAttributeNames"]["#s"] = "status"
        fields["ExpressionAttributeNames"]["#h"] = "heartbeat"
        fields["ExpressionAttributeValues"][":queued"] = {"S": QUEUED}
        fields["ExpressionAttributeValues"][":stale"] = {"N": repr(stale)}
        try:
            answer = client("dynamodb").update_item(
                TableName=self.table, Key=job_key(job_id),
                ConditionExpression="#s = :queued OR #h < :stale",
                ReturnValues="ALL_NEW", **fields)
        except Exception as error:
            if _is_conditional(error):
                return None
            raise
        # The same call that takes the job hands back what it now says, so
        # there is no window in which this reads a record somebody else has
        # changed underneath it.
        return item_to_dict((answer or {}).get("Attributes") or {})

    def work(self, job_id, job, work, message) -> None:
        spec = json.loads(self.read(job["spec_key"]).decode("utf-8"))

        # Whatever this job needs, here before the container starts. Nobody had
        # to press a button; pressing one only moves the wait earlier.
        self.stage(job_id, "fetching the models this job needs")
        missing = self.ensure_models(spec)
        if missing:
            return self.finish(job_id, FAILED, designs=[], error=(
                "could not fetch " + ", ".join(missing) + " from the weights bucket. "
                "The machine record says why."))

        work.mkdir(parents=True, exist_ok=True)
        for name in ("designs", "tmp", "cache", "home", "schedules"):
            (work / name).mkdir(exist_ok=True)
        (work / "spec.json").write_text(json.dumps(spec))
        (work / "progress.json").write_text(json.dumps({"stage": "starting", "progress": 0}))

        beat = self.heartbeat(job_id, work, message)
        beat.start()
        try:
            outcome = self.run_container(job_id, work)
        finally:
            beat.stop()

        if beat.cancelled:
            say(f"job {job_id} cancelled")
            return self.finish(job_id, CANCELLED, error="", designs=[])

        if outcome.get("timed_out"):
            return self.finish(job_id, FAILED, designs=[], error=(
                f"the job hit the {self.timeout // 60} minute limit and was stopped. "
                "Fewer designs, or a smaller target, will finish inside it."))

        if outcome.get("code"):
            return self.finish(job_id, FAILED, designs=[], error=(
                f"the model exited with status {outcome['code']}. "
                + (outcome.get("tail") or "There was no output to explain why.")))

        result = self.collect(job_id, work)
        if result["designs"]:
            self.finish(job_id, DONE, designs=result["designs"], error=result["error"],
                        command=result.get("command", ""))
        else:
            self.finish(job_id, FAILED, designs=[], command=result.get("command", ""),
                        error=result["error"] or "the model produced no designs")

    def stage(self, job_id: str, text: str) -> None:
        """Say what a job is waiting on, before the container exists to say it."""
        say(f"job {job_id}: {text}")
        try:
            client("dynamodb").update_item(
                TableName=self.table, Key=job_key(job_id), **set_fields(stage=text))
        except Exception:
            pass

    def heartbeat(self, job_id: str, work: Path, message):
        """Made through a method so the checks can drive the cancel path
        without waiting out a real one."""
        return Heartbeat(self, job_id, work, message)

    # -- the container ----------------------------------------------------

    def container_command(self, job_id: str, work: Path) -> list:
        """Everything the model is allowed, written out in one place.

        Read it as a list of things it cannot do: reach the network, write
        anywhere but /work, gain a privilege, see a credential, use more than
        its share of the memory, or outlive the run.
        """
        return [
            "docker", "run", "--rm",
            "--name", "proteincad-" + job_id,
            "--gpus", "all",
            # As whoever owns the directory being written to.
            #
            # The image bakes in a `model` user at uid 1000, and the host user
            # this runs as is a *system* account -- `useradd --system` hands
            # out uids below 1000, 995 here. So the job directory belongs to
            # 995, the container writes as 1000, and the run dies on its own
            # output: PermissionError: '/work/result.json.tmp'.
            #
            # Taking the uid from this process rather than naming one keeps
            # the two in step however the host account was created.
            "--user", f"{os.getuid()}:{os.getgid()}",
            # No network at all. The weights are already on disk, and there is
            # nothing this needs to fetch; without this the instance role is
            # one metadata request away.
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            # This instance has 16 GiB. Leaving four for the host and this
            # process is what keeps an over-large job from taking the worker
            # down with it -- the container is killed, the job fails, and the
            # box carries on.
            "--memory", self.memory, "--memory-swap", self.memory,
            "--pids-limit", "512",
            # Small, because the big intermediates belong on the volume rather
            # than in RAM that the model also needs.
            "--tmpfs", "/tmp:size=1g,mode=1777",
            "-v", f"{work}:/work:rw",
            # RFdiffusion caches the IGSO3 schedules it computes in a directory
            # beside its own source, and creates that directory if it is not
            # there -- both of which are writes into an image layer that is
            # read-only here. The model passes
            # `inference.schedule_directory_path` to move them into /work, and
            # this makes the old path writable as well, so the container
            # survives a model that does not: a worker image built before that
            # override existed still runs, instead of dying on
            # `[Errno 30] Read-only file system: '.../schedules'` a minute in.
            #
            # A bind mount rather than a tmpfs, because a cache file here is
            # 10-20 MB and the container's memory limit is the model's.
            "-v", f"{work}/schedules:/opt/models/RFdiffusion/schedules:rw",
            # The image carries the code and the dependency stack; the bucket
            # carries the weights, and they arrive on this machine's instance
            # store minutes before this line runs. Splitting them that way
            # keeps the image around seven gigabytes instead of thirty, and is
            # what makes --network none possible: there is nothing left for a
            # run to download.
            #
            # Each is mounted where its own tool already looks, so nothing has
            # to be told a new path: RFdiffusion reads <repo>/models,
            # ProteinMPNN reads <repo>/vanilla_model_weights, and ESMFold is
            # handed a directory instead of a Hugging Face repo id -- which is
            # why there is no HF cache here at all any more.
            "-v", f"{self.model_dir('rfdiffusion')}:/opt/models/RFdiffusion/models:ro",
            "-v", f"{self.model_dir('proteinmpnn')}:"
                  f"/opt/models/ProteinMPNN/vanilla_model_weights:ro",
            "-v", f"{self.model_dir('esmfold')}:/models/esmfold:ro",
            # ESM3 is the exception to the sentence above: it resolves its files
            # by repo id rather than taking a path, so what is mounted is the
            # hub cache it would otherwise have downloaded into. Read-only like
            # the rest, which is why its lock directory is sent elsewhere --
            # see esm3_environment().
            "-v", f"{self.model_dir('esm3')}:/models/esm3:ro",
            # Every writable path the stack expects is redirected into /work,
            # because the root filesystem is read-only and a library that
            # cannot write its cache fails in a way that reads as a model
            # error.
            "-e", "HOME=/work/home",
            "-e", "TMPDIR=/work/tmp",
            # With --user there is no passwd entry for that uid inside the
            # container, so anything calling getpass.getuser() would raise
            # rather than return a name. Both are checked before pwd is.
            "-e", "USER=proteincad",
            "-e", "LOGNAME=proteincad",
            "-e", "XDG_CACHE_HOME=/work/cache",
            "-e", "TORCH_HOME=/work/cache/torch",
            "-e", "MPLCONFIGDIR=/work/cache/mpl",
            "-e", "CUDA_CACHE_DISABLE=1",
            # A directory, not a repo id. transformers takes either, and a
            # directory needs no cache layout, no symlinks and no lock files --
            # which is what let the Hugging Face cache leave this design.
            "-e", "PROTEINCAD_ESMFOLD_MODEL=/models/esmfold",
            "-e", "PROTEINCAD_ESM3_HOME=/models/esm3",
            "-e", "PROTEINCAD_ESM3_PYTHON=/opt/esm3/bin/python",
            # Writable, and deliberately not the mount above: the hub wants to
            # write locks beside a cache it is only reading.
            "-e", "HF_HOME=/work/cache/hf",
            # Belt and braces. With no network these would fail anyway; saying
            # it up front turns a timeout into an immediate, readable error.
            "-e", "HF_HUB_OFFLINE=1",
            "-e", "TRANSFORMERS_OFFLINE=1",
            self.image,
            "/work/spec.json",
        ]

    def ensure_mount_dirs(self) -> None:
        """Make every weights directory before docker is asked to mount one.

        All three are bind-mounted on every run, whether the job needs them or
        not. Docker creates a bind-mount source that is not there -- and creates
        it as **root**, because the daemon is root. This process is not: it runs
        as a system account, so the next download into that directory dies with

            PermissionError: [Errno 13] Permission denied:
              '/mnt/fast/weights/esmfold/CACHEDIR.TAG.part.307CEa'

        (the suffix is boto3's own temporary file, written beside the target).

        Which is why a model could be downloaded happily before the first
        design and refuse ever after: the first container run is what created
        the directories, and it did not create them for us. Making them here,
        as this user, leaves docker nothing to invent.
        """
        for name in MODELS_BY_ID:
            try:
                self.model_dir(name).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                say(f"could not make the weights directory for {name}: {error}")

    def run_container(self, job_id: str, work: Path) -> dict:
        self.ensure_mount_dirs()
        return self._run_container(self.container_command(job_id, work), job_id, self.timeout)

    def _docker(self, command: list, job_id: str, timeout: int) -> dict:  # pragma: no cover
        say("$ " + " ".join(command))
        try:
            finished = subprocess.run(command, timeout=timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "stop", "-t", "20", "proteincad-" + job_id],
                           capture_output=True)
            return {"timed_out": True, "code": None, "tail": ""}
        output = (finished.stdout or "") + (finished.stderr or "")
        for line in output.splitlines()[-40:]:
            say("  " + line[:200])
        return {"timed_out": False, "code": finished.returncode,
                "tail": " ".join(output.splitlines()[-4:])[:600]}

    def stop_container(self, job_id: str) -> None:  # pragma: no cover
        subprocess.run(["docker", "stop", "-t", "20", "proteincad-" + job_id],
                       capture_output=True)

    # -- results ----------------------------------------------------------

    def collect(self, job_id: str, work: Path) -> dict:
        """Read what the container left, and put the coordinates in the bucket."""
        try:
            result = json.loads((work / "result.json").read_text())
        except Exception as error:
            return {"designs": [], "error": f"the model left no result file ({error})"}

        designs = []
        for index, design in enumerate(result.get("designs") or []):
            path = work / design.get("file", "")
            if not path.is_file():
                continue
            pdb = path.read_text()
            atoms = count_atoms(pdb)
            if not atoms:
                # A result with no coordinates is not a design. Storing it
                # would turn a run that produced nothing into a job that reads
                # as finished.
                continue
            key = "results/%s/design_%03d.pdb" % (job_id, index)
            client("s3").put_object(Bucket=self.bucket, Key=key,
                                    Body=pdb.encode("utf-8"),
                                    ContentType="chemical/x-pdb")
            designs.append({"name": design.get("name", "design_%d" % (index + 1)),
                            "metrics": design.get("metrics") or {},
                            "atoms": atoms, "key": key})

        return {"designs": designs, "error": result.get("error", ""),
                "command": result.get("log", "")}

    def finish(self, job_id: str, status: str, designs, error: str = "",
               command: str = "") -> None:
        say(f"job {job_id} {status}" + (f": {error[:120]}" if error else ""))
        fields = {"status": status, "error": error, "finished": self._clock(),
                  "designs": designs, "progress": len(designs), "stage": ""}
        if command:
            fields["command"] = command
        client("dynamodb").update_item(
            TableName=self.table, Key=job_key(job_id), **set_fields(**fields))

    # -- the bits that touch a bucket or a queue --------------------------

    def read(self, key: str) -> bytes:
        body = client("s3").get_object(Bucket=self.bucket, Key=key)["Body"]
        return body.read() if hasattr(body, "read") else body

    def delete(self, message) -> None:
        client("sqs").delete_message(QueueUrl=self.queue,
                                     ReceiptHandle=message["ReceiptHandle"])


class Heartbeat(threading.Thread):
    """Says the job is still alive, reads what it is doing, and watches for a
    cancel.

    Three jobs in one thread because they share a period and an early exit: all
    three stop the moment the container does, and none of them is worth a
    thread of its own.
    """

    daemon = True

    def __init__(self, worker: Worker, job_id: str, work: Path, message):
        super().__init__(name="proteincad-heartbeat")
        self.worker = worker
        self.job_id = job_id
        self.work = work
        self.message = message
        self.cancelled = False
        self._done = threading.Event()
        self._last = {}

    def stop(self) -> None:
        self._done.set()
        if self.is_alive():
            self.join(timeout=5)

    def run(self) -> None:
        while not self._done.wait(HEARTBEAT):
            try:
                self.beat()
            except Exception as error:  # never take the job down with it
                say(f"heartbeat: {error}")

    def beat(self) -> None:
        # Keep the message out of anybody else's hands while this runs.
        client("sqs").change_message_visibility(
            QueueUrl=self.worker.queue, ReceiptHandle=self.message["ReceiptHandle"],
            VisibilityTimeout=900)

        progress = {}
        try:
            progress = json.loads((self.work / "progress.json").read_text())
        except Exception:
            pass

        fields = {"heartbeat": self.worker._clock()}
        for key in ("stage", "progress", "command"):
            if key in progress and progress[key] != self._last.get(key):
                fields[key] = progress[key]
                self._last[key] = progress[key]

        answer = client("dynamodb").update_item(
            TableName=self.worker.table, Key=job_key(self.job_id),
            ReturnValues="ALL_NEW", **set_fields(**fields))

        job = item_to_dict((answer or {}).get("Attributes") or {})
        if job.get("cancel"):
            self.cancelled = True
            self._done.set()
            say(f"job {self.job_id} was cancelled; stopping the container")
            self.worker.stop_container(self.job_id)


def main(argv=None) -> int:
    for required in ("QUEUE", "TABLE", "BUCKET"):
        if not setting(required):
            raise SystemExit(
                f"PROTEINCAD_{required} is not set. This worker is configured entirely from "
                "/etc/proteincad/worker.env, which the launch template's boot script writes. "
                "If that file is missing or short, the boot failed: "
                "sudo cat /var/log/proteincad-boot.log")
    worker = Worker()
    try:
        worker.run()
    except KeyboardInterrupt:
        say("stopping")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
