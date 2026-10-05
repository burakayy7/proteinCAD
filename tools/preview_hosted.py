"""The hosted API, on your laptop, with no AWS account and no Cognito.

    python3 tools/preview_hosted.py

Runs the real proteincad/cloud_api.py and proteincad/sqs_worker.py against the
stand-in AWS services in tools/fake_aws.py, writes a web/config.json pointing
at it, and starts the viewer. Everything the hosted deployment does is then
clickable: the Account buttons, the GPU machine coming up, models downloading
with the megabytes counting, Run greying out until they are there, the quota
line, presigned results.

Ctrl-C puts your config.json back the way it was.

WHY THE ACCOUNT BUTTONS ARE NOT THERE WITHOUT THIS
--------------------------------------------------
They are not hidden by accident. `python3 -m proteincad` on its own has no
accounts and no API anywhere else -- the viewer calls relative URLs and talks
to its own server -- so a Sign in button would be offering something that does
not exist. The whole Account section appears only when web/config.json carries
an `auth` block, which is what this writes.

WHAT THIS CANNOT DO
-------------------
Complete a sign-up. Making an account needs a real Cognito user pool, and
until the stack is deployed there is not one. Pressing Create account here
goes to Cognito's /signup page and bounces to its error page, because the
client id in the throwaway config is invented -- which does at least prove the
button reaches the right endpoint with the right parameters.

To sign up for real, see step 6b of deploy/aws/SERVERLESS-DEPLOY.md: deploy,
redeploy with devOrigins set to http://localhost:8321, and write a config.json
with the real values and "redirect": "http://localhost:8321/".
"""

import atexit
import json, os, signal, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path[:0] = [ROOT, ROOT + "/tools"]

from fake_aws import FakeDynamo, FakeEc2, FakeS3, FakeSqs   # noqa: E402
from proteincad import cloud_api, sqs_worker                # noqa: E402

API_PORT = int(os.environ.get("PROTEINCAD_PREVIEW_API", 8322))
VIEWER_PORT = int(os.environ.get("PROTEINCAD_PORT", 8321))
PORT, ORIGIN = API_PORT, f"http://localhost:{VIEWER_PORT}"

WEIGHTS = tempfile.mkdtemp()
os.environ.update(
    PROTEINCAD_TABLE="proteincad-jobs", PROTEINCAD_BUCKET="proteincad-data",
    PROTEINCAD_QUEUE="https://sqs.example/q",
    PROTEINCAD_REGION="us-east-1", PROTEINCAD_MAX_DESIGNS="8",
    PROTEINCAD_DAILY_JOBS="20", PROTEINCAD_CONCURRENT_JOBS="9",
    PROTEINCAD_GLOBAL_DAILY_JOBS="100", PROTEINCAD_IDLE_MINUTES="30",
    PROTEINCAD_MACHINE_STARTS="5", PROTEINCAD_GLOBAL_DAILY_LAUNCHES="20",
    PROTEINCAD_LAUNCH_TEMPLATE="lt-0abc123",
    PROTEINCAD_SUBNETS="subnet-a,subnet-b,subnet-c",
    PROTEINCAD_INSTANCE_TYPE="g4dn.xlarge",
    PROTEINCAD_IMAGE="example/model@sha256:abc",
    # Named, so the worker does not spend four seconds asking a metadata
    # service that is not there and printing an alarming timeout about it.
    # Must be the id the fake EC2 hands out, or the worker stamps model
    # records with one machine's id while the record says another's, and every
    # model reads as absent -- which is exactly the bug this preview is for
    # catching, so it must not have its own version of it.
    PROTEINCAD_INSTANCE="i-000000000001",
    PROTEINCAD_WORK=tempfile.mkdtemp(), PROTEINCAD_WEIGHTS=WEIGHTS,
)

FULL = set((os.environ.get("STUB_FULL_ZONES") or "").split(",")) - {""}
DYNAMO, S3, SQS, EC2 = FakeDynamo(), FakeS3(), FakeSqs(), FakeEc2(full=FULL)
for name, stand_in in (("dynamodb", DYNAMO), ("s3", S3), ("sqs", SQS), ("ec2", EC2)):
    cloud_api.set_client(name, stand_in)

# Presigned links have to be fetchable from the browser, so point them back here.
def presigned(operation, Params, ExpiresIn, **_):
    S3.signed.append((operation, Params["Key"], ExpiresIn))
    return f"http://127.0.0.1:{PORT}/_fake-s3/{Params['Key']}?expires={ExpiresIn}"

S3.generate_presigned_url = presigned

