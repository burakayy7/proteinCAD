#!/usr/bin/env python3
"""The proteinCAD stack.

    cd deploy/aws/cdk
    python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
    npx aws-cdk synth          # renders the template. Creates nothing.
    npx aws-cdk diff           # what would change. Creates nothing.
    npx aws-cdk deploy         # creates it. Only run this when you mean to.

Everything that varies is context, in cdk.json or on the command line:

    npx aws-cdk deploy -c proteincad:budgetEmail=you@example.com

No account number, no region and no domain is written into the source, so the
same stack deploys twice into different accounts without editing anything.
"""

import os
import sys
from pathlib import Path


def _usable_tmpdir():
    """Make sure the TMPDIR *variable* points somewhere this user can write.

    CDK's Python bindings run a Node process (jsii) which inherits TMPDIR and
    immediately mkdtemp()s in it. On macOS, TMPDIR is sometimes left pointing
    at /var/folders/zz/zyxvpxvq6csfxvn_n0000000000000/T -- the fallback temp
    directory, which is owned by root with mode 700. A normal user cannot write
    there, so the Node side dies with

        EACCES: permission denied, mkdtemp '.../jsii-kernel-XXXXXX'

    and the CDK CLI reports only `Subprocess exited with error 1`. Nothing in
    that names TMPDIR, and it happens before any of this stack is evaluated, so
    it reads as a broken stack when it is a broken environment.

    The variable is what has to be checked, not Python's idea of a temp
    directory. `tempfile` walks a list of candidates and quietly skips any it
    cannot write to, so it reports /tmp and everything looks fine from here --
    while Node, which takes $TMPDIR at its word, fails. Asking Python where it
    would put a file answers a question nobody is asking.

    An unset TMPDIR is left alone: Node falls back to /tmp on its own.
    """
    named = os.environ.get("TMPDIR")
    if not named:
        return

    def writable(where):
        probe = os.path.join(where, ".proteincad-tmp-probe-%d" % os.getpid())
        try:
            os.mkdir(probe)
            os.rmdir(probe)
            return True
        except OSError:
            return False

    if writable(named):
        return

    for candidate in ("/tmp", os.path.expanduser("~/.cache"), os.getcwd()):
        if os.path.isdir(candidate) and writable(candidate):
            os.environ["TMPDIR"] = candidate
            import tempfile

            tempfile.tempdir = candidate
            sys.stderr.write(
                "note: TMPDIR was %s, which this user cannot write to.\n"
                "      Using %s instead, or the CDK's Node process would fail\n"
                "      with EACCES before reading a line of this stack.\n"
                % (named, candidate))
            return

    sys.stderr.write(
        "warning: TMPDIR (%s) is not writable and no alternative was found. "
        "CDK will fail with EACCES from jsii.\n" % named)


_usable_tmpdir()

# Find the right interpreter before asking for anything that needs it.
#
# cdk.json runs `python3 app.py`, and `python3` is whatever the shell resolves
# -- usually not the venv, because the CDK CLI is not started from inside it.
# The failure that produces is `Subprocess exited with error 1` with the
# traceback swallowed, which says nothing about a virtualenv. So: if aws_cdk is
# not importable here and there is a venv next door, start again with that one.
try:
    import aws_cdk as cdk
except ImportError:
    _venv = Path(__file__).resolve().parent / ".venv" / "bin" / "python3"
    if _venv.is_file() and not os.environ.get("PROTEINCAD_CDK_REEXEC"):
        os.environ["PROTEINCAD_CDK_REEXEC"] = "1"
        os.execv(str(_venv), [str(_venv), __file__, *sys.argv[1:]])
    raise SystemExit(
        "\naws_cdk is not installed for %s.\n\n"
        "  cd %s\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r requirements.txt\n\n"
        "Then run cdk again. There is no need to activate it -- app.py finds\n"
        ".venv by itself once it exists.\n"
        % (sys.executable, Path(__file__).resolve().parent))

from proteincad_stack import DLAMI, ProteincadStack, Settings


app = cdk.App()


def context(key, default=None, kind=str):
    value = app.node.try_get_context("proteincad:" + key)
    if value is None or value == "":
        return default
    return kind(value)


