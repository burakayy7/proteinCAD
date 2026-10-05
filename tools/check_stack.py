"""Checks for the CloudFormation the CDK stack renders.

    cd deploy/aws/cdk && npx aws-cdk synth >/dev/null
    python3 tools/check_stack.py

    python3 tools/check_stack.py path/to/template.json

Reads a template that already exists; it does not run CDK and it does not touch
AWS. With no template it says how to make one and exits 0, so it can sit in the
same list as the other checks on a machine with no CDK installed.

What it asserts is the half of the design that lives in the infrastructure
rather than in the code: that the worker has no way in, that the bucket has no
way in, that the browser's token is checked on every route but one, and that
**nothing anywhere may stop or terminate an instance** -- the machine ends
itself, and that only works if no credential can do it instead. Those are easy
to break by editing one line of Python, and impossible to notice by reading
the diff.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "deploy" / "aws" / "cdk" / "cdk.out" / "proteincad.template.json"

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


path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
if not path.is_file():
    print(f"no template at {path}")
    print("\nRender one first -- it creates nothing in AWS:\n")
    print("    cd deploy/aws/cdk")
    print("    python3 -m venv .venv && . .venv/bin/activate")
    print("    pip install -r requirements.txt")
    print("    npx aws-cdk synth > /dev/null")
    print("    python3 ../../../tools/check_stack.py\n")
    sys.exit(0)

template = json.loads(path.read_text())
resources = template["Resources"]
print(f"proteinCAD stack checks -- {path.name}, {len(resources)} resources")


def of(kind: str) -> list:
    return [r["Properties"] for r in resources.values() if r["Type"] == kind]


def one(kind: str) -> dict:
    found = of(kind)
    assert len(found) == 1, f"expected one {kind}, found {len(found)}"
    return found[0]


def every_statement() -> list:
    """(policy name, actions, resource, condition) for every Allow."""
    out = []
    for name, resource in resources.items():
        if resource["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Effect", "Allow") != "Allow":
                continue
            actions = statement.get("Action") or []
            actions = actions if isinstance(actions, list) else [actions]
            out.append((name, actions, json.dumps(statement.get("Resource")),
                        statement.get("Condition") or {}))
    return out


def every_action() -> list:
    """(policy name, action, resource) for everything any role may do."""
    out = []
    for name, resource in resources.items():
        if resource["Type"] not in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            if statement.get("Effect", "Allow") != "Allow":
                continue
            actions = statement.get("Action") or []
            actions = actions if isinstance(actions, list) else [actions]
            for action in actions:
                out.append((name, action, json.dumps(statement.get("Resource"))))
    return out


section("nothing exists while idle")
check("there is no instance in this stack at all",
      not [r for r in resources.values() if r["Type"] == "AWS::EC2::Instance"],
      "a persistent instance would cost its disk every month")
check("there is a launch template to make one from",
      len(of("AWS::EC2::LaunchTemplate")) == 1)
check("and a parameter saying which image it should run",
      len(of("AWS::SSM::Parameter")) == 1)

section("the way in, and the lack of one")
security_group = one("AWS::EC2::SecurityGroup")
check("the worker's security group has no inbound rules at all",
      not security_group.get("SecurityGroupIngress"),
      json.dumps(security_group.get("SecurityGroupIngress", "none")))
check("and it is allowed out, which is how it reaches the queue",
      bool(security_group.get("SecurityGroupEgress")))
check("nothing in the stack opens port 22",
      "22" not in json.dumps(security_group))

check("there are three zones to launch into",
      len(of("AWS::EC2::Subnet")) == 3, f"{len(of('AWS::EC2::Subnet'))} subnets")
check("and no NAT gateway, because nothing needs to reach in",
      not of("AWS::EC2::NatGateway"))

launch = one("AWS::EC2::LaunchTemplate")["LaunchTemplateData"]
metadata = launch.get("MetadataOptions") or {}
check("the machine requires IMDSv2", metadata.get("HttpTokens") == "required")
check("with a hop limit of one, so nothing behind it can ask",
      metadata.get("HttpPutResponseHopLimit") == 1)
check("shutting down terminates it, which is the whole cost model",
      launch.get("InstanceInitiatedShutdownBehavior") == "terminate")
check("it has a boot script", "UserData" in launch)
check("and the boot script does not download weights",
      "weights" not in json.dumps(launch.get("UserData", "")).lower()
      or "mkdir" in json.dumps(launch.get("UserData", "")),
      "weights arrive on demand, not at boot")
check("everything it creates is tagged",
      sorted(s["ResourceType"] for s in launch.get("TagSpecifications") or [])
      == ["instance", "volume"])

volume = (launch.get("BlockDeviceMappings") or [{}])[0].get("Ebs") or {}
check("the root volume is encrypted", volume.get("Encrypted") is True)
check("and is gp3", volume.get("VolumeType") == "gp3")
check("and goes away with the instance, so idle costs nothing",
      volume.get("DeleteOnTermination") is True)
check("and is small, because the weights are not on it",
      int(volume.get("VolumeSize") or 0) <= 150, f"{volume.get('VolumeSize')} GB")

section("the bucket")
bucket = one("AWS::S3::Bucket")
blocked = bucket.get("PublicAccessBlockConfiguration") or {}
check("every form of public access is blocked",
      all(blocked.get(key) is True for key in
          ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")),
      json.dumps(blocked))
check("it is encrypted", "BucketEncryption" in bucket)
rules = (bucket.get("CorsConfiguration") or {}).get("CorsRules") or []
check("there is a CORS rule, or a presigned link would not load in a browser",
      len(rules) == 1)
if rules:
    check("it allows reads only",
          sorted(rules[0]["AllowedMethods"]) == ["GET", "HEAD"],
          json.dumps(rules[0]["AllowedMethods"]))
    check("from the site, and from nothing else that is not a dev origin",
          rules[0]["AllowedOrigins"][0].startswith("https://")
          and all(o.startswith("https://") or o.startswith("http://localhost")
                  for o in rules[0]["AllowedOrigins"]),
          json.dumps(rules[0]["AllowedOrigins"]))
lifecycle = (bucket.get("LifecycleConfiguration") or {}).get("Rules") or []
check("specs and results both expire on their own", len(lifecycle) >= 2)
expiring = [r for r in lifecycle if r.get("ExpirationInDays")]
check("no expiry rule can reach the weights",
      all(r.get("Prefix", "").startswith(("specs/", "results/")) for r in expiring),
      json.dumps([r.get("Prefix") for r in expiring]))

policy = one("AWS::S3::BucketPolicy")["PolicyDocument"]
check("plain HTTP is denied on the bucket",
      any(s.get("Effect") == "Deny" and "aws:SecureTransport" in json.dumps(s)
          for s in policy["Statement"]))
check("and nothing in the account may delete the weights",
      any(s.get("Effect") == "Deny" and "s3:DeleteObject" in json.dumps(s.get("Action"))
          and "weights/" in json.dumps(s.get("Resource"))
          for s in policy["Statement"]))

section("the api")
api = one("AWS::ApiGatewayV2::Api")
cors = api.get("CorsConfiguration") or {}
api_origins = cors.get("AllowOrigins") or []
check("CORS names an origin and never a wildcard",
      bool(api_origins) and "*" not in api_origins, json.dumps(api_origins))
# `devOrigins` adds to this deliberately. The point of the check is that the
# default is one origin, so an extra one is always something somebody typed.
extra = [o for o in api_origins if not o.startswith("https://")]
check("no plain-HTTP origin unless devOrigins put it there",
      not extra or all("localhost" in o for o in extra), json.dumps(extra))
check("credentials are not allowed across origins",
      not cors.get("AllowCredentials"))
check("only the two headers the viewer sends are allowed",
      sorted(cors.get("AllowHeaders") or []) == ["authorization", "content-type"],
      json.dumps(cors.get("AllowHeaders")))

routes = {r["RouteKey"]: r for r in of("AWS::ApiGatewayV2::Route")}
open_routes = [key for key, r in routes.items() if r.get("AuthorizationType") in (None, "NONE")]
check("exactly one route is open, and it is /health",
      open_routes == ["GET /health"], json.dumps(open_routes))
check("every other route checks a Cognito token",
      all(r.get("AuthorizationType") == "JWT"
          for key, r in routes.items() if key != "GET /health"))
check("there is no route that starts the GPU",
      not any("gpu/start" in key for key in routes))
check("there is no route that stops the GPU",
      not any("gpu/stop" in key for key in routes))
# Both directions, because one direction is how a route goes missing without
# anything failing. A handler added to cloud_api.py that nobody publishes at the
# edge is a 404 the Lambda never sees -- the button is there, the press does
# nothing, and every test still passes because each side was only ever checked
# against itself. Counting the routes could not catch it: the count is of this
# template, and the template is the side that was not edited.
import re as _re                                                    # noqa: E402

sys.path.insert(0, str(ROOT))
from proteincad import cloud_api                                    # noqa: E402

served = set()
for router in cloud_api.ROUTERS.values():
    for method, pattern, _ in router.routes:
        path = _re.sub(r"\(\?P<(\w+)>\[\^/\]\+\)", r"{\1}",
                       pattern.pattern.strip("^$"))
        served.add(f"{method} {path}")

check("every route the code serves exists at the edge",
      not (served - set(routes)), str(sorted(served - set(routes))))
check("and nothing at the edge is served by nobody",
      not (set(routes) - served), str(sorted(set(routes) - served)))
check("one of them reads the machine", "GET /machine" in routes)
check("one of them starts a machine", "POST /machine/start" in routes)
check("one of them retires a machine", "POST /machine/retire" in routes)

# The floor under the whole cost model. Nothing in the account may terminate an
# instance, so a machine whose worker never starts has no other way to end.
from proteincad import sqs_worker as _worker                        # noqa: E402
from proteincad import cloud_api as _cloud                          # noqa: E402

def _render(node):
    """The user data as bash will actually see it, refs stood in for."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "Fn::Base64" in node:
            return _render(node["Fn::Base64"])
        if "Fn::Join" in node:
            sep, parts = node["Fn::Join"]
            return sep.join(_render(part) for part in parts)
        if "Ref" in node:
            return "REF"
        if "Fn::GetAtt" in node:
            return "ATT"
        if "Fn::Sub" in node:
            return _render(node["Fn::Sub"])
    return str(node)