BACKBONE = "\n".join(
    "ATOM  %5d  CA  GLY A%4d    %8.3f%8.3f%8.3f  1.00  0.00           C"
    % (i + 1, i + 1, i * 3.8, (i % 7) * 1.1, 0.0) for i in range(30)) + "\nEND\n"


def container(command, job_id, timeout):
    work = Path(command[command.index("-v") + 1].split(":")[0])
    (work / "designs").mkdir(exist_ok=True)
    written = []
    for i in range(2):
        (work / "designs" / f"design_{i:03d}.pdb").write_text(BACKBONE)
        written.append({"name": f"design_{i + 1}", "file": f"designs/design_{i:03d}.pdb",
                        "metrics": {"residues": 30, "note": "stub, not a design"}})
    (work / "result.json").write_text(json.dumps(
        {"status": "done", "error": "", "log": "run_inference.py (stub)", "designs": written}))
    return {"timed_out": False, "code": 0, "tail": ""}


WORKER = sqs_worker.Worker(run_container=container, sleep=lambda _: None)
# Keeps its instance id -- the worker stamps it onto every model record, and
# blanking it here would hide the very mismatch this preview exists to catch.
# idle_seconds = 0 is what stops it trying to shut anything down.
WORKER.idle_seconds = 0


# A stand-in machine. The real one boots, pulls an image and starts a worker;
# this waits a few seconds so the panel's launching -> booting -> ready is
# something you can actually watch happen.
BOOT_SECONDS = 16
SLOW = 0.25   # per simulated chunk, so a download takes about ten seconds


