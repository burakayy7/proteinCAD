"""The GPU box: started when a job needs it, stopped when nothing does.

A GPU instance bills by the second whether or not it is computing, and nothing
in this app needs one until somebody presses Run. So the instance is kept
stopped, woken by the first job that needs it, and stopped again once the work
is done and nobody has asked for more.

Stopped is not terminated. The root volume survives, so the checkouts, the
weights and the environment are all still there next time and waking is a boot
rather than an install. A stopped instance costs its EBS storage -- pennies a
day -- against roughly a dollar an hour for the card.

Two watchdogs, because one is not enough:

    here          stops the instance once no job has needed it for
                  PROTEINCAD_EC2_IDLE minutes.
    on the box    deploy/aws/proteincad-idle.sh, run by a systemd timer, powers
                  the machine off when the worker has been idle. This is the
                  one that matters: it still works when this process has
                  crashed, the laptop has closed or the network has gone.

Everything here is configuration -- instance id, region, port, address -- so
moving the app to a different host means changing environment variables, and
this module stays the only part of proteinCAD that has ever heard of AWS. The
worker running on the instance is colab_worker.py, unchanged and unaware of any
of this; Colab and EC2 differ in who turns the machine on, not in what runs.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import urllib.error
import urllib.request

# The instance states worth acting on. `pending` and `stopping` are transitions
# to wait out; `terminated` and `shutting-down` mean the volume is going away
# and there is nothing here to start.
LIVE = "running"
ASLEEP = "stopped"
GONE = ("terminated", "shutting-down")


class Ec2Error(RuntimeError):
    """Something about the instance is wrong in a way worth telling the user:
    no credentials, no such instance, a box that never came up. Reportable, not
    a bug, so it travels as a message rather than a traceback."""


class Cancelled(Ec2Error):
    """The job was cancelled while the instance was coming up. Not a failure --
    the user asked for it -- so the runner returns quietly rather than
    reporting it as one."""


def _boto3():
    """boto3, or an error that says how to get it.

    Imported here rather than at module scope so that a checkout with no AWS
    anything still starts, serves the viewer and runs mock jobs. The server is
    standard library only on purpose; this is the one optional dependency, and
    it is only reached when an instance id has actually been configured.
    """
    try:
        import boto3
    except ImportError as error:
        raise Ec2Error(
            "starting an EC2 instance needs boto3, which is not installed here. "
            "pip install 'proteincad[aws]'  (or: pip install boto3)"
        ) from error
    return boto3


def _reporter(job):
    """Somewhere to say what is happening. A job shows its stage in the panel,
    so a two-minute boot reads as progress rather than as a hang."""
    def say(text: str) -> None:
        if job is not None:
            job.stage = text
        print(f"[ec2] {text}", flush=True)
    return say


class Machine:
    """One EC2 instance, and the right to start and stop it.

    Held for the life of the process (see `from_config`) because the idle clock
    and the count of jobs currently using the box are state: rebuilding this
    object would forget both and turn the instance off underneath a running
    job.
    """

    def __init__(self, instance_id: str, region: str = "", profile: str = "",
                 port: int = 8000, scheme: str = "http", address: str = "public",
                 token: str = "", idle_seconds: int = 900, boot_timeout: int = 600,
                 tick: float = 30.0, poll_seconds: float = 4.0):
        self.instance_id = instance_id
        self.region = region
        self.profile = profile
        self.port = int(port)
        self.scheme = scheme or "http"
        self.address = address or "public"
        self.token = token
        # 0 disables this watchdog and leaves the one on the box in charge.
        self.idle_seconds = max(0, int(idle_seconds))
        self.boot_timeout = int(boot_timeout)
        # How often to look while something is changing: `tick` between idle
        # checks, `poll_seconds` between "is it up yet". Both are settings so
        # the waiting can be tested without actually waiting.
        self.tick = float(tick)
        self.poll_seconds = float(poll_seconds)

        self._client = None
        self._lock = threading.Lock()
        self._described: tuple = (0.0, {})
        self._watcher = None
        self._waking = None
        # How many jobs are on the box right now. The idle watchdog never stops
        # an instance with a lease out, so a run that takes an hour with nothing
        # to report is not mistaken for an idle one.
        self.leases = 0
        # Now, not zero: a process that starts while the instance is already
        # running has inherited it, and should give it a full idle window before
        # deciding nobody wants it.
        self.last_used = time.time()
        self.last_error = ""
        self.warning = ""
        self._checked_shutdown = False

    # ------------------------------------------------------------- aws calls

    def client(self):
        with self._lock:
            if self._client is None:
                boto3 = _boto3()
                session = boto3.Session(profile_name=self.profile or None)
                kwargs = {"region_name": self.region} if self.region else {}
                self._client = session.client("ec2", **kwargs)
            return self._client

    def _explain(self, error: Exception, what: str) -> str:
        """Turn a botocore exception into the sentence that fixes it.

        These are the four that happen, and each has a different cause and a
        different remedy; the raw exception names none of them.
        """
        name = type(error).__name__
        text = str(error)
        where = self.region or "the configured region"
        if "NoCredentials" in name or "NoCredentials" in text:
            return ("no AWS credentials. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or "
                    "run `aws configure`, or give the machine running proteinCAD an IAM role.")
        if "NoRegionError" in name:
            return ("no AWS region set. Put the instance's region in PROTEINCAD_EC2_REGION "
                    "(or AWS_DEFAULT_REGION).")
        if "InvalidInstanceID" in text:
            return (f"there is no instance {self.instance_id} in {where}. Check "
                    "PROTEINCAD_EC2_INSTANCE and PROTEINCAD_EC2_REGION -- an instance id is "
                    "only unique within its region.")
        if "UnauthorizedOperation" in text or "AuthFailure" in text or "AccessDenied" in text:
            return (f"these AWS credentials are not allowed to {what} {self.instance_id}. They "
                    "need ec2:DescribeInstances, ec2:StartInstances and ec2:StopInstances -- "
                    "the policy is in deploy/aws/iam-policy.json.")
        return f"could not {what} {self.instance_id}: {name}: {text}"

    def describe(self, max_age: float = 10.0) -> dict:
        """State and addresses, cached briefly.

        The panel polls this every few seconds and a waiting job polls it too;
        without the cache that is a DescribeInstances call per poll, which AWS
        throttles long before it costs anything. Pass max_age=0 when the answer
        has to be current -- right after asking for a start or a stop.
        """
        stamp, cached = self._described
        if cached and time.time() - stamp < max_age:
            return cached
        try:
            reply = self.client().describe_instances(InstanceIds=[self.instance_id])
        except Ec2Error:
            raise
        except Exception as error:
            raise Ec2Error(self._explain(error, "look up")) from error

        for reservation in reply.get("Reservations", []):
            for item in reservation.get("Instances", []):
                found = {
                    "state": (item.get("State") or {}).get("Name", "unknown"),
                    "public_ip": item.get("PublicIpAddress", ""),
                    "public_dns": item.get("PublicDnsName", ""),
                    "private_ip": item.get("PrivateIpAddress", ""),
                    "type": item.get("InstanceType", ""),
                }
                self._described = (time.time(), found)
                return found
        raise Ec2Error(f"there is no instance {self.instance_id} in "
                       f"{self.region or 'the configured region'}")

    def check_shutdown_behavior(self) -> None:
        """Make sure a shutdown from inside the box stops it rather than
        destroying it.

        The watchdog on the instance powers the machine off, and what EC2 does
        next is an instance attribute. The default is `stop`, which is what this
        whole design assumes -- but a `terminate` there turns an idle timeout
        into the permanent loss of a root volume holding 12 GB of downloaded
        weights. Best effort: the permission is not in the minimum policy, so a
        denial here means the check could not run, not that anything is wrong.
        """
        if self._checked_shutdown:
            return
        self._checked_shutdown = True
        try:
            reply = self.client().describe_instance_attribute(
                InstanceId=self.instance_id, Attribute="instanceInitiatedShutdownBehavior")
            behaviour = (reply.get("InstanceInitiatedShutdownBehavior") or {}).get("Value", "")
        except Exception:
            return
        if behaviour == "terminate":
            self.warning = (
                f"{self.instance_id} is set to TERMINATE on shutdown. The idle watchdog on the "
                "box powers it off, which would destroy it and everything installed on it. Fix "
                f"it with:  aws ec2 modify-instance-attribute --instance-id {self.instance_id} "
                "--instance-initiated-shutdown-behavior stop")
            print(f"[ec2] ! {self.warning}", flush=True)

    # --------------------------------------------------------------- address

    def host(self, info: dict) -> str:
        """Where to reach the worker.

        `public` and `private` are read off the instance at every start, which
        is what makes an Elastic IP unnecessary: a stopped instance comes back
        with a new public address each time, and asking costs nothing where
        holding an address costs by the hour. Anything else is used literally,
        which is how to point at an Elastic IP or a name you own.
        """
        if self.address == "public":
            host = info.get("public_ip") or info.get("public_dns")
            if not host:
                raise Ec2Error(
                    f"{self.instance_id} is running but has no public address. Either put it in "
                    "a subnet that auto-assigns a public IPv4 (or give it an Elastic IP), or set "
                    "PROTEINCAD_EC2_ADDRESS=private if proteinCAD runs inside the same VPC.")
            return host
        if self.address == "private":
            host = info.get("private_ip")
            if not host:
                raise Ec2Error(f"{self.instance_id} has no private address yet")
            return host
        return self.address

    def endpoint(self, info: dict) -> str:
        return f"{self.scheme}://{self.host(info)}:{self.port}"

    def _health(self, url: str, timeout: float = 6.0) -> dict:
        """Ask the worker how it is. Raises for anything except a good answer,
        so callers can treat "no answer" and "not a worker" alike."""
        request = urllib.request.Request(f"{url}/health")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())

    def _answering(self, url: str) -> bool:
        try:
            return self._health(url).get("status") == "ok"
        except Exception:
            return False

    # ------------------------------------------------------------ the waking

    def wake(self, job=None) -> str:
        """Bring the instance up and return the worker's address.

        Blocks for as long as that takes -- typically a minute or two from
        stopped, none at all when it is already up. Every wait reports itself
        through the job's stage, because the alternative is a progress bar that
        sits at zero while EC2 boots and reads as a hang.
        """
        say = _reporter(job)
        deadline = time.time() + self.boot_timeout
        self.check_shutdown_behavior()

        info = self.describe(max_age=5.0)
        state = info["state"]
        if state in GONE:
            raise Ec2Error(f"{self.instance_id} is {state}; there is nothing left to start. "
                           "Launch a new instance and set PROTEINCAD_EC2_INSTANCE to it.")

        if state == "stopping":
            # Asking a stopping instance to start is an error, not a no-op, so
            # the only thing to do is let it finish.
            say("the GPU is still shutting down; waiting for it")
            info = self._await({ASLEEP}, deadline, job, say)
            state = info["state"]

        if state == ASLEEP:
            say(f"starting the GPU instance ({info.get('type') or self.instance_id})")
            try:
                self.client().start_instances(InstanceIds=[self.instance_id])
            except Exception as error:
                raise Ec2Error(self._explain(error, "start")) from error
            self._described = (0.0, {})
            info = self.describe(max_age=0)
            state = info["state"]

        if state == "pending":
            say("the GPU instance is booting")
            info = self._await({LIVE}, deadline, job, say)
            state = info["state"]

        if state != LIVE:
            raise Ec2Error(f"{self.instance_id} is {state}, which is not a state it can serve "
                           "jobs in")

        return self._await_worker(info, deadline, job, say)

    def _await(self, wanted: set, deadline: float, job, say) -> dict:
        """Poll the instance until it reaches one of `wanted`."""
        while True:
            info = self.describe(max_age=0)
            if info["state"] in wanted:
                return info
            if info["state"] in GONE:
                raise Ec2Error(f"{self.instance_id} went to {info['state']} while we waited")
            if job is not None and job.cancelled:
                raise Cancelled("cancelled while the GPU was starting")
            if time.time() > deadline:
                raise Ec2Error(f"{self.instance_id} was still {info['state']} after "
                               f"{self.boot_timeout} s. Raise PROTEINCAD_EC2_BOOT if this box "
                               "is simply slow to boot.")
            time.sleep(self.poll_seconds)

    def _await_worker(self, info: dict, deadline: float, job, say) -> str:
        """Running is not ready: the address appears a moment after the state
        does, and the worker itself still has to start behind it."""
        trouble = ""
        announced = False
        while True:
            try:
                url = self.endpoint(info)
                if self._answering(url):
                    self.touch()
                    say(f"GPU ready at {url}")
                    return url
                trouble = ""
            except Ec2Error as error:
                # No address yet. Worth keeping: if this is still the reason
                # when the clock runs out, it is the useful message.
                trouble = str(error)

            if job is not None and job.cancelled:
                raise Cancelled("cancelled while the GPU was starting")
            if time.time() > deadline:
                if trouble:
                    raise Ec2Error(trouble)
                raise Ec2Error(
                    f"{self.instance_id} is running but nothing answered on "
                    f"{self.endpoint(info)}/health within {self.boot_timeout} s. Three things "
                    "do this: the worker service is not running (ssh in and run `systemctl "
                    f"status proteincad-worker`), the security group does not allow port "
                    f"{self.port} from this machine, or PROTEINCAD_EC2_PORT is not the port the "
                    "worker was started on.")
            if not announced:
                say("waiting for the worker to answer")
                announced = True
            time.sleep(self.poll_seconds)
            info = self.describe(max_age=5.0)
            if info["state"] != LIVE:
                raise Ec2Error(f"{self.instance_id} went to {info['state']} while we waited "
                               "for the worker")

    def start_soon(self) -> None:
        """Bring it up in the background, for the panel's Start button.

        Waking takes a minute or two and no browser should hold a request open
        for that; the panel polls instead. The lease this holds is what stops
        the idle watchdog from turning it straight back off, and releasing it at
        the end restarts the idle clock -- so pressing Start buys a full idle
        window to get a design ready in.
        """
        with self._lock:
            if self._waking is not None and self._waking.is_alive():
                return
            self._waking = threading.Thread(target=self._wake_quietly, daemon=True,
                                            name="proteincad-ec2-wake")
            self._waking.start()

    def _wake_quietly(self) -> None:
        try:
            with self.session():
                pass
        except Ec2Error as error:
            # Nobody is waiting on a response, so the error has to be kept
            # somewhere the panel can find it or the button silently does
            # nothing at all.
            self.last_error = str(error)
            print(f"[ec2] {error}", flush=True)
        else:
            self.last_error = ""

    # -------------------------------------------------------------- the lease

    @contextlib.contextmanager
    def session(self, job=None):
        """Hold the instance up for the duration of a job."""
        with self._lock:
            self.leases += 1
        self.watch()
        try:
            yield self.wake(job)
        finally:
            with self._lock:
                self.leases -= 1
            self.touch()

    def touch(self) -> None:
        self.last_used = time.time()

    def seconds_idle(self) -> float:
        return 0.0 if self.leases else max(0.0, time.time() - self.last_used)

    # ----------------------------------------------------------- the stopping

    def stop(self, reason: str = "asked") -> dict:
        try:
            self.client().stop_instances(InstanceIds=[self.instance_id])
        except Ec2Error:
            raise
        except Exception as error:
            raise Ec2Error(self._explain(error, "stop")) from error
        print(f"[ec2] stopping {self.instance_id} ({reason})", flush=True)
        self._described = (0.0, {})
        return self.describe(max_age=0)

    def watch(self) -> None:
        """Start the idle watchdog, once."""
        with self._lock:
            if self._watcher is not None and self._watcher.is_alive():
                return
            self._watcher = threading.Thread(target=self._idle_loop, daemon=True,
                                             name="proteincad-ec2-idle")
            self._watcher.start()

    def _idle_loop(self) -> None:
        while True:
            time.sleep(self.tick)
            if not self.idle_seconds:
                continue
            try:
                if self.leases or self.seconds_idle() < self.idle_seconds:
                    continue
                info = self.describe(max_age=self.tick)
                if info["state"] != LIVE:
                    continue
                if self._busy_elsewhere(info):
                    # Work on the card that this process did not start -- most
                    # likely a job from before proteinCAD was restarted.
                    # Stopping now would throw it away.
                    self.touch()
                    continue
                self.stop(f"idle for {self.idle_seconds // 60} min")
            except Ec2Error as error:
                print(f"[ec2] idle check: {error}", flush=True)
            except Exception as error:  # a watchdog that dies costs real money
                print(f"[ec2] idle check: {type(error).__name__}: {error}", flush=True)

    def _busy_elsewhere(self, info: dict) -> bool:
        try:
            return bool(self._health(self.endpoint(info)).get("busy"))
        except Exception:
            # Not answering is not busy. A worker that has fallen over is
            # exactly the case this watchdog exists for.
            return False

    # ------------------------------------------------------------- reporting

    def status(self) -> dict:
        """What the panel shows: where it is, and when it turns itself off."""
        out = {
            "configured": True,
            "instance": self.instance_id,
            "region": self.region,
            "idle_minutes": round(self.idle_seconds / 60.0, 1),
            "jobs": self.leases,
            "waking": bool(self._waking is not None and self._waking.is_alive()),
            "idle_for": round(self.seconds_idle()),
            "error": self.last_error,
            "warning": self.warning,
        }
        try:
            info = self.describe()
        except Ec2Error as error:
            out["state"] = "unknown"
            out["error"] = str(error)
            return out
        out["state"] = info["state"]
        out["type"] = info["type"]
        out["endpoint"] = ""
        if info["state"] == LIVE:
            with contextlib.suppress(Ec2Error):
                out["endpoint"] = self.endpoint(info)
            if self.idle_seconds and not self.leases:
                out["stops_in"] = max(0, round(self.idle_seconds - self.seconds_idle()))
        return out


# One Machine per instance, for the life of the process.
#
# build_runners() runs again every time the compute endpoint is repointed, and a
# fresh Machine each time would forget the idle clock and, worse, the count of
# jobs currently on the box -- which is the thing that stops the watchdog from
# turning the instance off underneath a running job.
_MACHINES: dict = {}


def from_config(config):
    """The machine this deployment is configured for, or None for no EC2."""
    instance = str(getattr(config, "ec2_instance", "") or "").strip()
    if not instance:
        return None
    region = str(getattr(config, "ec2_region", "") or "").strip()
    machine = _MACHINES.get((instance, region))
    if machine is None:
        machine = Machine(
            instance,
            region=region,
            profile=str(getattr(config, "ec2_profile", "") or ""),
            port=int(getattr(config, "ec2_port", 8000) or 8000),
            scheme=str(getattr(config, "ec2_scheme", "http") or "http"),
            address=str(getattr(config, "ec2_address", "public") or "public"),
            token=str(getattr(config, "ec2_token", "") or ""),
            idle_seconds=int(round(float(getattr(config, "ec2_idle_minutes", 15) or 0) * 60)),
            boot_timeout=int(getattr(config, "ec2_boot_timeout", 600) or 600),
        )
        _MACHINES[(instance, region)] = machine
    return machine
