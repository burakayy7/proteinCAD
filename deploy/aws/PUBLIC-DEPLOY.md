# Putting it on the internet: the network side

> # ⚠ Superseded
>
> **Do not build this.** It is kept because the reasoning in it is still worth
> reading, and because somebody who built it earlier needs to know what to tear
> down. The configuration files it referred to — `app-box/Caddyfile`,
> `app-box/api-proxy.js` and the rest — have been deleted.
>
> This path put an always-on `t4g.small` in front of the GPU box running
> `server.py` behind Caddy, with a shared key between Cloudflare and the box.
> Two things are wrong with it for a deployment other people use:
>
> - **`server.py` has no authentication of any kind** and sends
>   `Access-Control-Allow-Origin: *`. The shared key gates the network path,
>   not the person holding it, and there is no per-user limit — so one
>   enthusiastic visitor is an unbounded GPU bill.
> - **It costs about $29/month standing**, whether or not anybody uses it: an
>   always-on box, plus a stopped GPU instance holding a 200 GB disk.
>
> Use **[SERVERLESS-DEPLOY.md](SERVERLESS-DEPLOY.md)** instead: Cognito
> sign-in, per-user and global limits on both jobs and machine starts, a queue,
> results through short-lived links — and **about $1/month standing**, because
> nothing exists while nobody is designing. If you already built this path,
> step 8 of that document tears it down.

How to get from "the viewer is a static page on my website" to "anyone I let in
can open it from any computer and run a real design job", over HTTPS, without
leaving a GPU billing all night.

Read [README.md](README.md) first — that is the GPU box itself. This is the
network and the always-on half in front of it.

---

## Should I tick "Allow HTTPS traffic from the internet"?

It depends which box, and the answer is different for each.

| box | HTTPS (443) | HTTP (80) | SSH (22) |
|---|---|---|---|
| **app box** — runs proteinCAD, always on | **yes** | **yes** | your IP only |
| **GPU box** — runs the model, mostly off | **no** | **no** | your IP only |

**App box, yes.** It terminates TLS for the API, so 443 has to be reachable.
Tick HTTP too: Let's Encrypt's challenge arrives on port 80, and you want the
redirect from it.

**GPU box, no.** Nothing on it listens on 443 or 80, so the rule would allow
traffic to a closed port — clutter that reads like an intention. The only thing
that should reach the GPU box is the app box, on port 8000, from inside the VPC.

A rule you don't need is not harmless. It is the one somebody points at in six
months and asks what it was for.

---

## The gap before any of this works

`adumbra.example.com/proteincad/` is a **static** folder on Cloudflare Pages.
There is no server behind it. The viewer calls `api/health`, `api/design` and
the rest as relative URLs, and on a static host those are 404s — which is why
the Design tab currently says nothing is answering.

No security group fixes that. Something has to run `python3 -m proteincad`
somewhere the internet can reach, all the time, because the browser needs it the
moment the page loads.

It cannot be the GPU box. That would mean leaving a GPU on around the clock —
about $380 a month to serve a few kilobytes of JSON. So: a small box that is
always on, and a GPU box it starts when there is work.

```
   browser, anywhere
        │  https://adumbra.example.com/proteincad/
        ▼
   Cloudflare Pages          static viewer, and one function
        │                    that forwards /proteincad/api/* onward
        │  https + shared key
        ▼
   app box   t4g.small, always on          Caddy → python3 -m proteincad
        │
        │  private VPC address, port 8000, never touches the internet
        ▼
   GPU box   g4dn.xlarge, stopped until a job needs it
```

### What it costs to leave running

| | per month |
|---|---|
| app box, `t4g.small` | ~$12 |
| its 20 GB disk and public IPv4 | ~$5 |
| GPU box's 150 GB disk (stopped) | ~$12 |
| GPU compute | $0.53/hour, only while designing |

About **$29 a month standing**, plus what you actually use. Prices move and
differ by region; check yours.

> Cheaper for a demo you attend in person: skip the app box, run proteinCAD on
> your laptop and expose it with `cloudflared tunnel`. It works, it is free, and
> it stops the moment you shut the lid — which is why it is not the answer to
> "from any computer".

---

## The part that matters more than HTTPS

**proteinCAD's app server has no authentication.** Not a weak one — none.
`POST /api/design` and `POST /api/gpu/start` are open to whoever can reach the
port. HTTPS encrypts the pipe; it says nothing about who is allowed in it.

So the design below has two separate gates, and you want both:

1. **A shared key between Cloudflare and the app box.** Stops anyone reaching
   the API except through your site. Hostnames are not secret — certificate
   transparency logs publish every one you issue a certificate for, and they
   get scanned within minutes.
2. **Cloudflare Access in front of `/proteincad/`.** Decides *who* may use it.
   Free for up to 50 people, email one-time-codes, no passwords to manage.