def slow_download(key, path, progress):
    body = S3.objects.get(("proteincad-data", key))
    if body is None:
        raise RuntimeError(f"NoSuchKey: {key}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    # Report the manifest's size rather than the stub's tiny body, in chunks,
    # so the byte counter in the panel moves the way it will in production.
    manifest = json.loads(S3.objects[("proteincad-data", "weights/manifest.json")])
    size = next((f["bytes"] for m in manifest["models"].values()
                 for f in m["files"] if f["key"] == key), len(body))
    sent = 0
    while sent < size:
        step = min(size // 20 or 1, size - sent)
        time.sleep(SLOW)
        sent += step
        progress(step)


WORKER._download = slow_download
BOOTED = {"at": None}


def machine_life():
    """Boot the machine a few seconds after somebody launches it."""
    while True:
        try:
            state = cloud_api.read_machine()["state"]
            if state.get("state") == "booting" and BOOTED["at"] is None:
                BOOTED["at"] = time.time()
            if BOOTED["at"] and time.time() - BOOTED["at"] > BOOT_SECONDS:
                WORKER.serve_downloads()
            elif state.get("state") in ("booting", "launching"):
                # Walk the same four steps the real launch template's user data
                # reports, so what the panel shows here is what it will show on
                # a real machine -- only faster.
                gone = time.time() - (BOOTED["at"] or time.time())
                steps = [(1, "preparing the local disk"),
                         (2, "setting up the container runtime"),
                         (3, "downloading the model software, about 7 GB"),
                         (4, "starting the worker")]
                step, stage = steps[min(3, int(gone / (BOOT_SECONDS / 4.0)))]
                cloud_api.client("dynamodb").update_item(
                    TableName="proteincad-jobs", Key=cloud_api.machine_key(),
                    **cloud_api.set_fields(heartbeat=time.time(), stage=stage,
                                           step=step, steps=4))
            if state.get("state") == "gone":
                BOOTED["at"] = None
        except Exception as error:
            print("machine:", type(error).__name__, error, flush=True)
        time.sleep(1.0)


def drain():
    while True:
        if SQS.messages and BOOTED["at"] and time.time() - BOOTED["at"] > BOOT_SECONDS:
            try:
                WORKER.handle(SQS.receive_message(QueueUrl="")["Messages"][0])
            except Exception as error:
                print("worker:", type(error).__name__, error, flush=True)
        time.sleep(0.3)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "content-type, authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")

    def serve(self, method):
        path = urlparse(self.path).path
        if path.startswith("/_fake-s3/"):
            body = S3.objects.get(("proteincad-data", path[len("/_fake-s3/"):]))
            return self.reply(404 if body is None else 200,
                              body or b"no such object", "chemical/x-pdb")

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else None
        which = "api"
        if (path == "/design" or path.endswith("/fold")
                or path.startswith("/machine/")):
            which = "submit"
        os.environ["PROTEINCAD_FUNCTION"] = which
        print(f"  {method} {path} -> {which}", flush=True)
        # Real RunInstances takes a few seconds behind a Lambda cold start.
        # Simulated, because that window is exactly where the panel has to
        # show something and where it used to show nothing.
        if path == "/machine/start":
            time.sleep(float(os.environ.get("PROTEINCAD_PREVIEW_START_DELAY", "0")))
        answer = cloud_api.handler({
            "version": "2.0", "rawPath": path,
            "requestContext": {"http": {"method": method, "path": path}, "stage": "$default",
                               "authorizer": {"jwt": {"claims": {
                                   "sub": "browser-test", "email": "you@example.com"}}}},
            "body": raw, "isBase64Encoded": False,
        }, None)
        self.reply(answer["statusCode"], answer["body"].encode(),
                   answer["headers"]["content-type"])

    def reply(self, status, body, content_type):
        self.send_response(status); self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# The weights bucket, as publish-weights.py would have left it.
import hashlib  # noqa: E402

FILES = {
    "rfdiffusion": {"Base_ckpt.pt": b"base" * 999, "Complex_base_ckpt.pt": b"cplx" * 999},
    "proteinmpnn": {"v_48_020.pt": b"mpnn" * 999},
    "esmfold": {"model.safetensors": b"esm" * 999, "config.json": b"{}"},
}
SIZES = {"rfdiffusion": 3_900_000_000, "proteinmpnn": 100_000_000, "esmfold": 8_500_000_000}
manifest = {"version": 1, "models": {}}
for model, files in FILES.items():
    entries = []
    for name, body in files.items():
        S3.objects[("proteincad-data", f"weights/{model}/{name}")] = body
        entries.append({"key": f"weights/{model}/{name}",
                        "bytes": SIZES[model] // len(files),
                        "sha256": hashlib.sha256(body).hexdigest()})
    manifest["models"][model] = {"bytes": SIZES[model], "files": entries}
S3.objects[("proteincad-data", "weights/manifest.json")] = json.dumps(manifest).encode()

# ----------------------------------------------------- the config, and the app
#
# Written here rather than left to you, and put back on the way out. A
# config.json left behind after a preview is the sort of thing that makes the
# next `python3 -m proteincad` mysteriously talk to a port that is no longer
# listening.

CONFIG = Path(ROOT) / "web" / "config.json"
BACKUP = Path(ROOT) / "web" / "config.json.before-preview"

# Invented, and it has to be: there is no user pool to be a client of. The
# domain is a real Cognito hostname so that pressing the buttons demonstrates
# the redirect rather than a DNS failure.
PREVIEW_CONFIG = {
    "_comment": "Written by tools/preview_hosted.py. Deleted when it exits.",
    "api": f"http://127.0.0.1:{API_PORT}",
    "auth": {
        "domain": "https://adumbra-proteincad.auth.us-east-1.amazoncognito.com",
        "clientId": "preview-not-a-real-client-id",
        "redirect": f"http://localhost:{VIEWER_PORT}/",
    },
}


def put_config_back():
    try:
        if BACKUP.exists():
            BACKUP.replace(CONFIG)
            print("\nput your web/config.json back")
        elif CONFIG.exists():
            CONFIG.unlink()
            print("\nremoved the preview web/config.json")
    except OSError as error:
        print(f"\ncould not tidy up {CONFIG}: {error}")


def main():
    if CONFIG.exists():
        CONFIG.replace(BACKUP)
        print(f"kept your web/config.json as {BACKUP.name}")
    CONFIG.write_text(json.dumps(PREVIEW_CONFIG, indent=2) + "\n")

    atexit.register(put_config_back)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(0))

    threading.Thread(target=machine_life, daemon=True).start()
    threading.Thread(target=drain, daemon=True).start()
    api = ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler)
    threading.Thread(target=api.serve_forever, daemon=True).start()

    print(f"""
  stand-in cloud API   http://127.0.0.1:{API_PORT}
  viewer               http://localhost:{VIEWER_PORT}/

  Open the viewer, go to the DESIGN tab, and the Account section is at the top
  with Create account and Sign in. Both go to Cognito and bounce off its error
  page -- there is no user pool yet, and the client id above is invented.

  Everything below Account is fully working against the stand-ins: press
  Start GPU machine, watch it boot, download a model and watch the megabytes,
  and see Run stop being greyed out. Ctrl-C when you are done.
""", flush=True)

    # Loopback, because this is a preview with no authentication in front of it.
    from proteincad.server import serve
    serve(host="127.0.0.1", port=VIEWER_PORT, open_browser=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