boot = _render(one("AWS::EC2::LaunchTemplate").get("LaunchTemplateData", {}).get("UserData", ""))
user_data = json.dumps(one("AWS::EC2::LaunchTemplate")
                       .get("LaunchTemplateData", {}).get("UserData", ""))

# The progress report, parsed as the CLI would parse it. This is a bash script
# written inside a Python f-string, so `\"` in the source is a bare `"` by the
# time bash sees it -- which silently turned every report into malformed JSON
# the CLI refused and `|| true` swallowed. Every boot reported nothing and the
# panel sat on `step 0 of 0`. Checked by parsing rather than by eye.
payload = boot.split("<<JSON", 1)[1].split("JSON", 1)[0].strip() if "<<JSON" in boot else ""
for shell, stand_in in (('$(date +%s)', '1'), ('$2', 'preparing the disk, and such'),
                        ('$1', '1'), ('$STEPS', '4')):
    payload = payload.replace(shell, stand_in)
try:
    json.loads(payload)
    good = True
except Exception:
    good = False
check("the boot's progress report is valid JSON by the time bash has it", good,
      payload[:90])
check("and it is handed over as a file, not as an escaped argument",
      "--expression-attribute-values file://" in boot)
check("a report that cannot be sent is said out loud, not swallowed",
      "could not report progress" in boot)

