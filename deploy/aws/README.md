# Running the GPU on EC2 instead of Colab

> **Read this first if you are coming back to it cold.** Everything needed is
> in this one file. The short version is below; the steps start at
> [Before anything else](#before-anything-else-the-gpu-quota).
>
> This file covers the **GPU box**, driven by proteinCAD running on your own
> machine: you start the app, the app starts the instance. That is the right
> shape for one person.
>
> To put it on a public website instead, there are two other documents and you
> want the second one:
>
> - [SERVERLESS-DEPLOY.md](SERVERLESS-DEPLOY.md) — **the one to deploy.**
>   Cognito sign-in, a job queue, per-user quotas, results through short-lived
>   links, and a GPU machine that is created for a job and destroyed after it,
>   all described in [cdk/](cdk/) as code. About **$1/month** when nobody is
>   using it.
> - [PUBLIC-DEPLOY.md](PUBLIC-DEPLOY.md) — **superseded.** An always-on box
>   running `server.py` behind Caddy, with no authentication at all. Kept for
>   its reasoning and for the teardown.
>
> The serverless path replaces the app-side pieces described here: a Lambda
> creates the machine from a launch template rather than `ec2.py` starting a
> stopped one, the weights come from S3 instead of a disk, and the worker is
> [worker/](worker/) rather than `colab_worker.py` serving HTTP. The GPU quota
> section below still applies to both.

## The short version

**What changed in the app.** `ec2` is a third runner beside `mock` and
`remote`. Colab is untouched — both can be configured at once, and the `on`
menu beside **Run design** picks between them. The worker on the instance is
the same `proteincad/colab_worker.py`; only *who turns the machine on* is
different. Everything else is identical: the spec, the polling, all six
RFdiffusion protocols, the 44 settings, the sequence-and-fold stage. Pressing
**sequence** on a design uses the same runner the backbone was made with, so
stage two wakes the box the same way.

**What it cost to build:** three new pieces and nothing else moved.

| | |
|---|---|
| `proteincad/ec2.py` | the only file that has ever heard of AWS. Starts the instance, waits for the worker, hands out leases, stops it when idle |
| `Ec2Runner` in `design.py` | composes the existing `HttpRunner` rather than duplicating it. A fresh one per run, because a stopped instance comes back with a **new public IP every time** — which is why you do not need an Elastic IP |
| the **GPU** section in the Design panel | state, a countdown to the stop, a Start button for warming it up while a design is still being set up |

**The seven things to do on AWS**, each expanded below:

1. **Request the GPU quota first.** A new account has **0 vCPUs** for G
   instances and the launch error does not say so. Human-reviewed, can take a
   day. → [quota](#before-anything-else-the-gpu-quota)
2. **Launch one instance.** Deep Learning *Base* OSS Nvidia Driver GPU AMI
   (Ubuntu 22.04), `g4dn.xlarge`, 150 GB gp3, public IP on. → [step 1](#step-1--launch-the-instance)
3. ⚠️ **Confirm shutdown behaviour is `Stop`, not `Terminate`.** The box powers
   itself off to save money; on `terminate` that destroys 25 GB of installed
   models. It is the default, and proteinCAD warns at the first wake — check it
   anyway. → [step 1](#step-1--launch-the-instance)
4. **Security group:** 22 from your IP, **8000 from whatever runs proteinCAD
   only** — never `0.0.0.0/0`. → [the security group](#the-security-group)
5. **`sudo ./setup.sh`** on the box. Installs everything plus two systemd
   units, prints the token. About an hour. Use `--skip-models` first if you
   want to prove the wiring in two minutes. → [step 2](#step-2--install-the-worker-on-it)
6. **IAM:** edit `iam-policy.json` (three placeholders) and attach it. It names
   your one instance in its ARN, which is what makes the credentials safe to
   put on a web server. → [step 3](#step-3--credentials-for-the-app)
7. **Point the app at it**, stop the instance, press Run. → [step 4](#step-4--point-proteincad-at-it)

**What it costs.** Roughly $0.53/hour while designing, about $12/month for the
disk, and **$0 the rest of the time** — which is the entire point.

**Cost safety is two watchdogs**, because the failure that costs money is the
app not being there to turn the GPU off:

| | stops it after | survives |
|---|---|---|
| in the app (`PROTEINCAD_EC2_IDLE`) | 15 min idle | — |
| on the box (systemd timer) | 30 min idle | the app crashing, the laptop closing, the network going |

The second is the one that matters. The worker's `/health` now reports `busy`
and `idle`; a timer reads them and calls `poweroff`. `/health` deliberately
does **not** reset the idle clock — the watchdog is itself a caller of
`/health`, and that mistake would mean the box never stops. It also will not
fire while somebody is logged in over ssh.

**What was actually verified.** The Python and the lifecycle were tested end to
end against a stand-in EC2 API with the real worker behind it: one
`StartInstances` when a job needed it, a real design returned, one
`StopInstances` when the idle window expired. 342 Python + 104 node checks
pass, 56 of them new and covering the lifecycle, the error messages and the
idle logic. **`setup.sh` has not been run against a real instance** — it is
written, not proven. Run it with `--skip-models` first and see what it says.

---

proteinCAD does not care where the GPU is. The worker is the same
`colab_worker.py` file, speaking the same four-endpoint contract, whether it
runs in a Colab notebook or on an EC2 box. What changes here is **who turns the
machine on**: on Colab you do, and it disappears when the session ends; on EC2
the app does, when a job needs it, and turns it off again when nothing does.

```
browser ──▶ proteinCAD server ──▶ EC2 instance ──▶ RFdiffusion
            (your laptop or       (stopped until       ProteinMPNN
             your website)         a job needs it)     ESMFold
```

Nothing is hardcoded. Every setting below is an environment variable, so the
same checkout runs against Colab, against EC2, or against neither.

---

## What this costs

A stopped instance bills only for its disk. That is the whole point of the
design: **the GPU is off except while it is designing.**

| | roughly |
|---|---|
| `g4dn.xlarge` (T4, 16 GB) while running | $0.53 / hour |
| `g5.xlarge` (A10G, 24 GB) while running | $1.01 / hour |
| 150 GB gp3 root volume, always | $12 / month |
| stopped instance, no disk | $0 |

Check current prices for your region — these move, and they differ between
regions by a lot. The disk is the standing cost you cannot avoid: the models,
the weights and the Python environment add up to about 25 GB, and a machine
that had to reinstall them on every start would take an hour to wake instead of
a minute.

With a 15-minute idle timer, an afternoon of designing costs an afternoon of
GPU. Leaving the tab open overnight costs nothing, because nothing asked for
anything and the box stopped itself.

---

## Before anything else: the GPU quota

A new AWS account has a **quota of 0 vCPUs for G-type instances**, and the
error you get when you launch one without asking first does not say so
clearly. Request it now, because it is reviewed by a human and can take a day.

Service Quotas → Amazon EC2 → **Running On-Demand G and VT instances** →
request 4 vCPUs (a `g4dn.xlarge` or `g5.xlarge` is 4).

---

## Step 1 — launch the instance

Launch it once, by hand, in the region you want to work in.

| setting | value | why |
|---|---|---|
| AMI | **Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)** | the NVIDIA driver and CUDA are already on it, and Ubuntu 22.04 is Python 3.10 — which has far more DGL builds available than 3.13 does |
| Instance type | `g4dn.xlarge` to start, `g5.xlarge` if ESMFold runs out of memory | 16 GB of card is enough for most designs; 24 GB is enough for the large ones |
| Key pair | one you have | you need ssh for the install and for anything that goes wrong |
| Storage | **150 GB gp3** | about 25 GB of models plus room for job output |
| Auto-assign public IP | **enabled** | unless proteinCAD will run inside the same VPC, see *Addressing* below |
| Shutdown behaviour | **Stop** (the default) | ⚠️ see below |

⚠️ **Instance-initiated shutdown behaviour must be `Stop`, not `Terminate`.**
The watchdog on the box powers the machine off to save money. If the instance
is set to terminate on shutdown, that destroys it and the 25 GB you just spent
an hour installing. `Stop` is the default, so this is only a risk if you launch
from a template that changed it. proteinCAD checks at the first wake and puts a
warning in the panel, but check it yourself too:

```bash
aws ec2 describe-instance-attribute --instance-id i-0abc... \
    --attribute instanceInitiatedShutdownBehavior
# if it says "terminate":
aws ec2 modify-instance-attribute --instance-id i-0abc... \
    --instance-initiated-shutdown-behavior stop
```

### The security group

Two inbound rules, and no more:

| port | source | for |
|---|---|---|
| 22 | your IP | ssh, for the install |
| 8000 | **the IP of whatever runs proteinCAD** | the worker |

Do not open 8000 to `0.0.0.0/0`. The bearer token is the only other thing
standing between the internet and free use of your GPU, and it travels in
plaintext over that port. If proteinCAD runs on your laptop, this is your home
IP and you will have to update it when it changes; if it runs on your website's
server, it is that server's address and it does not change.

Better, if proteinCAD is going to live on a server anyway: put both in the same
VPC, allow 8000 from the app server's *security group* rather than an IP, and
set `PROTEINCAD_EC2_ADDRESS=private`. Then the traffic never leaves the VPC.

---

## Step 2 — install the worker on it

ssh in, copy this directory and `proteincad/colab_worker.py` across, and run:

```bash
sudo ./setup.sh
```

It installs a `proteincad` user, a Python environment, RFdiffusion, ProteinMPNN
and ESMFold, and two systemd units:

- **`proteincad-worker.service`** — the worker, enabled, so it comes back by
  itself every time the instance starts. This is what makes waking a boot
  rather than a chore.
- **`proteincad-idle.timer`** — the watchdog, checking every minute whether
  anything still needs the machine.

It downloads about 12 GB of weights and takes the best part of an hour. It is
idempotent — run it again after an interruption and it picks up what is
missing. To check the wiring before paying for the long part:

```bash
sudo ./setup.sh --skip-models      # services only, ~2 minutes
```

then set `PROTEINCAD_WORKER_ARGS=--generator echo` in
`/etc/proteincad/worker.env`, restart, and prove the app can reach the box
before installing anything heavy.

`setup.sh` prints the worker token at the end. You need it in step 4. It is
also in `/etc/proteincad/worker.env`.

### Settings on the box

Everything is in `/etc/proteincad/worker.env` — the port, the token, the
interpreter, and the two watchdog timings. Edit it and
`sudo systemctl restart proteincad-worker`. Nothing is baked into the units.

---

## Step 3 — credentials for the app

proteinCAD needs to be allowed to start and stop that one instance.

Edit `iam-policy.json`, replacing `REGION`, `ACCOUNT_ID` and `INSTANCE_ID`, then
attach it to:

- **an IAM role**, if proteinCAD will run on an EC2 instance or in ECS. Nothing
  else to do — boto3 picks the role up by itself and there is no key to leak.
- **an IAM user** with an access key, otherwise. Then either `aws configure`, or
  `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the app's environment.

The policy names your instance in its resource ARN, so these credentials can
start and stop that box and nothing else. That is what makes them safe to put
on a web server.

---

## Step 4 — point proteinCAD at it

```bash
pip install 'proteincad[aws]'          # adds boto3; nothing else needs it

export PROTEINCAD_EC2_INSTANCE=i-0abc123def456
export PROTEINCAD_EC2_REGION=us-east-1
export PROTEINCAD_EC2_TOKEN=<the token setup.sh printed>
python3 -m proteincad
```

Or as flags: `python3 -m proteincad --ec2-instance i-0abc... --ec2-region us-east-1`
(the token has to come from the environment; a secret on a command line is
visible to every process on the machine).

A **GPU** section appears in the Design panel showing the instance's state, and
`ec2` appears in the `on` menu beside Run and becomes the default. That is all
that changes. Everything else — protocols, contigs, the Advanced settings, the
sequence and fold stage — works exactly as it did on Colab.

### Every setting

| variable | default | |
|---|---|---|
| `PROTEINCAD_EC2_INSTANCE` | — | the instance id. Setting it is what turns this on |
| `PROTEINCAD_EC2_REGION` | `AWS_DEFAULT_REGION` | an instance id is only unique within its region |
| `PROTEINCAD_EC2_PROFILE` | default chain | a named profile from `~/.aws/credentials` |
| `PROTEINCAD_EC2_PORT` | `8000` | must match `PROTEINCAD_WORKER_PORT` on the box |
| `PROTEINCAD_EC2_TOKEN` | `PROTEINCAD_COMPUTE_TOKEN` | must match `PROTEINCAD_WORKER_TOKEN` on the box |
| `PROTEINCAD_EC2_ADDRESS` | `public` | `public`, `private`, or a hostname you own |
| `PROTEINCAD_EC2_SCHEME` | `http` | `https` if you put a certificate in front of the worker |
| `PROTEINCAD_EC2_IDLE` | `15` | minutes with no job before the app stops it. `0` leaves it to the box |
| `PROTEINCAD_EC2_BOOT` | `600` | seconds to wait for a starting instance before giving up |

### Addressing

A stopped instance gets a **new public IP every time it starts**, so proteinCAD
reads the address from EC2 at each wake rather than being told one. That is why
you do not need an Elastic IP — and why you should not pay for one.

Set `PROTEINCAD_EC2_ADDRESS` to `private` if the app runs in the same VPC, or to
a hostname if you have put a name and a certificate in front of the worker.

---

## Step 5 — stop it, and watch it wake

```bash
aws ec2 stop-instances --instance-ids i-0abc...
```

Then press **Run design** in the app. The job's stage line walks through it:

```
starting the GPU instance (g4dn.xlarge)
the GPU instance is booting
waiting for the worker to answer
GPU ready at http://54.x.x.x:8000
running RFdiffusion, design 1 of 4
```

About 90 seconds of that is the boot. Every job after it starts immediately,
until the box goes idle and stops itself. Press **Start** in the GPU section
while you are still setting a design up and even the first one is instant.

---

## The two watchdogs

One is not enough, because the failure that costs money is the app not being
there to turn the GPU off.

**In the app** (`PROTEINCAD_EC2_IDLE`, 15 minutes). Stops the instance once no
job has needed it for that long. It will not stop one with a job on it, and
before stopping it asks the worker whether it is busy — so a run started by a
previous run of the app is not thrown away.

**On the box** (`PROTEINCAD_IDLE_LIMIT` in `worker.env`, 30 minutes). A systemd
timer asks the worker every minute how long since anyone wanted anything, and
powers the machine off when the answer gets too big. This one still works when
proteinCAD has crashed, the laptop has closed, or the network has gone — which
is exactly when a GPU quietly bills for a weekend.

It will not act while somebody is logged in over ssh, so debugging does not get
the rug pulled out from under it. To suspend it anyway:

```bash
sudo systemctl stop proteincad-idle.timer     # comes back on next boot
```

---

## When it does not work

| what you see | what it means |
|---|---|
| `no AWS credentials` | boto3 found nothing. `aws configure`, or set the two `AWS_*` variables, or attach a role |
| `there is no instance i-… in us-east-1` | right id, wrong region — an instance id is only unique within one |
| `not allowed to start i-…` | the policy is missing, or its ARN names a different instance |
| `running but nothing answered on …/health` | the security group does not allow your IP on 8000, the port does not match, or the worker is down. `systemctl status proteincad-worker` |
| `running but has no public address` | the subnet does not auto-assign public IPv4. Enable it, add an Elastic IP, or use `PROTEINCAD_EC2_ADDRESS=private` |
| `is set to TERMINATE on shutdown` | fix it *now* — see step 1 |
| `starting an EC2 instance needs boto3` | `pip install 'proteincad[aws]'` |
| box never stops | check `systemctl list-timers proteincad-idle.timer` and `journalctl -t proteincad-idle` |
| jobs fail with CUDA errors | `sudo -u proteincad /opt/proteincad/env/bin/python /opt/proteincad/colab_worker.py setup` re-checks the environment and says what is wrong |

Logs on the box:

```bash
journalctl -u proteincad-worker -f     # the worker, and every model command it runs
journalctl -t proteincad-idle          # every decision the watchdog made
```

---

## Colab is still there

None of this removes the Colab path. `PROTEINCAD_COMPUTE_URL` still points at a
tunnel, `remote` still appears in the `on` menu, and both can be configured at
once — pick between them in the panel. The notebook in `notebooks/` is
unchanged, and a worker installed by `setup.sh` is the same program as the one
that notebook runs.
