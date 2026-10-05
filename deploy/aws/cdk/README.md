# The stack

Every AWS resource proteinCAD uses, as code. Read
[`proteincad_stack.py`](proteincad_stack.py) top to bottom and it is the
architecture; the two parts most worth a second look are `permissions`, which is
all of the IAM in one place, and `machine`, which is the only thing here that
can cost real money — and which creates nothing until somebody asks.

## Running it

```sh
cd deploy/aws/cdk
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

npx aws-cdk synth > /dev/null      # renders the template. Creates nothing.
python3 ../../../tools/check_stack.py   # 104 checks against what it rendered

npx aws-cdk diff                   # what would change. Creates nothing.
npx aws-cdk deploy                 # creates it
```

`synth` and `diff` are safe to run whenever. `deploy` is the one that spends
money, and the first time you run it in an account you will need
`npx aws-cdk bootstrap` first — that creates a `CDKToolkit` stack with a staging
bucket, which is CDK's own plumbing rather than anything of ours.

## Settings

Nothing is hardcoded. Everything that varies is context, with defaults in
`cdk.json`:

| | | |
|---|---|---|
| `proteincad:openSignUp` | `true` | anybody may make an account. `false` goes back to `admin-create-user`. Either way it is an in-place update — the pool keeps its accounts. |
| `proteincad:origin` | `https://adumbra.burakayy.com` | where the viewer is served from |
| `proteincad:devOrigins` | *empty* | extra origins for the API CORS, the bucket CORS and Cognito's callbacks. Comma-separated. Cognito allows plain HTTP for `localhost` only, and synth refuses anything else. |
| `proteincad:appPath` | `/proteincad/` | where the viewer lives on it |
| `proteincad:domainPrefix` | `proteincad-login` | the Cognito hosted-UI subdomain; must be unique across the region |
| `proteincad:instanceType` | `g4dn.xlarge` | enforced by IAM, not just by code |
| `proteincad:amiParameter` | Base OSS NVIDIA GPU AMI | an SSM path, resolved at deploy |
| `proteincad:rootVolumeGb` | `100` | floored by the AMI snapshot; the weights are not on it |
| `proteincad:maxDesigns` | `8` | per job |
| `proteincad:dailyJobs` | `20` | per user, per UTC day |
| `proteincad:concurrentJobs` | `2` | per user, at once |
| `proteincad:globalDailyJobs` | `100` | everyone together |
| `proteincad:machineStarts` | `5` | machine starts per user, per day |
| `proteincad:globalDailyLaunches` | `20` | launches by everyone together |
| `proteincad:idleMinutes` | `30` | before the machine shuts itself down |
| `proteincad:jobMemory` | `12g` | of the instance's 16 GiB |
| `proteincad:budgetUsd` | `120` | |
| `proteincad:budgetEmail` | *empty* | **set this, or no budget alarm is created.** The budget excludes credits and refunds, so it tracks spend rather than what you are billed. |

Override at deploy time:

```sh
npx aws-cdk deploy \
  -c proteincad:budgetEmail=you@example.com \
  -c proteincad:dailyJobs=5
```

The account and region come from your credentials (`CDK_DEFAULT_ACCOUNT` /
`CDK_DEFAULT_REGION`), so no account number appears anywhere in this directory.

## What it creates

A VPC with **three** public subnets and **no NAT gateway** — three because a
zone with no `g4dn` left is an ordinary Tuesday and the answer is another zone;
no NAT because the machine needs to reach out, nothing needs to reach in, and a
NAT gateway would be about $32/month to achieve what a security group with no
inbound rules already achieves for free.

| | |
|---|---|
| Cognito | user pool, self-signup **on** with email verification, hosted UI, PKCE client with no secret |
| DynamoDB | one table, on-demand, TTL, a `by_user` index, and the machine record |
| SQS | one standard queue + a dead letter queue at 2 receives |
| S3 | one private bucket; CORS for the one origin; lifecycle rules that cannot reach `weights/`; a `Deny` that stops anything deleting them |
| Lambda | `api`, `submit`, `waker` — three roles, three permission sets |
| HTTP API | JWT authorizer, one origin, throttled, one open route (`/health`) |
| EC2 | a **launch template** and **no instance**. IMDSv2 + hop limit 1, small encrypted gp3 deleted on termination, shutdown means terminate, **no inbound rules** |
| SSM | one parameter holding the image digest every machine boots with |
| ECR | `proteincad-model`, last 3 images |
| CodeBuild | two projects: one builds the image for `linux/amd64` and pins the digest, one fetches 12.5 GB of weights from source into the bucket. Neither runs on a trigger — you start them, twice in the life of a deployment. |
| EventBridge | 5-minute waker, which launches if the queue is not empty and nothing is running |
| Budgets | monthly alert, if you set the email |

`cdk destroy` leaves the user pool, the bucket and the table behind. Those hold
who may sign in, twelve gigabytes of model weights, everybody's designs and the
record of them; a destroy typed in the wrong terminal should not take them.
Delete them by hand if you really mean to.

It does **not** leave a machine behind, because there usually is not one. If a
job is running when you destroy the stack, that instance keeps going until its
idle timer fires and then terminates itself — the launch template is gone by
then, but the shutdown behaviour was baked into the instance at launch.

## How the launch permission is scoped

`ec2:RunInstances` is authorised against every resource a launch touches, not
just the template, so it takes two statements. The condition on the second is
what ties them together:

```
ArnEquals    ec2:LaunchTemplate = <this stack's template>
StringEquals ec2:InstanceType   = g4dn.xlarge
```

The first means the role may launch only from that template. The second means
"never fall back to something pricier" is a rule AWS enforces rather than a rule
the code remembers. `iam:PassRole` names the one instance role and the one
service. `ec2:CreateTags` is conditioned on `ec2:CreateAction = RunInstances`.

**Nothing may stop or terminate an instance.** Not the API, not the launcher,
not a machine itself — the machine ends by running `shutdown -h now`, which is
not an API call, and the launch template turns that into a termination.
`check_stack.py` asserts all of this.

One honest caveat: `ec2:DescribeInstances` does not support resource-level
permissions and AWS requires `*`. It is not used by the API at all any more —
the DynamoDB record is what the panel reads — so it appears only where a machine
looks itself up.

The two build roles are worth reading too, because they are the only things
here that can write an artefact everything else trusts. The image builder may
push to the one ECR repository and write the one SSM parameter, and nothing
else; the weights builder may `PutObject` and `GetObject` under `weights/`, and
nothing else. Neither has any EC2 or IAM permission at all, and neither can
delete a weight — the bucket policy denies that to everything in the account.

## The user pool is the one thing you can delete by editing a line

Four properties of `AWS::Cognito::UserPool` have an update behaviour of
**Replacement**: `UsernameAttributes`, `AliasAttributes`, `Schema` and
`UsernameConfiguration`. Changing any of them does not modify the pool — it
creates a new one, deletes the old one, and takes every account with it. In
`identity()` those are `sign_in_aliases` and `standard_attributes`.

`check_stack.py` asserts their exact values rather than their presence, so an
edit fails a check instead of a Saturday. `self_sign_up_enabled` is not one of
them: it maps to `AdminCreateUserConfig`, which updates in place in both
directions.

## Retained on destroy