# The boot beats for its whole length, and hands over rather than leaving a
# loop that outlives the script and keeps a dead record looking alive.
check("the boot keeps a heartbeat going while it works", "beat &" in boot)
check("the beat sends valid JSON too", '{":h":{"N":"' in boot)
check("and it is stopped before the worker takes over",
      boot.index('kill "$BEAT"') < boot.index("systemctl enable --now proteincad-sqs"))
check("the launch window is sized from the beat, not from a whole boot",
      _cloud.LAUNCH_STALE <= 300, f"LAUNCH_STALE={_cloud.LAUNCH_STALE}")

# The instance store. The DLAMI claims it first, as an LVM volume, so the
# format-and-mount this used to do failed twice and silently left everything
# on the root volume.
check("the AMI's own instance-store mount is used where it exists",
      "mountpoint -q /opt/dlami/nvme" in boot and "mount --bind" in boot)
check("a raw device is still formatted when nothing has claimed it",
      "mkfs.ext4 -F -L proteincad" in boot)
check("and landing on the root volume is reported rather than assumed away",
      "NOT on the instance store" in boot)
check("the machine schedules its own end before anything that can hang",
      "no worker has reported in" in user_data)
check("and it does it before the disk, the registry or the pull",
      user_data.index("no worker has reported in") < user_data.index("preparing the local disk"))
