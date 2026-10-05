"""Runtime configuration.

Every setting comes from a default, an environment variable, or a command line
flag -- in that order. Nothing that changes between a laptop, a Colab session
and a web server is written into the source, so the same checkout runs in all
three. In particular the compute endpoint is configuration: this file is the
only place that knows such a thing exists.

    PROTEINCAD_HOST                 interface to bind            (127.0.0.1)
    PROTEINCAD_PORT                 port                         (8080)
    PROTEINCAD_WEB_DIR              the web app                  (<repo>/web)
    PROTEINCAD_DATA_DIR             samples and cache            (<repo>/data)
    PROTEINCAD_RUNNER               default design runner        (mock)
    PROTEINCAD_COMPUTE_URL          remote GPU endpoint          (unset)
    PROTEINCAD_COMPUTE_TOKEN        bearer token for it          (unset)
    PROTEINCAD_COMPUTE_TIMEOUT      seconds per request          (120)
    PROTEINCAD_MAX_DESIGNS          cap on designs per job       (32)
    PROTEINCAD_ALLOW_REMOTE_CONFIG  let the browser set the
                                    compute endpoint    (on for loopback)

An EC2 instance this app starts and stops for itself, so a GPU is only running
while there is work for it. Setting the instance id is what turns the `ec2`
runner on; everything else has a working default.

    PROTEINCAD_EC2_INSTANCE         instance id, i-0abc...        (unset)
    PROTEINCAD_EC2_REGION           the region it lives in  (AWS_DEFAULT_REGION)
    PROTEINCAD_EC2_PROFILE          credentials profile      (default chain)
    PROTEINCAD_EC2_PORT             port the worker serves on    (8000)
    PROTEINCAD_EC2_SCHEME           http or https                (http)
    PROTEINCAD_EC2_ADDRESS          public | private | a host    (public)
    PROTEINCAD_EC2_TOKEN            bearer token for the worker
                                                        (COMPUTE_TOKEN)
    PROTEINCAD_EC2_IDLE             minutes idle before it is
                                    stopped; 0 never             (15)
    PROTEINCAD_EC2_BOOT             seconds to wait for it to
                                    come up                      (600)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default=None):
    value = os.environ.get(f"PROTEINCAD_{name}")
    return default if value is None or value == "" else value


def is_loopback(host: str) -> bool:
    """Is this server reachable only from the machine it runs on?"""
    return str(host or "").strip().lower() in ("127.0.0.1", "localhost", "::1", "")


def _flag(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8080
    web_dir: Path = ROOT / "web"
    data_dir: Path = ROOT / "data"
    verbose: bool = False

    runner: str = "mock"
    compute_url: str = ""
    compute_token: str = ""
    compute_timeout: int = 120
    max_designs: int = 32
    allow_remote_config: bool = False

    # The GPU this deployment may start for itself. Empty means it has none,
    # and the `ec2` runner is simply not offered.
    ec2_instance: str = ""
    ec2_region: str = ""
    ec2_profile: str = ""
    ec2_port: int = 8000
    ec2_scheme: str = "http"
    ec2_address: str = "public"
    ec2_token: str = ""
    ec2_idle_minutes: float = 15.0
    ec2_boot_timeout: int = 600

    structure_dirs: list = field(default_factory=list)

    def __post_init__(self):
        self.web_dir = Path(self.web_dir).resolve()
        self.data_dir = Path(self.data_dir).resolve()
        # Always derived, so overriding data_dir moves these with it.
        self.structure_dirs = [self.data_dir / "samples", self.cache_dir]

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def scans_dir(self) -> Path:
        """Rotational scans, one folder per scan, named after its contents.

        Separate from jobs_dir because the two are keyed differently and on
        purpose: a design job is numbered because asking twice is a second
        design, and a scan is hashed because asking twice is the same curve.
        """
        return self.data_dir / "scans"

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.scans_dir.mkdir(parents=True, exist_ok=True)

    def public(self) -> dict:
        """What is safe to tell the browser -- never the token."""
        return {
            "runner": self.runner,
            "compute_configured": bool(self.compute_url),
            # The address, so the panel can show what it is pointed at and
            # offer to change it. The token stays here.
            "compute_url": self.compute_url,
            "max_designs": self.max_designs,
            "allow_remote_config": self.allow_remote_config,
        }


def from_env(**overrides) -> Config:
    """Build a Config from the environment, with explicit overrides winning."""
    config = Config(
        host=_env("HOST", "127.0.0.1"),
        port=int(_env("PORT", 8080)),
        web_dir=Path(_env("WEB_DIR", ROOT / "web")),
        data_dir=Path(_env("DATA_DIR", ROOT / "data")),
        verbose=_flag("VERBOSE"),
        runner=_env("RUNNER", "mock"),
        compute_url=str(_env("COMPUTE_URL", "")).rstrip("/"),
        compute_token=str(_env("COMPUTE_TOKEN", "")),
        compute_timeout=int(_env("COMPUTE_TIMEOUT", 120)),
        max_designs=int(_env("MAX_DESIGNS", 32)),
        ec2_instance=str(_env("EC2_INSTANCE", "")).strip(),
        # AWS's own variables are the fallback so a machine already configured
        # for the CLI needs nothing else said.
        ec2_region=str(_env("EC2_REGION", "") or os.environ.get("AWS_DEFAULT_REGION", "")).strip(),
        ec2_profile=str(_env("EC2_PROFILE", "")).strip(),
        ec2_port=int(_env("EC2_PORT", 8000)),
        ec2_scheme=str(_env("EC2_SCHEME", "http")).strip(),
        ec2_address=str(_env("EC2_ADDRESS", "public")).strip(),
        # One worker, one token: the value the worker was started with. Kept
        # separate only so a deployment can talk to a Colab tunnel and an EC2
        # box at the same time without them sharing a secret.
        ec2_token=str(_env("EC2_TOKEN", "") or _env("COMPUTE_TOKEN", "")),
        ec2_idle_minutes=float(_env("EC2_IDLE", 15)),
        ec2_boot_timeout=int(_env("EC2_BOOT", 600)),
        # On by default when this is a single-user server on loopback: following
        # a Colab tunnel to its new address is ordinary use there, and the risk
        # it carries -- pointing the server somewhere else -- needs something on
        # your own machine to do it. Off the moment the server is reachable from
        # anywhere else.
        allow_remote_config=_flag("ALLOW_REMOTE_CONFIG",
                                  default=is_loopback(_env("HOST", "127.0.0.1"))),
    )
    for key, value in overrides.items():
        if value is not None and hasattr(config, key):
            setattr(config, key, value)
    # After the overrides, because one of them may be the host. A default that
    # is derived from another setting has to be derived from its final value,
    # or `--host 0.0.0.0` would leave the loopback default in place -- which is
    # the one case it exists to prevent.
    if _env("ALLOW_REMOTE_CONFIG") is None and overrides.get("allow_remote_config") is None:
        config.allow_remote_config = is_loopback(config.host)
    config.__post_init__()
    return config
