# Putting proteinCAD on the web

The path that ends with `https://adumbra.burakayy.com/proteincad/` working for
people who are not you: a signed-in API in front, a queue in the middle, and a
GPU machine that **does not exist** unless somebody is designing something.

About **one dollar a month** when nobody is using it.

---

## What you get

Sign-up is **open**: anybody who can confirm an email address can make an
account and run designs. What bounds the cost is the quotas, not the guest
list — see [The limits](#the-limits-and-where-to-change-them).

```
browser (static page on Cloudflare Pages)
  │  Authorization: Bearer <Cognito id token>
  ▼
API Gateway ── JWT authorizer ──► two Lambdas
  │                                ├ validates the job against the same
  │                                │ catalogue the local server uses
  │                                ├ counts it against five limits
  │                                └ spec → S3, record → DynamoDB, pointer → SQS
  │
  ├── a conditional write on one DynamoDB record: the launch lock, so two
  │   people pressing Start at the same second get one machine, not two
  ▼
RunInstances from a launch template — zone a, then b, then c if AWS is full
  │
  ▼
g4dn.xlarge, tagged, no inbound rules, IMDSv2 hop limit 1
  │  boot: mount the NVMe, point Docker at it, pull the pinned image, start
  │  the worker. No weights.
  │
  ├── weights arrive on demand from s3://…/weights/, checked against a
  │   SHA-256 manifest, and shown to the user as they arrive
  ▼
docker run --network=none --read-only --cap-drop=ALL
  no credentials, no network, nothing to reach
  │
  ▼
results → private S3 → presigned link, five minutes → browser
  │
30 idle minutes ──► `shutdown -h now` ──► terminate ──► nothing exists again
```

**Nothing in the account may stop or terminate an instance.** The machine ends
itself, and the launch template's `InstanceInitiatedShutdownBehavior=terminate`
does the rest. There is no permission to misuse and no button to press by
mistake.

## What it costs

| | |
|---|---|
| 12.5 GB of model weights in S3 | **$0.29 / month** |
| ECR, one ~7 GB image | $0.70 / month |
| S3 specs and results, DynamoDB, SQS, Lambda, Cognito, API Gateway | pennies; mostly inside the free tiers |
| **standing total** | **about $1 / month** |
| `g4dn.xlarge` + its root volume, while a machine exists | **$0.54 / hour** |
| the two CodeBuilds, when you run them | about **$1 each**, twice in the life of a deployment |

Twenty designs a day at ten minutes each, with the machine idling thirty
minutes between sittings, is roughly four hours of GPU — about **$2.20**.

That is you using it. With sign-up open, the ceiling is set by
`globalDailyJobs`, and at its default of 100 the ceiling is about **$390 a
month** — see [The limits](#the-limits-and-where-to-change-them), which is the
section to read before you tell anybody the address.

The previous design kept a stopped instance with a 200 GB disk so that starting
it was a boot rather than a download. That was $16 a month, every month, to
avoid a wait of well under a minute. This is that trade made the other way
round.

**A cold start is about five minutes**: forty seconds to launch, then three
to four to boot and pull the seven-gigabyte image, then fifteen to forty-five
seconds for whichever weights the job needs. Measured on the real thing, not
guessed — the image pull is most of it. A second design in the same half hour is
immediate. That wait is why the panel has buttons — so it happens while you are
still setting a design up, and so you can watch it rather than wonder.

---

## Before you start

- An AWS account you are willing to spend money in.
- The AWS CLI, signed in: `aws sts get-caller-identity` should answer.
- Node (for `npx aws-cdk`) and Python 3.9+.
- The Cloudflare Pages project for adumbra, and the checkout it deploys from.

**You do not need Docker, and nothing large is uploaded from your machine.**
The model image and the model weights are both built inside AWS by CodeBuild —
the image because building it on an Apple Silicon laptop produces an arm64
image a `g4dn` cannot run at all, and the weights because twelve and a half
gigabytes each way is an evening on a domestic connection and forty minutes on
a build host beside the bucket.

Check the GPU quota first. New accounts often have a limit of **0** vCPUs for
on-demand G instances, and the failure shows up much later as every launch
failing:

```sh
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-DB2E81BA --region us-east-1 \
  --query 'Quota.Value'
```

A `g4dn.xlarge` needs 4. If it says `0.0`, request an increase in the Service
Quotas console — it usually takes a few hours — before going further.

---

## 0. Where am I?

```sh
deploy/aws/status.sh
```

Read-only, creates nothing. It walks the five steps below — stack, image,
weights, `config.json`, the site — and tells you which are done. Also whether a
GPU machine is running right now, which is the only line that costs anything.
Worth running when you come back to this after a few days.

## 1. Render the stack and read it

Nothing here creates anything.

```sh
cd deploy/aws/cdk
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

npx aws-cdk synth > /dev/null
python3 ../../../tools/check_stack.py
```

That last command asserts the parts that are easy to break and hard to notice:
no instance in the stack at all, no inbound rules on the machine, shutdown means
terminate, no public access on the bucket, no expiry rule that can reach the
weights, the image built for `linux/amd64`, one unauthenticated route, and
**nothing anywhere that may stop or terminate an instance**. It should say
`97 passed, 0 failed`.

## 2. Deploy it

```sh
npx aws-cdk bootstrap          # once per account+region; makes a CDKToolkit stack
npx aws-cdk diff               # the full list of what would be created
npx aws-cdk deploy -c proteincad:budgetEmail=you@example.com
```

### When cdk says `Subprocess exited with error 1`

The CLI could not run `app.py` and swallowed the reason. **To see it, run the
app directly** — the traceback the CLI hides:

```sh
cd deploy/aws/cdk
python3 app.py > /dev/null
```

Two causes account for almost all of these, and `app.py` now handles both
itself. They are worth knowing anyway, because the message names neither.

**The virtualenv.** `cdk.json` runs `python3 app.py`, and `python3` is whatever
your shell resolves — usually *not* `.venv`, because the CLI is not started
from inside it. `app.py` re-runs itself with `.venv/bin/python3` when
`aws_cdk` is not importable, so no activation is needed. If there is no venv at
all it says so, with the three commands to make one.

**No credentials.** If CDK cannot work out which account to deploy into, it
says `Unable to resolve AWS account to use` and stops. That means it resolved
no credentials at all — it fills the account in from whatever the CLI can see.
`app.py` now catches this and prints what to check; the short version is

```sh
aws sts get-caller-identity
```

and then `aws configure`, `aws configure sso`, or `aws sso login` depending on
what that says. If the CLI prints an account and CDK still cannot, the two are
reading different profiles — `export AWS_PROFILE=` the one that worked.

**TMPDIR.** CDK's Python bindings drive a Node process (jsii) that inherits
`TMPDIR` and immediately `mkdtemp`s in it. On macOS that variable is sometimes
left pointing at `/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T`, the
fallback temp directory, which is owned by **root** with mode `700` — so a
normal user gets

```
EACCES: permission denied, mkdtemp '.../jsii-kernel-XXXXXX'
```

before a line of this stack is evaluated. `app.py` checks the variable and
substitutes `/tmp` when it is unwritable, printing a note when it does.

Worth fixing in your shell too, since anything else that spawns a tool
expecting a usable temp directory will hit it:

```sh
echo "$TMPDIR"                       # if this is under /var/folders/zz/...
ls -ld "$TMPDIR"                     # ...and owned by root, that is the fault
grep -rn TMPDIR ~/.zshrc ~/.zprofile ~/.profile 2>/dev/null
unset TMPDIR                         # for this shell
```

Note that `python3 -c 'import tempfile; print(tempfile.gettempdir())'` will
**not** show the problem: Python silently skips a temp directory it cannot
write to and reports `/tmp`. Node takes `$TMPDIR` at its word.

The `NOTICES` block about `npm ci` and invalid lock files is unrelated advice
from the CLI, printed on every run. It is not an error, and nothing here uses
`npm ci`. Silence it with `npx aws-cdk acknowledge 37949`.

**Check the Cognito domain prefix before your first deploy.** It is globally
unique *per region*, not per account, and a collision fails the deploy on
`AWS::Cognito::UserPoolDomain` with "Domain already associated with another
user pool".

There is a trap in the check: once **your** stack exists, the name resolves
because you own it, which looks identical to somebody else owning it. And
changing this value on a live stack does not rename anything — it replaces the
domain, moving the hosted UI to a new URL and invalidating every `config.json`
pointing at the old one. Pick it once, before the first deploy.

```sh
host "$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='LoginDomain'].OutputValue" \
          --output text 2>/dev/null | sed 's|https://||')" 2>/dev/null \
  || host adumbra-proteincad.auth."$REGION".amazoncognito.com
```

`NXDOMAIN` means free. An answer means taken — pick another:

```sh
npx aws-cdk deploy -c proteincad:domainPrefix=something-else-entirely
```

**Set the budget email.** Without it no alarm is created at all. The budget is
deliberately configured to **exclude credits and refunds**, so it measures what
was spent rather than what was billed — an account sitting on promotional
credit otherwise reports near zero however hard the GPU is working, and the
alert arrives the month the credit runs out, describing a month you can no
longer change.

```sh
aws budgets describe-budgets --account-id "$(aws sts get-caller-identity \
  --query Account --output text)" \
  --query "Budgets[?BudgetName=='proteincad-monthly'].[BudgetLimit.Amount,CostTypes]"
```

About ten minutes, most of it the Cognito domain.

### Put the outputs in your shell

Everything below uses these. Run it once per terminal and the rest of this
document is copy-and-paste.

```sh
export STACK=proteincad
export REGION=us-east-1

out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

export API_URL=$(out ApiUrl)
export APP_URL=$(out AppUrl)
export CLIENT_ID=$(out ClientId)
export LOGIN_DOMAIN=$(out LoginDomain)
export USER_POOL=$(out UserPoolId)
export BUCKET=$(out Bucket)
export QUEUE=$(out QueueUrl)
export DLQ=$(out DeadLetterQueue)
export TABLE=$(out Table)
export IMAGE_PARAM=$(out ImageParameter)
export IMAGE_PROJECT=$(out ImageBuildProject)
export WEIGHTS_PROJECT=$(out WeightsBuildProject)

printf 'api      %s\napp      %s\nclient   %s\nlogin    %s\npool     %s\nbucket   %s\nimage    %s\nweights  %s\n' \
  "$API_URL" "$APP_URL" "$CLIENT_ID" "$LOGIN_DOMAIN" "$USER_POOL" "$BUCKET" \
  "$IMAGE_PROJECT" "$WEIGHTS_PROJECT"
```

Or just read the lot:

```sh
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output table
```

**No instance is created.** There is a launch template, a bucket, a queue, a
table and two CodeBuild projects, and together they cost about a dollar a
month. Nothing is running.

`cdk deploy` also uploads the build context — this repository minus the viewer,
the sample structures and the notebooks — so the two builds below build the
version you just deployed rather than whatever is on a branch somewhere. If you
change the Dockerfile or the weights publisher later, `cdk deploy` again before
rebuilding.

## 3. Accounts

**Anyone who visits the site can make one.** The Account panel has a
**Create account** button beside Sign in; it goes to Cognito's registration
form, which emails a code, and the account does not work until that code is
typed back in. There is no invite code, no allow-list, and nothing for you to
do here at all — not even for your own account. Open the site and press
**Create account**.

Which means the quotas are the whole of the cost control. [Their values and
where to change them](#the-limits-and-where-to-change-them) is the section to
read before you put this in front of anybody.

To see who has signed up:

```sh
aws cognito-idp list-users --region "$REGION" --user-pool-id "$USER_POOL" \
  --query 'Users[].[Username,UserStatus,UserCreateDate]' --output table
```

`UNCONFIRMED` means they started and never typed the code in; those accounts
cannot sign in and cannot queue anything.

To throw somebody out:

```sh
aws cognito-idp admin-delete-user --region "$REGION" \
  --user-pool-id "$USER_POOL" --username someone@example.com
```

That stops them signing in — and stops nothing else. Jobs they already queued
still run: the worker does not know who submitted what, and stopping one
mid-flight would leave a half-written result in the bucket. It also does not
stop them signing up again with the same address, because that is what open
sign-up means. If you need somebody kept out, close sign-up (below) and go back
to creating accounts yourself.

You can still make an account for somebody directly, which skips the
verification email:

```sh
aws cognito-idp admin-create-user \
  --region "$REGION" --user-pool-id "$USER_POOL" \
  --username someone@example.com \
  --user-attributes Name=email,Value=someone@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

### Closing it again

One context value and a deploy. It is an in-place update: the pool keeps its
identity and every existing account survives.

```sh
cd deploy/aws/cdk
npx aws-cdk deploy -c proteincad:openSignUp=false
```

## 4. Build the model image

```sh
deploy/aws/worker/build.sh image
```

About twenty-five minutes, almost all of it installing torch and DGL. It starts
a CodeBuild, prints the phase every minute, and finishes by telling you what it
pinned. Nothing runs on your machine but `start-build`.

It builds with `--platform linux/amd64` and pushes to ECR, then writes the
**digest** to the SSM parameter every machine reads at boot. There is no file to
edit and nothing to restart: the next machine created picks it up.

A digest rather than a tag, on purpose. A machine created on demand must not be
able to pull whatever happened to be pushed since somebody chose; the worker
refuses to start on `:latest`.

To watch it properly, or to drive CodeBuild directly:

```sh
aws logs tail "/aws/codebuild/$IMAGE_PROJECT" --follow

# what build.sh does, without build.sh
aws codebuild start-build --region "$REGION" --project-name "$IMAGE_PROJECT"

# what it pinned
aws ssm get-parameter --region "$REGION" --name "$IMAGE_PARAM" \
  --query Parameter.Value --output text
```

## 5. Fetch the weights

```sh
deploy/aws/worker/build.sh weights
```

About forty minutes. The same shape: a CodeBuild starts, and this one runs
[`publish-weights.py`](worker/publish-weights.py) beside the bucket. It fetches
the eight RFdiffusion checkpoints from the IPD, ProteinMPNN's
`vanilla_model_weights` from its repository and `facebook/esmfold_v1` from
Hugging Face; hashes everything; uploads it to `weights/`; and writes
`weights/manifest.json`. About 12.5 GB in and 12.5 GB out, over a link that is
not yours.

**ESM3 is not in that run, and a deployment is complete without it.** It is the
second backbone engine, and its weights are 5.5 GB that a deployment using only
RFdiffusion has no use for — so they are asked for by name:

```sh
deploy/aws/worker/build.sh weights esm3
```

They are public and need no token. If the hub ever rate-limits the anonymous
download — which it does per address, and a machine created for a job has a new
one each time — a token helps, and it goes in Secrets Manager rather than on a
command line:

```sh
aws secretsmanager create-secret --region "$REGION" \
  --name proteincad/hf-token --secret-string 'hf_...'

cdk deploy -c proteincad:hfTokenSecret=proteincad/hf-token
```

That is optional and off by default. Note that naming a secret and not creating
it fails the build before it runs a command and before it writes a log line,
because CodeBuild resolves the value itself — so set one or the other, not the
name alone.

Idempotent: anything already in the bucket at the right size with the right hash
is skipped, so a run that fails halfway resumes. One model at a time, if you
only need to replace one:

```sh
deploy/aws/worker/build.sh weights esmfold
deploy/aws/worker/build.sh weights esm3        # the second engine, 5.5 GB

# the same, directly
aws codebuild start-build --region "$REGION" --project-name "$WEIGHTS_PROJECT" \
  --environment-variables-override name=ONLY,value=esmfold,type=PLAINTEXT

aws logs tail "/aws/codebuild/$WEIGHTS_PROJECT" --follow
```

When it finishes, there should be about 12.5 GB under `weights/` and a manifest
beside it:

```sh
aws s3 ls --region "$REGION" --recursive --human-readable --summarize \
  "s3://$BUCKET/weights/" | tail -5

aws s3 cp --region "$REGION" "s3://$BUCKET/weights/manifest.json" - \
  | python3 -c "import json,sys; m=json.load(sys.stdin)['models']; \
      [print(f\"{k:<14}{v['bytes']/1e9:6.1f} GB  {len(v['files'])} file(s)\") for k,v in m.items()]"
```

**Nothing in the account can delete these.** There is a `Deny` on
`s3:DeleteObject` for `weights/*` in the bucket policy, because they came from
four different places and publishing them again is an afternoon. Re-publishing
still works — that is a `PutObject`.

> Both builds read the context `cdk deploy` uploaded. If you would rather run
> either on your own machine, both still work there — see the header comments
> in [`build.sh`](worker/build.sh) and
> [`publish-weights.py`](worker/publish-weights.py).

## 6. Point the website at it

Written straight from the outputs, so there is nothing to mistype:

```sh
cat > web/config.json <<EOF
{
  "api": "$API_URL",
  "auth": {
    "domain": "$LOGIN_DOMAIN",
    "clientId": "$CLIENT_ID",
    "redirect": "$APP_URL"
  }
}
EOF

python3 -m json.tool web/config.json      # and read it back
```

The stack also prints the same thing as one line, if you would rather copy it:

```sh
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ConfigJson'].OutputValue" --output text
```

None of those is a secret. A Cognito user pool client id is a public
identifier; the client is created without a secret precisely because a static
page cannot keep one. `tools/push-adumbra.sh` checks this file for anything
that looks like a credential and refuses to transfer if it finds one.

> `web/config.json` is gitignored **in this repository** — it belongs to one
> deployment rather than to the code. It is not gitignored in the adumbra
> repository, and it should not be: the site needs it.

```sh
tools/push-adumbra.sh
cd ../adumbra
npm run dev                  # look at it at /proteincad/
git add -A public/proteincad && git commit -m "proteinCAD: hosted API" && git push
```

### Or all of step 6 at once

```sh
deploy/aws/go-live.sh
```

Reads the stack, warns if the image or the weights are missing, writes
`web/config.json` from the outputs, runs the transfer checks and copies the
folder across. It refuses to run at all if the stack is not deployed, because
the order is the thing that goes wrong: **`config.json` has to be written
before the folder is copied.** A folder pushed without it gives a live page
with no Account section, no GPU panel and a Design tab reporting an outage —
which looks like a broken deployment and is really a missing file.

It stops short of the commit. Publishing is yours to do.

## 6b. Testing from localhost, if you want to

There are two different things you might mean by this, and only one of them
needs a deploy.

### Seeing it work, without AWS at all

```sh
python3 tools/preview_hosted.py
```

The real Lambda code and the real worker, against the stand-in AWS services in
`tools/fake_aws.py`. It writes a throwaway `web/config.json`, starts the viewer,
and puts your own config back on Ctrl-C. Everything is clickable: the Account
buttons, the GPU machine booting, models downloading with the megabytes
counting up, Run greying out until they are there.

It is worth knowing why you need it. `python3 -m proteincad` on its own shows
**no Account section at all** — not a broken one, none. Locally there are no
accounts and no API elsewhere, so a Sign in button would be offering something
that does not exist. The section appears only when `config.json` carries an
`auth` block.

What it cannot do is complete a sign-up: that needs a real user pool. Pressing
**Create account** goes to Cognito's `/signup` page and bounces off its error
page, because the client id in the throwaway config is invented.

### Signing up for real, from a local viewer

This is the narrower case: pointing a **local** viewer at the **deployed** API,
signing in for real, queueing a real job, watching a real machine start.

It is off by default, because an origin on this list can make authenticated
cross-origin requests with a token it obtained.

```sh
cd deploy/aws/cdk
npx aws-cdk deploy -c proteincad:devOrigins=http://localhost:8321
```

That one value is added in three places at once — the API's CORS, the bucket's
CORS (or a presigned result link will not load) and Cognito's callback and
logout URLs. Several are comma-separated:

```sh
npx aws-cdk deploy -c proteincad:devOrigins=http://localhost:8321,http://localhost:5173
```

Then point a local `config.json` at the deployed API, with the **local** URL as
the redirect:

```sh
cat > web/config.json <<EOF
{
  "api": "$API_URL",
  "auth": {
    "domain": "$LOGIN_DOMAIN",
    "clientId": "$CLIENT_ID",
    "redirect": "http://localhost:8321/"
  }
}
EOF

PROTEINCAD_PORT=8321 python3 -m proteincad
```

Then open **`http://localhost:8321/`** — not `127.0.0.1`. Cognito permits a
plain-HTTP callback for the hostname `localhost` and for nothing else; the two
are the same machine and different strings. The stack refuses a `127.0.0.1` dev
origin at synth time rather than letting you find out twelve minutes into a
deploy.

Take it out again when you are done:

```sh
npx aws-cdk deploy -c proteincad:devOrigins=
rm web/config.json     # or put the production one back
```

## 7. Check it from a clean browser

Open a private window and go to `https://adumbra.burakayy.com/proteincad/`.

| | what should happen |
|---|---|
| the page loads | structures, styles, selection, export all work **signed out** |
| Account | two buttons: **Create account** and **Sign in** |
| press **Create account** | Cognito's registration form, a code by email, and back where you were with your structures still loaded |
| press **Sign in** afterwards | straight back in |
| GPU machine | state `gone`, **Start GPU machine** offered. No stop and no terminate anywhere; once one is running, **Retire** asks it to stand down after the work in hand |
| Run design | **disabled**, saying "Start the GPU machine, then download RFdiffusion." |
| press Start | `launching` → `booting` (with the stage it is on) → `ready`, four or five minutes |
| press Download on RFdiffusion | a byte counter that moves, then `✓ ready` |
| Run design | now enabled |
| pick a site, Run | queued, then running |
| the job finishes | Load puts the design in the scene, on target |
| the sequence button | disabled until ProteinMPNN and ESMFold are downloaded, and says so |

Then two things that matter more than any of that.

**Submitting without pressing anything.** Sign in on a second machine while the
first is idle, pick a site and press Run straight away. The job should be
accepted, a machine should start for it, and the job's stage should read
`fetching the models this job needs`. The buttons are a convenience, not a
requirement.

**The machine going away.** Note the time, walk away for thirty-five minutes,
and then:

```sh
aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:proteincad:role,Values=worker \
            Name=instance-state-name,Values=pending,running,stopping \
  --query 'Reservations[].Instances[].InstanceId' --output text
```

It should print nothing. **Do not leave this step until you have seen that
once.** It is the difference between a dollar a month and four hundred.

## 8. Tear down the old app box

Skip this if you never built [PUBLIC-DEPLOY.md](PUBLIC-DEPLOY.md). If you did,
do all five: that box runs `server.py`, which has no authentication and allows
every origin, and leaving it up keeps an open door behind the locked one.

1. **Delete the Pages Function** `functions/proteincad/api/[[path]].js` from the
   adumbra checkout, commit and push. The viewer now calls API Gateway directly.
2. **Remove the Pages variables** `PROTEINCAD_ORIGIN` and `PROTEINCAD_EDGE_KEY`,
   in Production and Preview both.
3. **Terminate the `t4g.small`.** Check the id twice.
4. **Release the Elastic IP** — an unattached one bills about $3.60/month for
   doing nothing. `aws ec2 release-address --allocation-id eipalloc-…`
5. **Delete the DNS record** for `proteincad-api.…`, and the old
   `proteincad-web` and `proteincad-gpu` security groups.

If you also built the older stopped-instance GPU box from
[README.md](README.md), terminate that too and delete its 200 GB volume. It is
$16 a month of nothing.

---

## When something is wrong

| what you see | where to look |
|---|---|
| "AWS has no g4dn.xlarge to spare…" | real, and usually clears in minutes. Press Start again. If it never clears, check the GPU quota above. |
| the machine sits on `booting` forever | the boot script failed — get a shell (below) and read `/var/log/proteincad-boot.log` |
| a build fails | `aws logs tail "/aws/codebuild/$IMAGE_PROJECT" --since 2h`, or `$WEIGHTS_PROJECT` |
| the machine will not start, "no pinned image in SSM" | step 4 has not run, or it failed. `deploy/aws/worker/build.sh image` |
| every launch fails immediately | the vCPU quota, or `RunInstances` being refused — check the submit Lambda's logs |
| a model fails its checksum | the copy in the bucket is damaged; re-run `publish-weights.py`, which will skip the good ones |
| "The proteinCAD API is not answering" | `aws logs tail /aws/lambda/proteincad-Api… --follow` |
| every call is 401 | signed out, or the token expired. The panel says so. |
| sign-in loops back signed out | `redirect` in config.json must match a Cognito callback URL **exactly**, trailing slash and all |
| CORS errors in the console | `proteincad:origin` in cdk.json is not the origin the page is served from |
| jobs queue and nothing runs | is there a machine? Is the worker up? `journalctl -u proteincad-sqs` |
| "that download link has expired" | presigned links last five minutes. Refresh the job list. |
| jobs vanish | the dead letter queue — a message that killed the worker twice |
| **a machine that never goes away** | the worker is not running, or it cannot read the queue depth. This is the only thing that stops it, so look now. |

## Getting a shell on a running machine

The GPU machine has no inbound rules — no port 22, no key pair, nothing. Session
Manager gets you in anyway: the instance role carries
`AmazonSSMManagedInstanceCore`, and the boot script makes sure the agent is
running before it does anything that might fail.

```sh
BOX=$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:proteincad:role,Values=worker \
            Name=instance-state-name,Values=pending,running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)

aws ssm start-session --target "$BOX"
```

This works because the instance role carries the AWS-managed policy
`AmazonSSMManagedInstanceCore`, and for no other reason — there is no key pair,
no bastion and no inbound rule anywhere in the stack. To satisfy yourself:

```sh
aws iam list-attached-role-policies \
  --role-name "$(aws cloudformation describe-stack-resource --region "$REGION" \
      --stack-name "$STACK" --logical-resource-id WorkerRole \
      --query 'StackResourceDetail.PhysicalResourceId' --output text)" \
  --query 'AttachedPolicies[].PolicyName' --output text
```

(If that fails with "not connected", give the agent a minute after boot. If it
keeps failing, install the Session Manager plugin for the AWS CLI.)

Once you are in:

```sh
sudo cat /var/log/proteincad-boot.log     # the first ninety seconds
journalctl -u proteincad-sqs -f           # the worker
docker ps                                  # is a job running
ls -la /mnt/fast/weights                    # which models have arrived
df -h /mnt/fast                             # the instance store
nvidia-smi                                  # the card
```

Remember it is a machine that does not exist most of the time: there is nothing
to get a shell on unless somebody has started one, and it will shut itself down
under you after thirty idle minutes. Your session going dead is that working.

Useful things to have to hand:

```sh
# is there a machine, and what does it think it is doing?
aws dynamodb query --region "$REGION" --table-name "$TABLE" \
  --key-condition-expression 'pk = :p' \
  --expression-attribute-values '{":p":{"S":"MACHINE"}}' --output json

# how deep is the queue
aws sqs get-queue-attributes --region "$REGION" --queue-url "$QUEUE" \
  --attribute-names ApproximateNumberOfMessagesVisible \
                    ApproximateNumberOfMessagesNotVisible

# anything that killed the worker twice
aws sqs receive-message --region "$REGION" --queue-url "$DLQ" --max-number-of-messages 10

# what the machines are pinned to
aws ssm get-parameter --region "$REGION" --name "$IMAGE_PARAM" \
  --query Parameter.Value --output text

# the Lambdas, by their CloudFormation-generated names
for fn in Api Submit Waker; do
  name=$(aws cloudformation describe-stack-resource --region "$REGION" \
    --stack-name "$STACK" --logical-resource-id "$fn" \
    --query 'StackResourceDetail.PhysicalResourceId' --output text)
  echo "$fn -> /aws/lambda/$name"
done
```

## Changing the limits

Context values, so changing one is a re-deploy and nothing else:

```sh
cd deploy/aws/cdk
npx aws-cdk deploy \
  -c proteincad:machineStarts=3 \
  -c proteincad:globalDailyLaunches=10 \
  -c proteincad:idleMinutes=20
```

The five that bound the bill:

| | default | |
|---|---|---|
| `dailyJobs` | 20 | jobs per user per UTC day |
| `concurrentJobs` | 2 | queued or running, per user |
| `globalDailyJobs` | 100 | jobs by everyone together |
| `machineStarts` | 5 | machine starts per user per day |
| `globalDailyLaunches` | 20 | launches by everyone together |

Plus `idleMinutes`, which is the one that matters most. Thirty is a guess at
"long enough that setting up a second design does not pay for a second boot and
a second download". If people mostly send one job and walk away, fifteen is
cheaper; if they work in long sittings, forty-five wastes less of their time.

## The limits, and where to change them

With sign-up open, these are the only thing standing between a public form and
a bill. Every one is a context value in
[`deploy/aws/cdk/cdk.json`](cdk/cdk.json), overridable per deploy with `-c`.

| context value | now | what it bounds |
|---|---|---|
| `proteincad:dailyJobs` | **20** | jobs one person may run per UTC day |
| `proteincad:concurrentJobs` | **2** | jobs one person may have queued or running at once |
| `proteincad:machineStarts` | **5** | times one person may start the GPU machine per day |
| `proteincad:globalDailyJobs` | **100** | jobs by everyone together, per day |
| `proteincad:globalDailyLaunches` | **20** | machine starts by everyone together, per day |
| `proteincad:idleMinutes` | **30** | idle minutes before the machine shuts itself down |
| `proteincad:maxDesigns` | **8** | designs in a single job |
| `proteincad:budgetUsd` | **120** | the monthly figure the alert fires against |
| `proteincad:budgetEmail` | *empty* | **who gets told.** No email, no alert |

Change one and deploy; nothing else moves.

```sh
cd deploy/aws/cdk
npx aws-cdk deploy \
  -c proteincad:globalDailyJobs=30 \
  -c proteincad:budgetEmail=you@example.com
```

Or edit `cdk.json` if you would rather they were written down.

### The number worth doing arithmetic on

**`globalDailyJobs` is the one that binds, and at 100 it does not bind at all.**

A design job occupies the GPU for roughly fifteen minutes. A hundred of them is
twenty-five hours of work, which is more than a day has — so at the default the
queue would never empty, the idle timer would never fire, and the machine would
simply stay up:

```
24 h/day × $0.54/h  ≈  $13/day  ≈  $390/month
```

That is the worst case with sign-up open, and it is reached by a dozen
enthusiastic strangers rather than by anything going wrong. To make the cap
actually cap something, pick the daily spend you are willing to see and divide:

| `globalDailyJobs` | GPU hours/day | roughly |
|---|---|---|
| 100 *(now)* | capped only by the clock | **$390 / month** |
| 40 | 10 | $160 / month |
| **30** | 7.5 | **$120 / month** |
| 15 | 3.75 | $60 / month |

Thirty is the value that matches the $120 budget alert, which is otherwise
telling you about a month you can no longer change. The per-user caps are not
the protection here — twenty jobs each is generous, and the whole point of open
sign-up is that the number of users is not something you control.

## What has and has not been tried

The Lambda code, the launch lock, the zone fallback, the quota arithmetic, the
weight download and its checksum, the retry policy and the idle shutdown are all
exercised by `python3 tools/check_cloud.py` against stand-in AWS services. The
whole hosted path — sign in, start a machine, watch it boot, download two models
in parallel, submit, fetch the result, and the capacity-failure message — has
been driven in a browser against those same stand-ins.

Not tried: `cdk deploy` against a real account, either CodeBuild, and a real
model run on a real card. The template synthesizes and
`python3 tools/check_stack.py` asserts its security properties — including that
the image is built for `linux/amd64` and that the machine can be reached with
Session Manager — but nothing here has met AWS.

The two most likely places for the first real run to stumble, so you know where
to look: the **image build**, where `colab_worker.py setup` resolves torch and
DGL against an index that has been unreliable before (the build fails on an
explicit import check rather than at the first job, which is the point of that
check), and the **first fold**, where ESMFold is loaded from a directory rather
than a Hugging Face repo id for the first time.