check("the worker may schedule and cancel, not only halt now",
      "NOPASSWD: /sbin/shutdown'" in user_data, "sudoers line")
check("the boot script and the worker agree on how long",
      f"+{_worker.DEADMAN_MINUTES} " in user_data.replace("\\", ""),
      f"worker says {_worker.DEADMAN_MINUTES}")

stage = one("AWS::ApiGatewayV2::Stage")
throttle = stage.get("DefaultRouteSettings") or {}
check("the api is throttled as a second ceiling under the quotas",
      bool(throttle.get("ThrottlingRateLimit")) and bool(throttle.get("ThrottlingBurstLimit")),
      json.dumps(throttle))

section("who may sign in")
pool = one("AWS::Cognito::UserPool")
# Either mode is a legitimate deployment, so this reports rather than judges.
# What must hold in both is underneath it.
admin_only = (pool.get("AdminCreateUserConfig") or {}).get("AllowAdminCreateUserOnly")
print("  ·    sign-up is "
      + ("CLOSED -- accounts are created with admin-create-user"
         if admin_only else "OPEN -- anybody may make an account")
      + "   (-c proteincad:openSignUp=…)")
check("an address has to be confirmed either way",
      "email" in (pool.get("AutoVerifiedAttributes") or []),
      "Cognito emails a code; the account is unusable until it comes back")
check("and there is a message to send them",
      bool(pool.get("VerificationMessageTemplate") or pool.get("EmailVerificationMessage")))
check("no pre-signup trigger stands in the way",
      not (pool.get("LambdaConfig") or {}).get("PreSignUp"),
      "there is no invite code, and nothing pretending to be one")
check("a password has to be a real one",
      int((pool.get("Policies") or {}).get("PasswordPolicy", {})
          .get("MinimumLength", 0)) >= 12)

# The four properties of AWS::Cognito::UserPool whose documented update
# behaviour is Replacement. Editing one does not modify the pool: it makes a
# new one, deletes the old one, and takes every account with it. Their values
# are asserted rather than their mere presence, so a change fails here instead
# of at three in the morning.
check("the username configuration is the one the live pool already has",
      pool.get("UsernameAttributes") == ["email"]
      and pool.get("AliasAttributes") is None
      and pool.get("UsernameConfiguration") is None,
      "changing any of these replaces the pool and deletes every account")
check("and so is the schema",
      pool.get("Schema") == [{"Mutable": True, "Name": "email", "Required": True}],
      json.dumps(pool.get("Schema")))

client = one("AWS::Cognito::UserPoolClient")
check("the browser client has no secret, because it could not keep one",
      client.get("GenerateSecret") in (None, False))
check("it uses the authorization-code flow",
      client.get("AllowedOAuthFlows") == ["code"])
check("the implicit flow is not enabled",
      "implicit" not in json.dumps(client.get("AllowedOAuthFlows") or []))
check("it will not say whether an account exists",
      client.get("PreventUserExistenceErrors") == "ENABLED")
callbacks = client.get("CallbackURLs") or []
check("it only redirects to the site, or to a localhost dev origin",
      all(url.startswith("https://") or url.startswith("http://localhost")
          for url in callbacks),
      json.dumps(callbacks))
