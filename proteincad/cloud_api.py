"""The API as it runs on AWS Lambda, in front of a queue.

A second front end for the same app. The local one in api.py owns a job queue,
runs the work in a thread and writes results next to itself on disk; that is
right for one person on one machine and wrong for a website, where the process
answering the browser must not be the process holding a GPU.

So out here the request path stops at "written down and queued":

    POST /design          validate, bill it to the caller, put the spec in S3,
                          the record in DynamoDB and a pointer on SQS, start
                          the GPU box, return the job
    GET  /jobs            the caller's own jobs, newest first
    GET  /jobs/{id}/designs/{i}   a presigned S3 link, good for five minutes
    GET  /gpu             where the box is. Read-only: see below.

Nothing here runs a model, and nothing here can be made to. The worker
(sqs_worker.py) reads the queue from the other side.

WHAT IS SHARED, AND WHY IT MATTERS
----------------------------------
The rules about what a job may ask for are not restated here. validate_spec(),
build_fold_spec() and design_options() are imported from design.py -- the same
functions the local server runs -- so there is exactly one answer to "is this a
legal job", and adding a protocol or a setting cannot leave one front end
accepting what the other rejects.

WHAT IS DELIBERATELY DIFFERENT
------------------------------
Three things are tighter out here than on a laptop, because out here the caller
is not the owner of the machine:

    run.extra     refused. It is a free-form Hydra override, which is the right
                  escape hatch for someone running the model on their own GPU
                  and the wrong one to offer the internet.
    model         forced. The client does not get to name the runner.
    quotas        a global daily cap, a per-user daily cap and a per-user
                  concurrency cap, all of which a fold costs the same as a
                  design -- because it costs the same GPU.

There is no POST /gpu/start and no POST /gpu/stop. Locally those are
conveniences; shared, a stop button is a way for one signed-in user to kill
somebody else's run. The queue starts the box and the idle timer stops it, and
GET /gpu says so by answering `controls: false`, which is what makes the panel
hide the buttons.

CONFIGURATION
-------------
All of it from the environment, set by the CDK stack. Nothing is hardcoded and
nothing here knows an account number.

    PROTEINCAD_TABLE               DynamoDB table
    PROTEINCAD_BUCKET              S3 bucket for specs and results
    PROTEINCAD_QUEUE               SQS queue URL
    PROTEINCAD_INSTANCE            the GPU instance id
    PROTEINCAD_REGION              region for the clients
    PROTEINCAD_MAX_DESIGNS         cap on designs per job          (8)
    PROTEINCAD_MAX_SPEC_BYTES      cap on the structure in a spec  (5 MB)
    PROTEINCAD_DAILY_JOBS          per user, per UTC day           (20)
    PROTEINCAD_CONCURRENT_JOBS     per user, at once               (2)
    PROTEINCAD_GLOBAL_DAILY_JOBS   everyone, per UTC day           (100)
    PROTEINCAD_LINK_SECONDS        life of a presigned result link (300)

The GPU machine, which exists only while it is working:

    PROTEINCAD_LAUNCH_TEMPLATE     the launch template id
    PROTEINCAD_SUBNETS             comma-separated, one per zone, tried in
                                   order when a zone has no capacity
    PROTEINCAD_INSTANCE_TYPE       for the message when none of them do
    PROTEINCAD_MACHINE_STARTS      machine starts per user, per UTC day  (5)
    PROTEINCAD_GLOBAL_DAILY_LAUNCHES  launches by everyone, per day     (20)
    PROTEINCAD_IDLE_MINUTES        idle before it shuts itself down     (30)
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
import uuid
from datetime import datetime, timedelta, timezone

from . import __version__
from .api import ApiError, Request, Router
from .design import build_fold_spec, count_atoms, design_options, validate_spec

# One table per Lambda, so the routes a function serves and the permissions its
# role carries are the same list written twice and checkable against each other.
# A misrouted request fails at the router, not three lines into a call it has no
# permission to make.
API = Router("cloud-api")        # reads, and cancel
SUBMIT = Router("cloud-submit")  # the routes that cost money

ROUTERS = {"api": API, "submit": SUBMIT}

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
OPEN = (QUEUED, RUNNING)

# The machine, which is not a job and has a life of its own.
#
#     gone       there is no instance. The normal state, and the cheap one.
#     launching  RunInstances has been called
#     booting    it exists; the worker has not said anything yet
#     ready      the worker is draining the queue
#     failed     the launch did not work, and why is in the record
GONE = "gone"
LAUNCHING = "launching"
BOOTING = "booting"
READY = "ready"
MACHINE = "MACHINE"
ALIVE = (LAUNCHING, BOOTING, READY)

# A model is absent, asked for, arriving, or here.
ABSENT = "absent"
WANTED = "wanted"
DOWNLOADING = "downloading"

# How long a machine record may go unheard from before anybody may assume the
# instance is gone. The worker beats every thirty seconds, so two minutes is
# four missed beats -- long enough not to trip on a slow one, short enough that
# a browser window left open does not sit there believing in a machine that
# terminated twenty minutes ago.
MACHINE_STALE = 120

# A launch that never reports in. It used to have to cover a whole boot in one
# go, because nothing on the machine touched the record between RunInstances
# and the worker's first heartbeat -- so ten minutes, and a boot that died was
# ten minutes of the panel insisting it was still coming.
#
# The boot now beats every twenty seconds for its whole length, so this is
# twelve missed beats rather than a guess at how long a boot takes, and a
# machine that goes away mid-boot is noticed in about four minutes.
LAUNCH_STALE = 240

# What AWS says when a zone has no room. Neither is a reason to give up, and
# neither is a reason to ask for a different instance type.
NO_CAPACITY = ("InsufficientInstanceCapacity", "Unsupported",
               "InsufficientHostCapacity", "InsufficientReservedInstanceCapacity")

# A limit on this account rather than a shortage at AWS, and the difference is
# the whole point of telling them apart: capacity clears on its own in a few
# minutes, a quota never clears until somebody asks for more. Reported as the
# same "try again shortly" they would otherwise be, it is an instruction to
# retry forever.
QUOTA_EXCEEDED = ("VcpuLimitExceeded", "InstanceLimitExceeded",
                  "MaxSpotInstanceCountExceeded")


# ------------------------------------------------------------------ settings


def setting(name: str, default: str = "") -> str:
    value = os.environ.get("PROTEINCAD_" + name)
    return default if value is None or value == "" else value


def number(name: str, default: int) -> int:
    try:
        return int(float(setting(name, str(default))))
    except (TypeError, ValueError):
        return default


def now() -> float:
    """Wall clock, in one place so tests can move it."""
    return time.time()


def today() -> str:
    """The quota day. UTC, so it does not depend on where the caller is."""
    return datetime.fromtimestamp(now(), timezone.utc).strftime("%Y-%m-%d")


def resets_in() -> str:
    """How long until the daily counters roll over, in words."""
    moment = datetime.fromtimestamp(now(), timezone.utc)
    midnight = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = max(1, int((midnight - moment).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} minutes"
    return f"{minutes // 60} hours"


# ------------------------------------------------------------------- clients
#
# Made once per process and cached, because a Lambda container serves many
# requests and building a boto3 client costs more than the request does.
# `make_client` is the only place boto3 is named, which is what lets the checks
# in tools/check_cloud.py run this module with no AWS and no boto3 installed.

_CLIENTS: dict = {}


def make_client(service: str):  # pragma: no cover - replaced in tests
    import boto3

    region = setting("REGION")
    return boto3.client(service, region_name=region or None)


def client(service: str):
    if service not in _CLIENTS:
        _CLIENTS[service] = make_client(service)
    return _CLIENTS[service]


def set_client(service: str, stand_in) -> None:
    """Used by the checks, and by nothing else."""
    _CLIENTS[service] = stand_in


def reset_clients() -> None:
    _CLIENTS.clear()


# ------------------------------------------------------------------ dynamodb
#
# The low-level client rather than the resource API: the wire format is visible,
# there are no Decimals to unpick, and a stand-in for the checks is a dictionary
# with four methods rather than a reimplementation of boto3's resource layer.


def _av(value):
    """A Python value as a DynamoDB attribute value."""
    if isinstance(value, bool):
        return {"BOOL": value}
    if value is None:
        return {"NULL": True}
    if isinstance(value, (int, float)):
        return {"N": repr(value) if isinstance(value, float) else str(value)}
    if isinstance(value, str):
        # DynamoDB refuses an empty string in a key and used to refuse one
        # anywhere; storing them as NULL keeps `error: ""` from being a special
        # case at every call site.
        return {"NULL": True} if value == "" else {"S": value}
    if isinstance(value, (list, tuple)):
        return {"L": [_av(item) for item in value]}
    if isinstance(value, dict):
        return {"M": {str(k): _av(v) for k, v in value.items()}}
    return {"S": str(value)}


def _plain(attribute):
    """The inverse of _av, for one attribute."""
    if "S" in attribute:
        return attribute["S"]
    if "N" in attribute:
        text = attribute["N"]
        return float(text) if ("." in text or "e" in text.lower()) else int(text)
    if "BOOL" in attribute:
        return attribute["BOOL"]
    if "NULL" in attribute:
        return ""
    if "L" in attribute:
        return [_plain(item) for item in attribute["L"]]
    if "M" in attribute:
        return {key: _plain(value) for key, value in attribute["M"].items()}
    return None


def item_to_dict(item: dict) -> dict:
    return {key: _plain(value) for key, value in (item or {}).items()}


def dict_to_item(payload: dict) -> dict:
    return {key: _av(value) for key, value in payload.items()}


def set_fields(**fields) -> dict:
    """The UpdateItem arguments that set each field, with every name escaped.

    DynamoDB reserves about five hundred and seventy words, and `status`,
    `error`, `source`, `target`, `total` and `name` are all on the list. Every
    name goes through a placeholder rather than the ones that happen to be
    reserved today: one rule is easier to keep than a lookup, and the failure it
    prevents -- ValidationException, at runtime, on the one field nobody
    thought about -- reads as a bug in the job rather than in the query.
    """
    names, values, parts = {}, {}, []
    for index, key in enumerate(sorted(fields)):
        names["#n%d" % index] = key
        values[":v%d" % index] = _av(fields[key])
        parts.append("#n%d = :v%d" % (index, index))
    return {
        "UpdateExpression": "SET " + ", ".join(parts),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }


def job_key(job_id: str) -> dict:
    return {"pk": {"S": "JOB#" + job_id}, "sk": {"S": "JOB"}}


def _is_conditional(error: Exception) -> bool:
    """Did DynamoDB refuse this because the condition did not hold?

    Matched on the name rather than the class because botocore builds these
    exception types at runtime, and importing botocore here would undo the point
    of make_client being the only place boto3 is named.
    """
    if type(error).__name__ == "ConditionalCheckFailedException":
        return True
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
        return code == "ConditionalCheckFailedException"
    return False


# -------------------------------------------------------------------- quotas


def _take(pk: str, sk: str, limit: int) -> bool:
    """Claim one slot from a counter. False means it is full.

    One conditional update, so two requests arriving together cannot both see
    the last slot. The counter is created by the same call that increments it.
    """
    if limit <= 0:
        return False
    try:
        client("dynamodb").update_item(
            TableName=setting("TABLE"),
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            UpdateExpression="ADD jobs :one SET expires = if_not_exists(expires, :ttl)",
            ConditionExpression="attribute_not_exists(jobs) OR jobs < :limit",
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":limit": {"N": str(limit)},
                ":ttl": {"N": str(int(now()) + 3 * 86400)},
            },
        )
        return True
    except Exception as error:
        if _is_conditional(error):
            return False
        raise


def _give_back(pk: str, sk: str) -> None:
    """Undo a slot taken for a job that was then refused for another reason.

    Without this the global counter would charge everyone for a request that
    never reached the queue, and a user who typed one bad spec twenty times
    would be out of jobs for the day.
    """
    try:
        client("dynamodb").update_item(
            TableName=setting("TABLE"),
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            UpdateExpression="ADD jobs :minus",
            ExpressionAttributeValues={":minus": {"N": "-1"}},
        )
    except Exception:
        # A counter one too high is a worse outcome than a traceback in a log,
        # but not by enough to fail a request that was already being refused.
        pass


def open_jobs(user_id: str) -> int:
    return sum(1 for job in user_jobs(user_id, limit=40) if job.get("status") in OPEN)


def check_quota(user_id: str) -> None:
    """Three caps, in the order that costs least to undo.

    The global one goes first because it is the one protecting the bill; if a
    per-user cap then refuses, the global slot is handed straight back.
    """
    day = today()
    global_key = ("GLOBAL", "QUOTA#" + day)
    user_key = ("USER#" + user_id, "QUOTA#" + day)

    if not _take(global_key[0], global_key[1], number("GLOBAL_DAILY_JOBS", 100)):
        raise ApiError(
            f"proteinCAD has run its {number('GLOBAL_DAILY_JOBS', 100)} jobs for today. "
            f"The limit is for everyone together, and it resets in {resets_in()}.", 429)

    if not _take(user_key[0], user_key[1], number("DAILY_JOBS", 20)):
        _give_back(*global_key)
        raise ApiError(
            f"you have used your {number('DAILY_JOBS', 20)} jobs for today. "
            f"They come back in {resets_in()}.", 429)

    # Counted rather than held in a counter: a worker that dies mid-job would
    # never hand a held slot back, and the user would be locked out until
    # somebody noticed. Counting self-heals. Two requests in the same instant
    # can both pass this, which the daily caps then bound.
    running = open_jobs(user_id)
    allowed = number("CONCURRENT_JOBS", 2)
    if running >= allowed:
        _give_back(*global_key)
        _give_back(*user_key)
        raise ApiError(
            f"you already have {running} jobs running or queued, which is the limit. "
            "Wait for one to finish, or cancel it.", 429)


def take_start_quota(user_id: str) -> bool:
    """Charge one machine start. Global first, so a refusal costs least to undo.

    Starting a machine is not free even when it runs nothing: the idle timeout
    bounds it, but thirty idle minutes is still sixteen cents, and somebody who
    can press a button fifty times can spend eight dollars without designing
    anything. These are the cap on that.
    """
    day = today()
    if not _take(MACHINE, "LAUNCHES#" + day, number("GLOBAL_DAILY_LAUNCHES", 20)):
        raise ApiError(
            f"proteinCAD has started its {number('GLOBAL_DAILY_LAUNCHES', 20)} GPU "
            f"machines for today. The limit is for everyone together, and it resets in "
            f"{resets_in()}.", 429)
    if not _take("USER#" + user_id, "STARTS#" + day, number("MACHINE_STARTS", 5)):
        _give_back(MACHINE, "LAUNCHES#" + day)
        return False
    return True


def refund_start(user_id: str) -> None:
    """A launch that did not produce a machine did not cost one."""
    day = today()
    _give_back(MACHINE, "LAUNCHES#" + day)
    _give_back("USER#" + user_id, "STARTS#" + day)


def quota_used(user_id: str) -> dict:
    """What the panel shows beside the Run button."""
    table = setting("TABLE")
    day = today()

    def count(pk, sk):
        try:
            got = client("dynamodb").get_item(
                TableName=table, Key={"pk": {"S": pk}, "sk": {"S": sk}})
        except Exception:
            return 0
        return int(item_to_dict(got.get("Item") or {}).get("jobs") or 0)

    return {
        "used": count("USER#" + user_id, "QUOTA#" + day),
        "daily": number("DAILY_JOBS", 20),
        "concurrent": number("CONCURRENT_JOBS", 2),
        "starts_used": count("USER#" + user_id, "STARTS#" + day),
        "starts": number("MACHINE_STARTS", 5),
        "resets_in": resets_in(),
    }


# ------------------------------------------------------------- what a job is


def job_item(job_id: str, user_id: str, spec: dict, spec_key: str) -> dict:
    target = spec.get("target") or {}
    return {
        "pk": "JOB#" + job_id,
        "sk": "JOB",
        "job_id": job_id,
        "user_id": user_id,
        "created": now(),
        # Only on the item so the by_user index has something to sort on. The
        # index projects these and nothing else, which keeps a listing cheap.
        "status": QUEUED,
        "kind": spec.get("kind") or "binder",
        "mode": spec.get("mode") or ("fold" if spec.get("kind") == "fold" else "binder"),
        "model": spec.get("model") or "ec2",
        "total": int((spec.get("run") or {}).get("numDesigns", 1) or 1),
        "progress": 0,
        "stage": "",
        "command": "",
        "error": "",
        "cancel": False,
        "spec_key": spec_key,
        "target": target.get("name") or "",
        "hotspots": target.get("hotspots") or [],
        "source": spec.get("source") or "",
        "designs": [],
        "expires": int(now()) + 30 * 86400,
    }


def job_to_dict(item: dict) -> dict:
    """Exactly the shape web/src/app.js already reads.

    Same keys as jobs.Job.to_dict(), so the viewer cannot tell which back end
    answered and does not have to.
    """
    created = float(item.get("created") or 0)
    started = float(item.get("started") or 0) or None
    finished = float(item.get("finished") or 0) or None
    return {
        "id": item.get("job_id", ""),
        "status": item.get("status", ""),
        "model": item.get("model", ""),
        "kind": item.get("kind", ""),
        "mode": item.get("mode", ""),
        "source": item.get("source") or None,
        "progress": int(item.get("progress") or 0),
        "stage": item.get("stage", ""),
        "command": item.get("command", ""),
        "total": int(item.get("total") or 1),
        "error": item.get("error", ""),
        "created": created,
        "elapsed": round((finished or now()) - (started or created), 1),
        "target": item.get("target", ""),
        "hotspots": item.get("hotspots") or [],
        "designs": [
            # `key` is where it is in the bucket, which is this side's business.
            {"name": d.get("name", ""), "metrics": d.get("metrics") or {},
             "atoms": int(d.get("atoms") or 0)}
            for d in (item.get("designs") or [])
        ],
    }


def get_job(job_id: str) -> dict:
    got = client("dynamodb").get_item(TableName=setting("TABLE"), Key=job_key(job_id))
    return item_to_dict(got.get("Item") or {})


def user_jobs(user_id: str, limit: int = 40) -> list:
    answer = client("dynamodb").query(
        TableName=setting("TABLE"),
        IndexName="by_user",
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": {"S": user_id}},
        ScanIndexForward=False,
        Limit=limit,
    )
    return [item_to_dict(item) for item in answer.get("Items", [])]


def owned(job_id: str, user_id: str) -> dict:
    """The job, if it is the caller's.

    Somebody else's job id answers "no job" rather than "not yours": the second
    confirms the id exists, and job ids are the only thing here worth guessing.
    """
    job = get_job(job_id)
    if not job or job.get("user_id") != user_id:
        raise ApiError(f"no job {job_id}", 404)
    return job


# ------------------------------------------------------- checking what came in


PDB_MARKERS = ("ATOM  ", "HETATM", "HEADER", "MODEL ", "CRYST1", "TITLE ")


def looks_like_structure(text: str) -> bool:
    """Is this a .pdb or .cif file, rather than something else entirely?

    The same question the file picker asks with `accept=".pdb,.cif"`, asked
    again here because a request does not have to come from the file picker.
    Only the head is examined: these can be megabytes, and a format is decided
    in the first few lines or not at all.
    """
    head = text[:4096]
    if any(marker in head for marker in PDB_MARKERS):
        return True
    # mmCIF: a data block, and the loop that holds coordinates.
    return "data_" in head and ("_atom_site" in text[:200_000] or "_entry" in head)


def check_structure(text: str, what: str) -> None:
    limit = number("MAX_SPEC_BYTES", 5 * 1024 * 1024)
    if len(text) > limit:
        raise ApiError(
            f"{what} is {len(text) // 1024} kB, over the {limit // (1024 * 1024)} MB limit. "
            "Reduce the crop radius, or pick fewer design sites.", 413)
    if not looks_like_structure(text):
        raise ApiError(
            f"{what} does not look like a PDB or mmCIF structure. proteinCAD reads "
            ".pdb, .ent, .cif and .mmcif files.", 400)
    if count_atoms(text) == 0:
        raise ApiError(f"{what} contains no ATOM or HETATM records.", 400)


def clean_spec(spec) -> dict:
    """Everything the hosted API checks before design.py's own validator runs.

    Ordered so the message names the first real problem: a wrong sort of file
    is a better answer than a complaint about its atom count.
    """
    if not isinstance(spec, dict):
        raise ApiError("the job must be an object", 400)

    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    if target.get("pdb"):
        check_structure(str(target["pdb"]), "the target structure")

    run = spec.get("run")
    if run is not None and not isinstance(run, dict):
        raise ApiError("spec.run must be an object", 400)
    if isinstance(run, dict) and run.get("extra"):
        # Free-form Hydra overrides. They are the right escape hatch for
        # somebody running the model on their own GPU and the wrong one to hand
        # the internet: every other setting is checked against the catalogue,
        # and this one by definition is not.
        raise ApiError(
            "extra RFdiffusion overrides are not accepted by the hosted API. Every setting "
            "in the panel is available; the free-form box only works when you are running "
            "proteinCAD against your own GPU.", 400)

    try:
        spec = validate_spec(spec)
    except ValueError as error:
        raise ApiError(str(error), 400) from error

    # Decided here, never by the caller. Out here there is one runner.
    spec["model"] = "ec2"
    cap = number("MAX_DESIGNS", 8)
    spec.setdefault("run", {})["numDesigns"] = max(
        1, min(int(spec["run"].get("numDesigns", 1) or 1), cap))
    return spec


# --------------------------------------------------------------- the s3 parts


def put_object(key: str, body: bytes, content_type: str = "application/json") -> None:
    client("s3").put_object(
        Bucket=setting("BUCKET"), Key=key, Body=body, ContentType=content_type)


def get_object(key: str) -> bytes:
    answer = client("s3").get_object(Bucket=setting("BUCKET"), Key=key)
    body = answer["Body"]
    return body.read() if hasattr(body, "read") else body


def presign(key: str) -> str:
    return client("s3").generate_presigned_url(
        "get_object",
        Params={"Bucket": setting("BUCKET"), "Key": key},
        ExpiresIn=number("LINK_SECONDS", 300),
    )


# -------------------------------------------------------------------- queuing


def wake_machine(user_id: str) -> None:
    """Make sure something will pick this job up. Never fatal.

    Sending a design works without having pressed any button: if there is no
    machine, submitting starts one, and the worker fetches whatever the job
    needs. The buttons exist to let somebody do that deliberately and watch it
    happen, not because the app cannot do it for them.

    A launch that fails here leaves the job queued rather than losing it. The
    five-minute rule finds a queue with something in it and no machine, and
    tries again.
    """
    if not setting("LAUNCH_TEMPLATE"):
        return
    try:
        if live(read_machine()["state"]):
            return
        start_machine(user_id)
    except ApiError as error:
        # Out of capacity, or out of starts. Both are worth the log and neither
        # is worth failing a job that is safely on the queue.
        print(f"[cloud] job queued but no machine started: {error}", flush=True)
    except Exception as error:
        print(f"[cloud] could not start a machine: {type(error).__name__}: {error}",
              flush=True)


def queue(spec: dict, user_id: str) -> dict:
    """Bill it, write it down, then queue it -- in that order.

    The order is the whole of the crash safety here. The worker only ever sees
    a job through the queue message, so the spec and the record are both in
    place before the message exists; there is no moment at which a worker can
    read a job whose spec is missing.
    """
    check_quota(user_id)

    job_id = uuid.uuid4().hex
    spec_key = "specs/%s.json" % job_id
    try:
        put_object(spec_key, json.dumps(spec).encode("utf-8"))
        item = job_item(job_id, user_id, spec, spec_key)
        client("dynamodb").put_item(TableName=setting("TABLE"), Item=dict_to_item(item))
    except Exception as error:
        _refund(user_id)
        raise ApiError(f"could not accept the job: {type(error).__name__}: {error}", 502)

    try:
        client("sqs").send_message(
            QueueUrl=setting("QUEUE"),
            MessageBody=json.dumps({"job_id": job_id, "spec_key": spec_key}),
        )
    except Exception as error:
        # The record exists and nothing will ever pick it up, so say so in the
        # record rather than leaving a job queued forever.
        fail_job(job_id, f"the job could not be queued: {type(error).__name__}: {error}")
        _refund(user_id)
        raise ApiError("the job could not be queued; nothing was started", 502)

    wake_machine(user_id)
    return job_to_dict(item)


def _refund(user_id: str) -> None:
    day = today()
    _give_back("GLOBAL", "QUOTA#" + day)
    _give_back("USER#" + user_id, "QUOTA#" + day)


def fail_job(job_id: str, message: str) -> None:
    client("dynamodb").update_item(
        TableName=setting("TABLE"), Key=job_key(job_id),
        **set_fields(status=FAILED, error=message, finished=now()))


# --------------------------------------------------------------------- routes


@API.route("GET", "/health")
def health(request: Request) -> dict:
    """The one route that does not need a token.

    Without it the viewer cannot tell "there is no server" from "you are not
    signed in", and those have different fixes. It says the version and the
    shape of the deployment, and nothing about anybody's jobs.
    """
    return {
        "status": "ok",
        "version": __version__,
        "stale": False,
        "runners": ["ec2"],
        "runner": "ec2",
        "compute_configured": True,
        "allow_remote_config": False,
        "max_designs": number("MAX_DESIGNS", 8),
        "cached_structures": [],
        "cache_count": 0,
        # Tells the panel to put a sign-in control up before anything is tried.
        "auth": "cognito",
        "hosted": True,
    }


@API.route("GET", "/design/options")
def options(request: Request) -> dict:
    """Every protocol and setting, from the same table the worker builds its
    command line from."""
    catalogue = design_options()
    # The free-form box is not offered here, because clean_spec refuses it.
    catalogue["extra_allowed"] = False
    return catalogue


@API.route("GET", "/jobs")
def jobs(request: Request) -> dict:
    user_id = caller(request)
    return {
        "jobs": [job_to_dict(item) for item in user_jobs(user_id)],
        "quota": quota_used(user_id),
    }


@API.route("GET", "/jobs/{job_id}")
def job_status(request: Request) -> dict:
    return job_to_dict(owned(request.params["job_id"], caller(request)))


@API.route("POST", "/jobs/{job_id}/cancel")
def job_cancel(request: Request) -> dict:
    """Ask for it to stop.

    A job still in the queue is cancelled here and now; the worker's claim is
    conditional on the status still being `queued`, so it will find this and
    drop the message. A job already running gets a flag, which the worker reads
    every few seconds and turns into a `docker stop`.
    """
    job_id = request.params["job_id"]
    job = owned(job_id, caller(request))
    if job.get("status") not in OPEN:
        return job_to_dict(job)

    ending = job.get("status") == QUEUED
    fields = {"cancel": True}
    if ending:
        fields.update(status=CANCELLED, finished=now())
    client("dynamodb").update_item(
        TableName=setting("TABLE"), Key=job_key(job_id), **set_fields(**fields))
    job["cancel"] = True
    if ending:
        job["status"] = CANCELLED
    return job_to_dict(job)


@API.route("GET", "/jobs/{job_id}/designs/{index}")
def job_design(request: Request) -> dict:
    """A link to one design, good for a few minutes.

    Not the file itself: it would cross a Lambda response limit on a large
    design and cost a second copy of every byte. The bucket blocks all public
    access, so a signed link is the only way in and it expires.
    """
    job = owned(request.params["job_id"], caller(request))
    index = whole(request.params["index"], "design index")
    designs = job.get("designs") or []
    if index < 0 or index >= len(designs):
        raise ApiError(f"job {job.get('job_id')} has no design {index}", 404)
    design = designs[index]
    if not design.get("key"):
        raise ApiError(f"design {index} was never stored", 404)
    return {"url": presign(design["key"]), "name": design.get("name", f"design_{index}")}


@SUBMIT.route("POST", "/design")
def design(request: Request) -> dict:
    return queue(clean_spec(request.json), caller(request))


@SUBMIT.route("POST", "/jobs/{job_id}/designs/{index}/fold")
def job_fold(request: Request) -> dict:
    """Take one finished backbone on to sequence design.

    Built on this side from the target as it was sent to the model and the
    backbone as the model returned it -- both already in the bucket -- so the
    sequence is designed against exactly what was there. The browser sends only
    which design it means.
    """
    user_id = caller(request)
    job = owned(request.params["job_id"], user_id)
    index = whole(request.params["index"], "design index")
    try:
        spec = build_fold_spec(StoredJob(job), index, request.json)
    except ValueError as error:
        raise ApiError(str(error), 400) from error
    # A fold is a GPU run like any other, so it is billed like any other.
    return queue(clean_spec(spec), user_id)


# ------------------------------------------------------------- the machine
#
# One instance, shared by everyone, and it does not exist most of the time.
# Which makes the record below the only durable thing about it: the instance is
# created for a job and destroyed after one, so "is there a machine" is a
# question about DynamoDB, not about EC2.
#
#     pk=MACHINE  sk=STATE              state, instance_id, zone, heartbeat
#     pk=MACHINE  sk=MODEL#rfdiffusion  state, bytes, total
#     pk=MACHINE  sk=MODEL#esmfold      ...
#
# Separate items rather than one record with a map inside it. The worker writes
# download progress every second or two while a Lambda may be taking the launch
# lock; separate items mean those never touch the same row, there are no nested
# document paths to get right, and one Query still returns the whole picture.


_BUILD = None


def code_version() -> str:
    """A short fingerprint of the Python this deployment is made of.

    Both halves come out of the same `cdk deploy`: the Lambda gets
    proteincad/*.py in its bundle, and the GPU machine syncs the same files
    from the bucket at boot. So hashing them gives two processes the same
    answer when they are the same deployment, and different answers when they
    are not -- which is the question nobody could ask before.

    It is worth asking because a machine is created once and lives for hours.
    Deploy a fix while one is running and it keeps the code it booted with,
    silently: the button is there, the handler is deployed, and the machine
    ignores it. Every symptom of that looks like a bug in the fix.

    Not a guard -- nothing refuses to run on a mismatch, because an older
    worker is still a worker and killing jobs over a version number would be
    worse. Just a thing the panel can say out loud.
    """
    global _BUILD
    if _BUILD is None:
        import hashlib

        digest = hashlib.sha256()
        for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        _BUILD = digest.hexdigest()[:12]
    return _BUILD


def machine_key(what: str = "STATE") -> dict:
    return {"pk": {"S": MACHINE}, "sk": {"S": what}}


def model_key(name: str) -> dict:
    return machine_key("MODEL#" + name)


def read_machine() -> dict:
    """Everything under pk=MACHINE, in one call.

    Strongly consistent, and it has to be. Every button in the panel writes a
    record and then calls this to build the answer it sends back -- Start
    writes `launching` and reads the machine, Download writes `wanted` and
    reads the models. A default eventually-consistent read is allowed to
    answer with the state from *before* that write, and what the panel then
    paints is exactly the row the user was looking at when they pressed it.
    Which is indistinguishable, from the other side of the screen, from a
    button that does nothing.

    It costs twice the read units of an eventually-consistent query on a
    table that is queried a few times a minute. That is the cheapest bug fix
    in this file.
    """
    answer = client("dynamodb").query(
        TableName=setting("TABLE"),
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": {"S": MACHINE}},
        ConsistentRead=True,
    )
    state, models = {}, {}
    for raw in answer.get("Items", []):
        item = item_to_dict(raw)
        sort = item.get("sk", "")
        if sort == "STATE":
            state = item
        elif sort.startswith("MODEL#"):
            models[sort[len("MODEL#"):]] = item
    return {"state": state, "models": models}


def live(state: dict) -> bool:
    """Is there a machine, as far as anybody can tell?

    A record saying `ready` whose worker stopped beating four minutes ago is
    not a machine, it is the last thing a machine said. Treating it as gone is
    what lets the panel tell somebody their instance went away instead of
    leaving them waiting on a queue nothing is draining.
    """
    if state.get("state") not in ALIVE:
        return False
    quiet = now() - float(state.get("heartbeat") or 0)
    if state.get("state") != READY:
        # Launching and booting have no heartbeat to expect: the launcher
        # stamps one when RunInstances returns and nothing touches it again
        # until the worker starts. A boot is four or five minutes -- most of it
        # pulling seven gigabytes of image -- so judging these two by the short
        # allowance declares a perfectly healthy machine dead at two minutes,
        # puts the Start button back, and leaves the models unreachable while
        # the instance quietly finishes booting and then bills all night.
        #
        # Only `ready` has something beating behind it every thirty seconds,
        # so only `ready` is held to the short one.
        return quiet < LAUNCH_STALE
    return quiet < MACHINE_STALE


def retiring(state: dict) -> bool:
    """Has this machine been asked to stand down?

    Stamped with the instance it was asked of, and compared here, for the same
    reason the model records are: these rows outlive the machine. A request
    left over from the machine before would otherwise retire its replacement
    the moment it finished booting, over and over.
    """
    asked = state.get("retire_instance", "")
    return bool(asked) and asked == state.get("instance_id", "")


def resolve_models(found: dict) -> list:
    """What each model is, on the machine that is running now.

    Weights live on the instance store, which is created with the machine and
    destroyed with it -- but these records outlive both. So a model record means
    something only while the machine that wrote it is the machine running now.
    Reading a previous machine's as current is how the panel came to report
    three models downloaded onto a disk created empty a minute earlier, and Run
    enabled against weights that were nowhere.

    One function because there are two callers and they must not disagree. They
    did: this rule drew the panel, while want_model() looked at the raw record
    underneath it. A `ready` row left behind by a previous machine therefore
    read `absent` on screen -- correctly, the weights are gone with the disk --
    while want_model saw `ready` and returned without writing anything. The
    panel offered a Download button, pressing it did nothing whatsoever, and the
    worker was never told to fetch anything.
    """
    from .colab_worker import MODELS

    state = found["state"]
    here = live(state)
    on_this_machine = state.get("instance_id", "") if here else ""

    models = []
    for entry in MODELS:
        record = found["models"].get(entry["id"], {})
        stamped = record.get("instance_id", "")
        # Compared only when both sides know which machine they mean. If either
        # does not -- a worker that could not read the metadata service, say --
        # the honest answer is that this cannot be told apart, and the useful
        # behaviour is the old one: trust the record.
        #
        # Reporting absent instead would be safer on paper and unusable in
        # practice: pressing Download makes the worker see the weights already
        # on disk, write `ready` with the same blank id, and the row snap back
        # to absent for ever. Trusting it costs at worst one re-download the
        # worker performs anyway, since it decides from the disk, not from here.
        same = here and ((not on_this_machine or not stamped)
                         or stamped == on_this_machine)
        models.append({
            "id": entry["id"],
            "label": entry["label"],
            "help": entry["help"],
            "state": record.get("state", ABSENT) if same else ABSENT,
            "bytes": int(record.get("bytes") or 0) if same else 0,
            "total": int(record.get("total") or entry["bytes"]),
            "error": record.get("error", "") if same else "",
        })
    return models


def machine_view(user_id: str = "") -> dict:
    """What the panel draws.

    Deliberately one payload: the machine, every model, and what each protocol
    needs. The Run button's rule -- machine ready and these models present --
    is then the same rule on both sides of the wire rather than a list in
    JavaScript that drifts.
    """
    from .colab_worker import (
        ENGINES, FOLD_MODELS, MODELS, MODELS_BY_ID, RFDIFFUSION_MODES,
        modes_for_engine,
    )

    found = read_machine()
    state = found["state"]
    here = live(state)
    status = state.get("state") if here else GONE

    # A launch that failed leaves no machine, so the state is honestly `gone`.
    # The reason is worth keeping for a few minutes though: somebody who
    # reloads the page after a capacity error should still be told what
    # happened rather than facing a Start button that appears to have done
    # nothing.
    recent_failure = ""
    if not here and state.get("state") == FAILED:
        if now() - float(state.get("heartbeat") or 0) < 300:
            recent_failure = state.get("error", "")

    models = resolve_models(found)

    return {
        "configured": bool(setting("LAUNCH_TEMPLATE")),
        "state": status,
        "instance": state.get("instance_id", "") if here else "",
        "zone": state.get("zone", "") if here else "",
        "since": float(state.get("launched_at") or 0) if here else 0,
        # What the boot script is doing, and how far through it is. A machine
        # takes five minutes to arrive; without these the panel shows one word
        # for all five and somebody presses Start again in the middle.
        "stage": state.get("stage", "") if here else "",
        "step": int(state.get("step") or 0) if here else 0,
        "steps": int(state.get("steps") or 0) if here else 0,
        "error": recent_failure,
        "models": models,
        # Still no stop button, and still no terminate: on a machine other
        # people are also using, either is a way to end somebody else's run,
        # and nothing in this account has the permission to do it anyway.
        #
        # `retire` is the third thing, and it is not those two. It asks the
        # machine to finish what it is doing and then shut itself down -- the
        # same route it takes on the idle timer, at a moment of its own
        # choosing. No job is interrupted, and no new IAM permission exists to
        # misuse: the instance still ends itself.
        "controls": {"start": True, "stop": False, "terminate": False, "retire": True},
        "retiring": retiring(state) if here else False,
        # How long a retire has gone unanswered. A machine that booted before
        # this feature existed cannot act on one, and would otherwise sit there
        # saying "Retiring" for ever -- which is the same lie as a button that
        # does nothing, arriving a minute later.
        "retire_waiting": (now() - float(state.get("retire_at") or 0)
                           if here and retiring(state) else 0),
        # Which deployment this machine is running, against which one answered
        # this request. Different means it booted before the last deploy and is
        # still running what it booted with.
        "build": state.get("build", "") if here else "",
        # Only a running worker has a build, because only a worker writes one.
        # Judged during a launch or a boot it is the *previous* machine's, and
        # the panel told somebody their brand-new machine was out of date two
        # minutes into its first boot.
        "stale_build": bool(here and status == READY and state.get("build")
                            and state.get("build") != code_version()),
        "idle_minutes": number("IDLE_MINUTES", 30),
        # Keyed by mode, for the engine that was the only one when this route
        # was written. The two engines have protocols with the same names and
        # different needs, so a single flat map cannot answer for both -- but a
        # cached copy of the viewer still reads this one, and for an RFdiffusion
        # job it is still right.
        "needs": {mode["id"]: list(mode.get("models") or ("rfdiffusion",))
                  for mode in RFDIFFUSION_MODES},
        "engine_needs": {
            engine["id"]: {mode["id"]: list(mode.get("models") or (engine["id"],))
                           for mode in modes_for_engine(engine["id"])}
            for engine in ENGINES
        },
        "fold_needs": list(FOLD_MODELS),
        "known": sorted(MODELS_BY_ID),
    }


def take_launch_lock(user_id: str, seen: float) -> bool:
    """Claim the right to launch. One conditional write; exactly one winner.

    Two people pressing Start at the same second is the ordinary case, not the
    exotic one -- and two instances would be two GPUs billing for one job. So
    the decision is made by DynamoDB rather than by checking first and then
    acting, which is the same race written longer.

    What this does NOT decide is whether there is a machine. `live()` owns
    that, and start_machine has already asked it; this is mutual exclusion and
    nothing else, so `seen` is the heartbeat the caller read and the condition
    is a compare-and-swap against it.

    It used to carry a second copy of the liveness rule -- take the lock if the
    heartbeat is older than LAUNCH_STALE -- and the two copies disagreed.
    live() calls a `ready` record dead after MACHINE_STALE, two minutes; this
    waited for LAUNCH_STALE, ten. An instance that went away without saying so
    -- a crash, a hardware fault, a terminate-instances by hand -- therefore
    left an eight-minute window in which the panel said `gone`, offered a Start
    button, and pressing it launched nothing and reported nothing, because the
    condition here refused and the refusal is indistinguishable from somebody
    else having won the race.

    One rule, in one place, is the fix. Anything live() calls dead can be
    taken over; anything it calls alive never gets here.
    """
    # Every per-machine field reset together. retire_at/retire_instance join the
    # list because they describe the machine that is going, not the one being
    # made: the stamp already stops a replacement inheriting the request, and
    # clearing it stops the record carrying a dead machine's business around.
    fields = set_fields(state=LAUNCHING, launched_by=user_id, launched_at=now(),
                        heartbeat=now(), instance_id="", zone="", error="",
                        retire_at=0, retire_instance="", build="")
    fields["ExpressionAttributeNames"]["#s"] = "state"
    fields["ExpressionAttributeNames"]["#h"] = "heartbeat"
    fields["ExpressionAttributeValues"][":seen"] = {"N": repr(float(seen or 0))}
    try:
        client("dynamodb").update_item(
            TableName=setting("TABLE"), Key=machine_key(),
            # Unchanged since the caller looked: no record at all, no heartbeat
            # on it yet, or the same heartbeat it read. Anything else means
            # another launch got there first, and theirs is the machine.
            ConditionExpression=("attribute_not_exists(#s) "
                                 "OR attribute_not_exists(#h) OR #h = :seen"),
            **fields)
        return True
    except Exception as error:
        if _is_conditional(error):
            return False
        raise


def release_launch_lock(why: str = "") -> None:
    client("dynamodb").update_item(
        TableName=setting("TABLE"), Key=machine_key(),
        **set_fields(state=FAILED if why else GONE, error=why, heartbeat=now()))


def _error_code(error: Exception) -> str:
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code", "")
    return type(error).__name__


def quota_message(kind: str, gone_ago: float = 0.0) -> str:
    """Why the launch was refused, said as precisely as the record allows.

    The limit is the same either way, but what to *do* about it is not: a
    machine that has just gone is a wait of a minute or two, and a quota with
    nothing behind it is a support request. Guessing between the two is what
    the first version of this message made the reader do.

    The machine record is enough to tell them apart and costs nothing: a worker
    announces GONE on its way down, so a recent goodbye means its instance is
    still terminating and still holding its vCPUs. Deliberately not asked of
    EC2 -- ec2:DescribeInstances cannot be scoped to a resource, and this API
    is built not to hold it (see the IAM notes in the stack).

    `gone_ago` is passed in rather than read here, because by the time a launch
    fails the record no longer says what it said: taking the launch lock is a
    write, and it has already replaced GONE with LAUNCHING and a fresh
    heartbeat. The only place that still knows is the caller, before it locked.
    """
    if 0 < gone_ago < 600:
        return (
            f"there was a machine here {int(gone_ago)}s ago and it has probably not "
            f"finished terminating, so its vCPUs are not free yet and this account "
            f"cannot start another {kind} until they are. AWS usually releases them a "
            f"minute or two after an instance goes.\n\n"
            f"Wait a moment and press Start again — nothing is wrong, and nothing "
            f"needs changing.\n\n"
            f"If this is a nuisance, it is because the quota is exactly one machine "
            f"wide: Service Quotas -> EC2 -> \"Running On-Demand G and VT instances\" "
            f"is counted in vCPUs and a {kind} needs 4, so a replacement can never "
            f"start until the one before it has gone. Raising it to 8 removes the wait.")

    return (
        f"this AWS account will not run another {kind} right now: its EC2 limit is "
        f"already spoken for. That is a limit on the account, not a shortage at AWS, "
        f"so trying again will not help on its own.\n\n"
        f"  - an instance that is still shutting down holds its vCPUs until it has "
        f"fully terminated. If one was just stopped, wait a minute and press Start "
        f"again.\n"
        f"  - otherwise the quota needs raising: Service Quotas -> EC2 -> "
        f"\"Running On-Demand G and VT instances\", which is counted in vCPUs. "
        f"A {kind} needs 4; 8 lets a replacement start before the old one has gone.")


def subnets() -> list:
    return [piece.strip() for piece in setting("SUBNETS", "").split(",") if piece.strip()]


def launch(user_id: str, gone_ago: float = 0.0) -> dict:
    """Start one instance, trying each zone in turn.

    A zone with no g4dn left is a normal Tuesday, and the answer is another
    zone -- never another instance type. Falling back to something bigger turns
    "the cheap machine was busy" into a bill nobody chose, and the IAM policy
    refuses it anyway, so this only has to not try.
    """
    template = setting("LAUNCH_TEMPLATE")
    if not template:
        raise ApiError("this deployment has no launch template configured", 503)

    where = subnets()
    if not where:
        raise ApiError("this deployment has no subnets configured", 503)

    last = ""
    for subnet in where:
        try:
            answer = client("ec2").run_instances(
                LaunchTemplate={"LaunchTemplateId": template, "Version": "$Latest"},
                SubnetId=subnet, MinCount=1, MaxCount=1,
                TagSpecifications=[{
                    "ResourceType": kind,
                    "Tags": [{"Key": "Name", "Value": "proteincad-gpu"},
                             {"Key": "proteincad:role", "Value": "worker"},
                             {"Key": "proteincad:started-by", "Value": user_id}],
                } for kind in ("instance", "volume")],
            )
        except Exception as error:
            code = _error_code(error)
            last = code or type(error).__name__
            if code in NO_CAPACITY:
                print(f"[cloud] no capacity in {subnet} ({code}); trying the next zone",
                      flush=True)
                continue
            if code in QUOTA_EXCEEDED:
                # Account-wide and region-wide, so the next zone has exactly the
                # same answer. Raised here rather than tried three times.
                kind = setting("INSTANCE_TYPE", "g4dn.xlarge")
                print(f"[cloud] {code}: this account's EC2 limit stopped the launch",
                      flush=True)
                raise ApiError(quota_message(kind, gone_ago), 429) from error

        instance = (answer.get("Instances") or [{}])[0]
        client("dynamodb").update_item(
            TableName=setting("TABLE"), Key=machine_key(),
            **set_fields(state=BOOTING, heartbeat=now(),
                         instance_id=instance.get("InstanceId", ""),
                         zone=(instance.get("Placement") or {}).get("AvailabilityZone", ""),
                         subnet=subnet, error=""))
        print(f"[cloud] launched {instance.get('InstanceId')} in {subnet}", flush=True)
        return read_machine()["state"]

    raise ApiError(
        f"AWS has no {setting('INSTANCE_TYPE', 'g4dn.xlarge')} to spare in any of the "
        f"{len(where)} zones this deployment can use. Nothing is wrong here -- it usually "
        f"clears within a few minutes. Try again shortly. [{last}]", 503)


def start_machine(user_id: str) -> dict:
    """Start one if there is not one. Idempotent by design.

    Pressing Start when somebody else has already started it is not an error
    and does not cost a quota slot: it is the common case on a shared machine,
    and the right answer is the state of the machine that is already there.
    """
    found = read_machine()
    if live(found["state"]):
        return machine_view(user_id)

    # How recently there was a machine, kept before the lock below overwrites
    # it. It is the difference between "wait a moment" and "raise a quota" if
    # this launch is refused for capacity on the account.
    #
    # GONE *or* FAILED: a machine that ends cleanly leaves the first, and a
    # Start that was itself refused leaves the second -- which is precisely the
    # state the record is in when somebody presses Start twice during one
    # instance's shutdown, and so precisely when this hint is wanted. The
    # latest thing the record knows is the right clock either way.
    last = found["state"]
    recent = max(float(last.get("heartbeat") or 0), float(last.get("launched_at") or 0))
    gone_ago = now() - recent if last.get("state") in (GONE, FAILED) and recent else 0.0

    if not take_launch_lock(user_id, float(found["state"].get("heartbeat") or 0)):
        # Somebody won it between the read and the write. Theirs is the
        # machine; show them what it is doing.
        return machine_view(user_id)

    if not take_start_quota(user_id):
        release_launch_lock()
        raise ApiError(
            f"you have started the GPU machine {number('MACHINE_STARTS', 5)} times today, "
            f"which is the limit. It comes back in {resets_in()}. If one is running now, "
            "you can use it without starting another.", 429)

    try:
        launch(user_id, gone_ago)
    except ApiError as error:
        release_launch_lock(str(error))
        refund_start(user_id)
        raise
    except Exception as error:
        release_launch_lock(f"{type(error).__name__}: {error}")
        refund_start(user_id)
        raise
    return machine_view(user_id)


def want_model(name: str) -> dict:
    """Ask the machine to fetch a model. It polls this record.

    Not a download this process performs -- it has neither the disk nor the
    minutes. The worker is the thing with a hundred and twenty gigabytes of
    instance store and a reason to use it.
    """
    from .colab_worker import MODELS_BY_ID

    if name not in MODELS_BY_ID:
        raise ApiError(f"there is no model called {name!r}; this build has "
                       f"{', '.join(sorted(MODELS_BY_ID))}", 404)

    found = read_machine()
    if not live(found["state"]):
        raise ApiError("there is no GPU machine running to download it to. Start one "
                       "first.", 409)
    if found["state"].get("state") != READY:
        raise ApiError("the machine is still starting up. The download can begin as soon "
                       "as it is ready.", 409)

    # From the resolved view, not the raw record. They are different whenever a
    # record outlived the machine that wrote it, and the raw one is the wrong
    # answer: the weights went with that machine's disk.
    current = next((m["state"] for m in resolve_models(found) if m["id"] == name), ABSENT)
    if current in (READY, DOWNLOADING, WANTED):
        return machine_view()

    # Stamped with the machine it is being asked of, exactly as the worker
    # stamps its own updates. Without it machine_view reads this record back as
    # belonging to some other machine and reports the model absent -- so the
    # row would snap back to "not downloaded" the instant it was pressed.
    client("dynamodb").update_item(
        TableName=setting("TABLE"), Key=model_key(name),
        **set_fields(state=WANTED, bytes=0, total=MODELS_BY_ID[name]["bytes"],
                     error="", asked_at=now(),
                     instance_id=found["state"].get("instance_id", "")))
    return machine_view()


@API.route("GET", "/machine")
def machine(request: Request) -> dict:
    """Where the machine is, what it has downloaded, and what each protocol
    needs -- which together are exactly the Run button's enabling condition."""
    if not setting("LAUNCH_TEMPLATE"):
        return {"configured": False}
    return machine_view(caller(request))


@SUBMIT.route("POST", "/machine/start")
def machine_start(request: Request) -> dict:
    return start_machine(caller(request))


def retire_machine(user_id: str) -> dict:
    """Ask the machine to finish up and go.

    Not a kill. The flag below is read by the worker between jobs, and it acts
    on it exactly where it already acts on the idle timer -- after the job in
    hand is done and with nothing waiting on the queue. So the worst this can
    do to somebody else's design is make them wait for a new machine; it cannot
    lose one.

    Which is also why it is a flag rather than an API call against EC2: there
    is no ec2:TerminateInstances anywhere in this account, deliberately, and
    this feature does not add one.
    """
    found = read_machine()
    if not live(found["state"]):
        raise ApiError("there is no GPU machine running to retire.", 409)

    instance = found["state"].get("instance_id", "")
    if not instance:
        raise ApiError("the machine has not said which instance it is yet; "
                       "try again in a moment.", 409)

    client("dynamodb").update_item(
        TableName=setting("TABLE"), Key=machine_key(),
        **set_fields(retire_at=now(), retire_by=user_id, retire_instance=instance))
    print(f"[cloud] {user_id} asked {instance} to retire", flush=True)
    return machine_view(user_id)


@SUBMIT.route("POST", "/machine/retire")
def machine_retire(request: Request) -> dict:
    return retire_machine(caller(request))


@SUBMIT.route("POST", "/machine/models/{name}")
def machine_model(request: Request) -> dict:
    caller(request)
    return want_model(request.params["name"])


class StoredJob:
    """What build_fold_spec() expects, backed by DynamoDB and S3.

    It wants a local Job: a spec, an id, a list of designs and a file on disk
    per design. Three of those are in the record; the fourth is fetched and
    written to the Lambda's own /tmp, which is why this is a class and not a
    dict.
    """

    def __init__(self, item: dict):
        self.item = item
        self.id = item.get("job_id", "")
        self.designs = item.get("designs") or []
        self.spec = json.loads(get_object(item["spec_key"]).decode("utf-8"))

    def design_path(self, index: int):
        from pathlib import Path

        if index < 0 or index >= len(self.designs):
            return None
        key = self.designs[index].get("key")
        if not key:
            return None
        path = Path("/tmp") / ("fold_%s_%03d.pdb" % (self.id, index))
        path.write_bytes(get_object(key))
        return path


# ------------------------------------------------------------ the lambda edge


def caller(request: Request) -> str:
    """Who is asking, according to the token API Gateway already checked.

    The JWT authorizer runs before this function does, so a request that got
    here has a valid, unexpired token from the one user pool. Nothing here
    parses a token; if the claim is missing, that is a misconfigured route, not
    an anonymous user, and it should fail loudly.
    """
    user_id = (request.context or {}).get("user_id") or ""
    if not user_id:
        raise ApiError("this route needs a signed-in user", 401)
    return user_id


def whole(text: str, what: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError) as error:
        raise ApiError(f"{what} must be a number", 400) from error


def claims(event: dict) -> dict:
    context = (event.get("requestContext") or {}).get("authorizer") or {}
    return (context.get("jwt") or {}).get("claims") or {}


def to_request(event: dict) -> Request:
    """An API Gateway v2 event as the Request the routers already understand."""
    http = (event.get("requestContext") or {}).get("http") or {}
    path = event.get("rawPath") or http.get("path") or "/"

    # With a named stage the stage is part of the path and is not part of any
    # route. $default, which is what the stack uses, leaves the path alone.
    stage = (event.get("requestContext") or {}).get("stage") or ""
    if stage and stage != "$default" and path.startswith("/" + stage):
        path = path[len(stage) + 1:] or "/"

    body = event.get("body") or b""
    if isinstance(body, str):
        body = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")

    return Request(
        method=(http.get("method") or event.get("httpMethod") or "GET").upper(),
        path=path,
        query=dict(event.get("queryStringParameters") or {}),
        body=body,
        context={"user_id": claims(event).get("sub", ""), "event": event},
    )


def reply(body: bytes, status: int, content_type: str = "application/json") -> dict:
    # No CORS headers. The HTTP API adds them from its own configuration, and a
    # second Access-Control-Allow-Origin is not a stricter one -- the browser
    # rejects the pair outright.
    return {
        "statusCode": status,
        "headers": {"content-type": content_type, "cache-control": "no-store"},
        "body": body.decode("utf-8"),
        "isBase64Encoded": False,
    }


def waker(event, context=None):
    """Every five minutes: is there work, and is there anything to do it?

    A safety net, not the mechanism. Submitting a job starts a machine itself;
    this is for the case where that call failed -- a throttle, no capacity at
    the time, a start quota that has since reset -- and a job would otherwise
    sit on the queue until somebody noticed.

    It launches under the same lock as everything else, so a run that overlaps
    with somebody pressing Start cannot produce two machines.
    """
    try:
        attributes = client("sqs").get_queue_attributes(
            QueueUrl=setting("QUEUE"),
            AttributeNames=["ApproximateNumberOfMessagesVisible"])["Attributes"]
        waiting = int(attributes.get("ApproximateNumberOfMessagesVisible", 0))
    except Exception as error:
        print(f"[waker] could not read the queue: {error}", flush=True)
        return {"waiting": None, "launched": False}

    if not waiting:
        return {"waiting": 0, "launched": False}

    found = read_machine()
    if live(found["state"]):
        return {"waiting": waiting, "launched": False,
                "state": found["state"].get("state")}

    print(f"[waker] {waiting} job(s) waiting and no machine; starting one", flush=True)
    try:
        # Charged to the machine rather than to a person: nobody pressed
        # anything, and the global launch cap still applies.
        start_machine("waker")
    except ApiError as error:
        print(f"[waker] could not start one: {error}", flush=True)
        return {"waiting": waiting, "launched": False, "error": str(error)}
    return {"waiting": waiting, "launched": True}


def handler(event, context=None):
    """The Lambda entry point, shared by all three functions.

    Which routes this copy serves comes from PROTEINCAD_FUNCTION, and so do the
    permissions on its role. Keeping both lists means a request that reaches the
    wrong function is refused by the router rather than by a call it has no
    permission to make -- the same answer, from the layer that can explain it.
    """
    which = setting("FUNCTION", "api")
    router = ROUTERS.get(which)
    if router is None:
        return reply(json.dumps({"error": f"PROTEINCAD_FUNCTION={which!r} is not one of "
                                          f"{', '.join(sorted(ROUTERS))}"}).encode(), 500)

    request = to_request(event)
    try:
        response = router.dispatch(request)
    except ApiError as error:
        return reply(json.dumps({"error": str(error)}).encode("utf-8"), error.status)
    except Exception as error:  # a handler blew up: report it, keep serving
        import traceback

        traceback.print_exc()
        return reply(json.dumps({"error": f"{type(error).__name__}: {error}"}).encode(), 500)
    return reply(response.body, response.status, response.content_type)