Without the second one, "any computer" includes everybody else's.

---

## Step 1 — two security groups, before any instance

Make these first. The launch wizard lets you pick an existing group, which is
easier to get right than editing one afterwards.

**EC2 → Security Groups → Create security group.**

### `proteincad-web`

| type | port | source | why |
|---|---|---|---|
| HTTPS | 443 | `0.0.0.0/0` | the API |
| HTTP | 80 | `0.0.0.0/0` | Let's Encrypt's challenge, and the redirect |
| SSH | 22 | **My IP** | you, setting it up |

### `proteincad-gpu`

| type | port | source | why |
|---|---|---|---|
| Custom TCP | 8000 | **`proteincad-web`** | the only thing allowed to use the GPU |
| SSH | 22 | **My IP** | installing the models |

That second source is the important one. In the Source box, choose *Custom* and
start typing `proteincad-web` — you are picking the **security group**, not an
IP. Anything in that group can reach port 8000; nothing else can, ever, no
matter what address it comes from. It keeps working when the app box restarts
and gets a different private IP.

Leave outbound alone. Both boxes need to reach the internet to install things,
and the GPU box downloads about 12 GB of model weights.

---

## Step 2 — launch the app box

**EC2 → Launch instance.**

| field | value |
|---|---|
| Name | `proteincad-app` |
| AMI | Ubuntu Server 24.04 LTS, **64-bit (Arm)** |
| Instance type | `t4g.small` |
| Key pair | yours |
| VPC | the **default** one — the same VPC as the GPU box |
| Subnet | any public one |
| Auto-assign public IP | **Enable** |
| Firewall | **Select existing security group → `proteincad-web`** |
| Storage | 20 GB gp3 |

Picking an existing security group is also where the "Allow HTTPS traffic from
the internet" checkbox goes away — it only appears when the wizard is creating a
group for you. If you do let it create one, tick **both** HTTPS and HTTP.

Arm is deliberate: the app server is standard-library Python plus boto3, so
Graviton runs it at about two-thirds the price.

---

## Step 3 — launch the GPU box

Everything in [README.md](README.md) still applies — the AMI, `g4dn.xlarge`,
150 GB, and above all **shutdown behaviour `Stop`, not `Terminate`**. Two
differences here:

- **Firewall: select existing → `proteincad-gpu`.** Do not tick Allow HTTP or
  Allow HTTPS.
- **Same VPC as the app box**, or the private address will not route and the
  security-group rule will not apply.

Leave **auto-assign public IP enabled**. That sounds like it contradicts the
whole section, and it doesn't: a public IP lets the box reach *out* to download
models. Nothing can reach *in*, because `proteincad-gpu` has no rule allowing it
— a security group denies everything it does not explicitly permit. The
alternative, a private subnet, needs a NAT gateway at about $32 a month, which
is more than the GPU box's disk.

---

## Step 4 — let the app box start the GPU box

The app needs permission to call `StartInstances` on that one machine. On EC2
that is a role, so there is no access key to leak.

1. **IAM → Policies → Create policy → JSON.** Paste
   [`iam-policy.json`](iam-policy.json) and fill in `REGION`, `ACCOUNT_ID` and
   the GPU box's instance id. Name it `proteincad-gpu-control`.
2. **IAM → Roles → Create role → AWS service → EC2.** Attach that policy. Name
   it `proteincad-app-role`.
3. **EC2 → select `proteincad-app` → Actions → Security → Modify IAM role.**
   Attach it.

No restart needed; boto3 picks the role up from instance metadata.

---

## Step 5 — a fixed address and a name

A stopped-and-started instance gets a new public IP, and DNS would go stale.

1. **EC2 → Elastic IPs → Allocate**, then **Associate** with `proteincad-app`.
2. In your DNS, add an **A record**: `proteincad-api` → that Elastic IP.
3. If your domain is on Cloudflare, set it to **DNS only** (grey cloud, not
   orange). Caddy needs to answer Let's Encrypt on port 80 directly, and the
   traffic to this record comes from Cloudflare's own network anyway.

Wait until `dig proteincad-api.your-domain.com` returns the right address before
the next step. Caddy will fail to get a certificate if DNS has not caught up.

---

## Step 6 — install the app box

ssh in, then:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv git curl debian-keyring debian-archive-keyring apt-transport-https

# Caddy, for HTTPS that renews itself
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy

# proteinCAD
sudo useradd --system --create-home --home-dir /opt/proteincad proteincad
sudo -u proteincad git clone https://github.com/YOU/proteinCAD.git /opt/proteincad/app
sudo -u proteincad python3 -m venv /opt/proteincad/venv
sudo -u proteincad /opt/proteincad/venv/bin/pip install -e '/opt/proteincad/app[aws]'
```

Settings, the service, and the web server:

```bash
sudo mkdir -p /etc/proteincad
sudo cp /opt/proteincad/app/deploy/aws/app-box/app.env.example /etc/proteincad/app.env
sudo nano /etc/proteincad/app.env        # instance id, region, worker token
sudo chown root:proteincad /etc/proteincad/app.env && sudo chmod 640 /etc/proteincad/app.env