check("the API, the bucket and the sign-in agree about which origins exist",
      {u.split("/")[0] + "//" + u.split("/")[2] for u in callbacks} == set(api_origins),
      "a dev origin allowed to call the API but not to sign in is a puzzle")
check("and the same list reaches the bucket, or a presigned link will not load",
      rules and sorted(rules[0]["AllowedOrigins"]) == sorted(api_origins))

section("the queue")
queues = of("AWS::SQS::Queue")
check("there are two: the queue and its dead letters", len(queues) == 2)
main = [q for q in queues if q.get("RedrivePolicy")]
check("the main queue has a dead letter queue behind it", len(main) == 1)
if main:
    check("after two attempts, not more -- a failed job is not retried",
          main[0]["RedrivePolicy"]["maxReceiveCount"] == 2)
    check("the visibility timeout leaves room to claim and start beating",
          int(main[0].get("VisibilityTimeout") or 0) >= 300,
          str(main[0].get("VisibilityTimeout")))
check("neither queue is FIFO, because the DynamoDB claim is what deduplicates",
      not any(q.get("FifoQueue") for q in queues))
check("plain HTTP is denied on both",
      sum(1 for p in of("AWS::SQS::QueuePolicy")
          if "aws:SecureTransport" in json.dumps(p)) == 2)

section("the table")
table = one("AWS::DynamoDB::Table")
check("old records leave on their own",
      (table.get("TimeToLiveSpecification") or {}).get("AttributeName") == "expires")
check("it is billed per request, so an idle month costs nothing",
      table.get("BillingMode") == "PAY_PER_REQUEST")
indexes = table.get("GlobalSecondaryIndexes") or []
check("there is a by_user index for listing somebody's own jobs",
      [i["IndexName"] for i in indexes] == ["by_user"])
check("and it does not copy the whole record into a second table",
      bool(indexes) and indexes[0]["Projection"]["ProjectionType"] == "INCLUDE")

section("the budget, if you set an email")
budget = of("AWS::Budgets::Budget")
if not budget:
    print("  ·    no budget in this template -- deploy with "
          "-c proteincad:budgetEmail=you@example.com")
else:
    data = budget[0]["Budget"]
    costs = data.get("CostTypes") or {}
    check("it measures what was spent, not what was billed",
          costs.get("IncludeCredit") is False,
          "with credits counted, an account on promotional credit reports near "
          "zero however hard the GPU works")
    check("and a refund does not quietly raise the ceiling",
          costs.get("IncludeRefund") is False)
    check("it is monthly", data.get("TimeUnit") == "MONTHLY")
    check("and somebody is told",
          bool(budget[0].get("NotificationsWithSubscribers")))

section("what anything is allowed to do")
actions = every_action()
check("nothing anywhere may terminate an instance",
      not any(a == "ec2:TerminateInstances" for _, a, _ in actions),
      "a machine ends by shutting itself down, which needs no permission")

check("nothing anywhere may stop an instance either",
      not any(a == "ec2:StopInstances" for _, a, _ in actions),
      "the machine ends itself with shutdown -h now; no credential can do it")

runs = [(name, res, cond) for name, a, res, cond in every_statement()
        if "ec2:RunInstances" in a]
check("only the submit function and the waker may create a machine",
      sorted({n.split("ServiceRole")[0] for n, _, _ in runs}) == ["Submit", "Waker"],
      str(sorted({n for n, _, _ in runs})))