def flag(key, default):
    value = app.node.try_get_context("proteincad:" + key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


settings = Settings(
    origin=context("origin", "https://adumbra.burakayy.com"),
    # Anybody may make an account, confirmed by an emailed code. With this on,
    # the quotas below are the whole of the cost control -- see "The limits"
    # in SERVERLESS-DEPLOY.md. `-c proteincad:openSignUp=false` closes it,
    # in place, without disturbing existing accounts.
    open_sign_up=flag("openSignUp", True),
    # Extra origins allowed to call the API and read results, and to be
    # redirected back to after signing in. Empty by default. Comma-separated:
    #
    #   npx aws-cdk deploy -c proteincad:devOrigins=http://localhost:8321
    #
    # Cognito allows a plain-HTTP callback for `localhost` and nothing else,
    # so http://127.0.0.1:8321 is refused at synth with an explanation rather
    # than twelve minutes into a deploy.
    dev_origins=[piece for piece in context("devOrigins", "").split(",") if piece.strip()],
    app_path=context("appPath", "/proteincad/"),
    # Globally unique per region, so a fresh account deploying this for the
    # first time may find it taken and has to pick another. Changing it on a
    # stack that already exists REPLACES the UserPoolDomain and moves the
    # hosted UI to a new URL -- harmless to the accounts, disruptive to every
    # config.json pointing at the old one. Check before a first deploy:
    #   host proteincad-login.auth.us-east-1.amazoncognito.com
    domain_prefix=context("domainPrefix", "proteincad-login"),
    instance_type=context("instanceType", "g4dn.xlarge"),
    ami_parameter=context("amiParameter", DLAMI),
    root_volume_gb=context("rootVolumeGb", 100, int),
    max_designs=context("maxDesigns", 8, int),
    daily_jobs=context("dailyJobs", 20, int),
    concurrent_jobs=context("concurrentJobs", 2, int),
    global_daily_jobs=context("globalDailyJobs", 100, int),
    machine_starts=context("machineStarts", 5, int),
    global_daily_launches=context("globalDailyLaunches", 20, int),
    idle_minutes=context("idleMinutes", 30, int),
    job_timeout=context("jobTimeout", 3600, int),
    job_memory=context("jobMemory", "12g"),
    budget_usd=context("budgetUsd", 120, int),
    budget_email=context("budgetEmail", ""),
    # Optional, and empty by default: ESM3's weights are public and publishing
    # them needs no token. Set this only if the hub rate-limits the anonymous
    # download, which it does per address. A token is a secret, so this names a
    # Secrets Manager secret rather than carrying one:
    #   -c proteincad:hfTokenSecret=proteincad/hf-token
    # or, for one key of a JSON secret, `proteincad/hf-token:HF_TOKEN`.
    # Naming a secret that does not exist fails the weights build before it runs
    # a command, so set one or the other -- never the name alone.
    hf_token_secret=context("hfTokenSecret", "")
)

def _account():
    """The account to deploy into, or an error that says how to have one.

    The CDK CLI sets CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION in this
    process's environment, filled in from whatever credentials it resolved. If
    it could not resolve any, it sets neither and the only complaint is CDK's
    own -- "Unable to resolve AWS account to use" -- which describes the
    symptom and not one thing you could do about it.
    """
    account = os.environ.get("CDK_DEFAULT_ACCOUNT")
    if account:
        return account
    raise SystemExit("""
No AWS account. The CDK CLI could not work out which one to use, which means
it could not resolve any credentials.

Check what the CLI itself sees:

    aws sts get-caller-identity

  - "Unable to locate credentials" -> there are none configured yet:
        aws configure                     (an access key)
        aws configure sso                 (IAM Identity Center)
  - "The security token ... is expired" or "Error loading SSO Token":
        aws sso login                     (refresh it)
  - it prints an account, but this still fails -> the CLI and CDK are not
    looking at the same profile:
        export AWS_PROFILE=the-one-that-worked

To render the template WITHOUT any credentials -- which creates nothing and is
all `cdk synth` needs -- set them by hand:

    CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1 npx aws-cdk synth
""")


def _region():
    region = (os.environ.get("CDK_DEFAULT_REGION")
              or os.environ.get("AWS_REGION")
              or os.environ.get("AWS_DEFAULT_REGION"))
    if region:
        return region
    raise SystemExit("""
No AWS region. Credentials resolved but no region came with them.

    aws configure set region us-east-1

or for this command only:

    AWS_REGION=us-east-1 npx aws-cdk deploy

The region matters here beyond the usual: it decides where the GPU instance
runs, and the Cognito hosted-UI domain prefix has to be unique within it.
""")


ProteincadStack(
    app,
    context("stackName", "proteincad"),
    settings=settings,
    # From the environment rather than written down, so this file has never
    # seen an account number. `cdk deploy` fills them from your credentials.
    env=cdk.Environment(account=_account(), region=_region()),
    description="proteinCAD: authenticated API, job queue, and an on-demand GPU worker",
)

app.synth()