sudo cp /opt/proteincad/app/deploy/aws/app-box/proteincad-app.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now proteincad-app

sudo cp /opt/proteincad/app/deploy/aws/app-box/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile           # your hostname, and a key you generate:
                                         #   openssl rand -hex 32
sudo systemctl reload caddy
```

Check it before going further. From the box:

```bash
curl -s localhost:8080/api/health | head -c 200          # Python is up
curl -si https://proteincad-api.your-domain.com/api/health | head -1   # expect 403
curl -s -H 'X-ProteinCAD-Key: THEKEY' \
     https://proteincad-api.your-domain.com/api/health | head -c 200   # expect JSON
```

A **403 without the key is the point**, not a fault. If you get JSON without
it, the Caddyfile is not being read — `sudo caddy validate --config
/etc/caddy/Caddyfile`.

---

## Step 7 — wire the website to it

In the **Adumbra repo**, not this one:

```bash
mkdir -p functions/proteincad/api
cp ../proteinCAD/deploy/aws/app-box/api-proxy.js \
   functions/proteincad/api/'[[path]].js'
```

Then in the Cloudflare dashboard, **Pages → your project → Settings →
Environment variables**, for **both** Production and Preview:

| name | value | |
|---|---|---|
| `PROTEINCAD_ORIGIN` | `https://proteincad-api.your-domain.com` | plaintext |
| `PROTEINCAD_EDGE_KEY` | the string from the Caddyfile | **Encrypt** |

Commit and push. Cloudflare rebuilds, and `/proteincad/api/*` now reaches your
box while everything else under `/proteincad/` stays a static file.

Nothing in proteinCAD changes for this. The viewer still calls relative URLs, so
the same folder keeps working from a laptop and from Colab.

---

## Step 8 — decide who may use it

Everything so far stops strangers reaching the API *directly*. It does nothing
about a stranger who simply opens your page.

**Cloudflare Zero Trust → Access → Applications → Add → Self-hosted.**

| field | value |
|---|---|
| Application domain | `adumbra.your-domain.com` path `proteincad` |
| Policy | Allow → Emails → your address, and anyone you want |
| Identity | One-time PIN is enough; no identity provider needed |

Now `/proteincad/` asks for an email code, and everything under it — page and
API — is behind that one gate.

> Gating only `/proteincad/api` instead, to keep the viewer publicly visible,
> half works: the page loads for anyone, but their `fetch` gets redirected to a
> login screen it cannot render, and the panel just says nothing is answering.
> Fine as a deliberate choice, confusing as an accident.

If you would rather stay open, at least add **Security → WAF → Rate limiting
rules**: path contains `/proteincad/api/design`, 5 requests per minute per IP.
That plus `PROTEINCAD_MAX_DESIGNS=8` and the 15-minute idle stop bounds the
damage to something survivable.

---

## Step 9 — prove it works

From a computer that has never touched any of this:

1. Open `https://adumbra.your-domain.com/proteincad/`, sign in if you set up
   Access.
2. The padlock is closed and the **GPU** section appears in the Design panel
   saying **stopped**. If it is missing, the proxy is not reaching the box.
3. Click **1CRN**, **Pick site**, click a few residues.
4. **Run design.** The stage line should walk through
   `starting the GPU instance` → `booting` → `waiting for the worker` →
   `GPU ready` → `running RFdiffusion`. First one takes a couple of minutes.
5. In the AWS console the GPU box goes `stopped` → `pending` → `running`.
6. Leave it. Within about fifteen minutes it stops itself, and the panel's
   countdown agrees.

Step 6 is the one worth waiting for. Everything else failing costs you an
afternoon; that one failing costs you a monthly bill.

---

## When it doesn't work

| what you see | where to look |
|---|---|
| Design panel says nothing is answering | `curl` the origin with the key (step 6). If that works, the Pages variables are wrong or not set for that environment |
| `502 cannot reach the proteinCAD server` | the app box is down, or DNS/the certificate moved. `systemctl status proteincad-app caddy` |
| 403 from your own site | `PROTEINCAD_EDGE_KEY` and the Caddyfile disagree |
| GPU section missing | the app has no instance configured — check `/etc/proteincad/app.env` and `systemctl restart proteincad-app` |
| Job fails with `not allowed to start i-…` | the IAM role is not attached, or its ARN names a different instance |
| Job hangs at `waiting for the worker` | `proteincad-gpu` has no rule allowing 8000 from `proteincad-web`, or the boxes are in different VPCs |
| Certificate never issues | DNS not pointing at the Elastic IP yet, port 80 blocked, or the record is proxied (orange cloud) |