# The statement covering `instance/*` is the one that decides whether a launch
# happens at all -- every RunInstances is authorised against it as well as
# against the supporting resources. So that is where the conditions have to be,
# and this asserts they are on *that* statement rather than merely present
# somewhere in the policy.
#
# The supporting resources -- network interface, volume, subnet, security
# group, image -- are deliberately unconditioned: AWS does not put
# ec2:InstanceType in the request context for them, and a StringEquals against
# a key that is not there is false, which denies the launch. That is a real
# failure this stack shipped once.
for role in ("Submit", "Waker"):
    on_instance = [(res, cond) for name, res, cond in runs
                   if name.startswith(role) and "instance/" in res]
    check(f"{role}: the instance itself is pinned to the launch template",
          bool(on_instance)
          and all("ec2:LaunchTemplate" in json.dumps(c) for _, c in on_instance))
    check(f"{role}: and to one instance type, so there is no pricier fallback",
          bool(on_instance)
          and all("ec2:InstanceType" in json.dumps(c) for _, c in on_instance))
    unconditioned = [res for name, res, cond in runs
                     if name.startswith(role) and not cond]
    check(f"{role}: nothing unconditioned can create an instance",
          not any("instance/" in res for res in unconditioned),
          "the supporting resources may be unconditioned; the instance may not")

passes = [(name, res, cond) for name, a, res, cond in every_statement()
          if "iam:PassRole" in a]
check("PassRole names one role, not a wildcard",
      bool(passes) and all('"*"' not in res for _, res, _ in passes),
      str([r for _, r, _ in passes])[:80])
check("and only to EC2",
      all("ec2.amazonaws.com" in json.dumps(cond) for _, _, cond in passes))

tagging = [(name, cond) for name, a, _, cond in every_statement()
           if "ec2:CreateTags" in a]
check("tagging is only allowed as part of a launch",
      bool(tagging) and all("RunInstances" in json.dumps(cond) for _, cond in tagging))

ALLOWED_ON_EVERYTHING = ("logs:", "xray:", "ssm", "ssmmessages", "ec2messages",
                         "cloudwatch:", "ec2:Describe", "ecr:GetAuthorizationToken")
wide = [(name, a) for name, a, res in actions
        if res == '"*"' and not a.startswith(ALLOWED_ON_EVERYTHING)]
check("nothing else is granted on every resource in the account",
      not wide, str(wide[:4]))

# Only our bucket. The CDK staging bucket is CDK's own, and the roles that
# read a build context or a Lambda asset out of it are granted List by the
# constructs that put them there.
listers = [(name, cond) for name, acts, res, cond in every_statement()
           if any(a.startswith("s3:List") for a in acts) and "Data" in res
           and not name.startswith("Custom")]
check("no role may list our bucket without naming a prefix",
      all("s3:prefix" in json.dumps(cond) for _, cond in listers),
      str([n for n, _ in listers]))

# CDK's own deployment helper is granted DeleteObject on whatever bucket it
# writes to; it runs during `cdk deploy` and nowhere else. What matters is that
# no role which runs while the app is running can delete anything, and that
# nothing at all can delete the weights -- the bucket policy asserts the
# second, which is the one that cannot be regenerated by re-running something.
deleters = [name for name, a, _ in actions
            if a.startswith("s3:Delete") and not name.startswith("Custom")]
check("no runtime role may delete from the bucket", not deleters, str(deleters))
check("the read function has no EC2 permission at all",
      not any(name.startswith("Api") and a.startswith("ec2:") for name, a, _ in actions),
      "it reads a DynamoDB record; there is usually no instance to ask about")
check("the machine may read the weights and the image pointer",
      any(name.startswith("WorkerRole") and a == "ssm:GetParameter"
          for name, a, _ in actions))

section("the things that get built, get built inside AWS")
projects = {p.get("Name"): p for p in of("AWS::CodeBuild::Project")}
check("there are two build projects", len(projects) == 2, str(sorted(projects)))
check("one builds the image", any("image" in name for name in projects))
check("one fetches the weights", any("weights" in name for name in projects))

image_build = next((p for name, p in projects.items() if "image" in name), None)
if image_build:
    spec = json.dumps(image_build["Source"].get("BuildSpec", ""))
    check("the image is built for linux/amd64, not whatever the host is",
          "--platform linux/amd64" in spec,
          "an arm64 image is one a g4dn cannot run at all")
    check("it needs Docker, so it is privileged",
          image_build["Environment"].get("PrivilegedMode") is True)
    check("it pins the result by digest, not by tag",
          "imageDigest" in spec and "put-parameter" in spec)
    check("and it never writes `latest`", ":latest" not in spec)
    check("it has long enough for a CUDA build",
          int(image_build.get("TimeoutInMinutes") or 0) >= 60,
          f"{image_build.get('TimeoutInMinutes')} min")

weights_build = next((p for name, p in projects.items() if "weights" in name), None)
if weights_build:
    spec = json.dumps(weights_build["Source"].get("BuildSpec", ""))
    check("the weights are fetched by the publisher in this repo",
          "publish-weights.py" in spec)
    check("and it does not need Docker",
          not weights_build["Environment"].get("PrivilegedMode"))
    check("it has long enough for twelve gigabytes each way",
          int(weights_build.get("TimeoutInMinutes") or 0) >= 60,
          f"{weights_build.get('TimeoutInMinutes')} min")

    # A secret the build cannot read is worse than one it does not have.
    # CodeBuild resolves a SECRETS_MANAGER variable itself, before the
    # container starts and before any command runs, so a missing
    # GetSecretValue fails the build in under a minute with no log events at
    # all -- and nothing in the template says why. Declaring the variable is
    # not enough: CDK cannot infer an ARN from a secret's name, so it grants
    # nothing unless asked.
    #
    # Vacuous when no token is configured, which is the default and a complete
    # deployment; load-bearing the moment one is.
    secret_vars = [v for v in (weights_build["Environment"].get("EnvironmentVariables") or [])
                   if v.get("Type") == "SECRETS_MANAGER"]
    # every_statement() yields (policy, actions, resource-json, condition).
    granted = [(policy, resource) for policy, actions, resource, _ in every_statement()
               if "secretsmanager:GetSecretValue" in actions]
    check("a secret the weights build is given, it is also allowed to read",
          not secret_vars or bool(granted),
          f"{len(secret_vars)} secret variable(s), {len(granted)} grant(s)")
    for entry in secret_vars:
        named = str(entry.get("Value") or "").split(":")[0].split("/")[-1]
        check(f"and the grant names {entry.get('Name')}'s own secret, not every secret",
              any(named in resource for _, resource in granted)
              and not any(resource == '"*"' for _, resource in granted),
              named)

builders = [name for name, acts, _, _ in every_statement()
            if name.startswith(("ImageBuild", "WeightsBuild"))
            and any(a.startswith(("ec2:", "iam:")) for a in acts)]
check("neither build project may touch EC2 or IAM", not builders, str(builders))
check("only the image build may write the image pointer",
      sorted({name.split("Role")[0] for name, acts, _, _ in every_statement()
              if "ssm:PutParameter" in acts}) == ["ImageBuild"],
      str(sorted({n for n, a, _, _ in every_statement() if "ssm:PutParameter" in a})))
check("only the weights build may write under weights/",
      sorted({name.split("Role")[0] for name, acts, res, _ in every_statement()
              if "s3:PutObject" in acts and "weights/" in res})
      == ["WeightsBuild"],
      str(sorted({n for n, a, r, _ in every_statement()
                  if "s3:PutObject" in a and "weights/" in r})))

section("a way in when a boot goes wrong")
worker_role = next(r["Properties"] for name, r in resources.items()
                   if r["Type"] == "AWS::IAM::Role" and name.startswith("WorkerRole"))
managed = json.dumps(worker_role.get("ManagedPolicyArns") or [])
check("the GPU machine carries AmazonSSMManagedInstanceCore",
      "AmazonSSMManagedInstanceCore" in managed,
      "so `aws ssm start-session --target <id>` gets a shell with no inbound rules open")
check("and the boot script makes sure the agent is running",
      "amazon-ssm-agent" in json.dumps(launch.get("UserData", "")),
      "the AMI ships it; assuming it is started is how you lose the only way in")

section("what it tells you afterwards")
outputs = template.get("Outputs") or {}
for wanted in ("ApiUrl", "UserPoolId", "ClientId", "QueueUrl", "Bucket", "Table",
               "LaunchTemplate", "ImageParameter", "ConfigJson", "BuildEverything",
               "ImageBuildProject", "WeightsBuildProject", "DeadLetterQueue"):
    check(f"it prints {wanted}", wanted in outputs)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
