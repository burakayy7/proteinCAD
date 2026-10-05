"""Every AWS resource proteinCAD uses, and nothing it does not.

Read top to bottom, it is the architecture:

    who may ask          Cognito, with self-signup off
    what they may ask    two Lambdas and a waker behind one HTTP API
    where it is written  DynamoDB, one table -- jobs, quotas, and the machine
    what does the work   SQS, and a GPU machine that does not exist most of
                         the time: a launch template, not an instance
    where the models are S3, fetched onto a machine when a job needs them
    where results go     S3, private, reached through presigned links
    how it gets built    CodeBuild, so nothing is built or uploaded from a
                         laptop

Three things are worth a second look. `permissions` is the whole of the IAM in
one place -- and the shape to check is that **nothing may stop or terminate an
instance**, because the machine ends itself and that only works if no
credential can do it instead. `machine` is the only thing here that can cost
real money, and it creates nothing until somebody asks. `builders` is how the
image and the weights get made without leaving AWS.
"""

from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    Aws, CfnOutput, CfnTag, Duration, Fn, RemovalPolicy, Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_budgets as budgets,
    aws_codebuild as codebuild,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_s3_assets as s3assets,
    aws_s3_deployment as s3deploy,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

REPO = Path(__file__).resolve().parents[3]

# The Deep Learning **Base** AMI: the NVIDIA driver, Docker and the container
# toolkit, and none of the frameworks -- which is the whole of what this needs,
# because the frameworks are in the container. Resolved from SSM at deploy
# time, so a deploy picks up the current image rather than an AMI id that rots,
# and so this file has no region in it.
#
# A context value rather than a constant: if the open kernel modules ever
# misbehave on a T4, `base-proprietary-nvidia-driver-gpu-ubuntu-22.04` is a
# one-line change rather than a patch.
DLAMI = ("/aws/service/deeplearning/ami/x86_64/"
         "base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id")

# What goes in the Lambda bundle. The viewer, the sample structures and the
# notebooks are most of this repository and none of them are of any use to a
# Lambda; excluding them keeps the package near a megabyte, which is the
# difference between a cold start you notice and one you do not.
NOT_IN_THE_BUNDLE = [
    ".git", ".git/**", ".github", ".github/**",
    "web", "web/**", "data", "data/**", "notebooks", "notebooks/**",
    "deploy", "deploy/**", "tools", "tools/**",
    "**/__pycache__", "**/*.pyc", "*.md", ".gitignore", ".claude", ".claude/**",
]

# What CodeBuild gets. Wider than the Lambda bundle by exactly two things --
# the Dockerfile and the weights publisher -- because those are what it runs.
# Still no viewer, no sample structures and no notebooks: a build context is
# uploaded on every `cdk deploy`, and there is no reason for it to carry two
# and a half megabytes of three.js.
# How long a machine gets before it ends itself if no worker ever reports.
# The same number as sqs_worker.DEADMAN_MINUTES, which is what pushes it back;
# tools/check_stack.py holds the two together so they cannot drift apart.
DEADMAN_MINUTES = 90

BUILD_CONTEXT = [
    ".git", ".git/**", ".github", ".github/**",
    "web", "web/**", "data", "data/**", "notebooks", "notebooks/**",
    "tools", "tools/**",
    "deploy/aws/cdk/cdk.out", "deploy/aws/cdk/cdk.out/**",
    "deploy/aws/cdk/.venv", "deploy/aws/cdk/.venv/**",
    "**/__pycache__", "**/*.pyc", ".gitignore", ".claude", ".claude/**",
]


@dataclass
class Settings:
    origin: str
    open_sign_up: bool
    dev_origins: list
    app_path: str
    domain_prefix: str
    instance_type: str
    ami_parameter: str
    root_volume_gb: int
    max_designs: int
    daily_jobs: int
    concurrent_jobs: int
    global_daily_jobs: int
    machine_starts: int
    global_daily_launches: int
    idle_minutes: int
    job_timeout: int
    job_memory: str
    budget_usd: int
    budget_email: str
    # Where the weights build finds a Hugging Face token, if it needs one.
    # ESM3's weights are public, so this is empty by default and publishing
    # works without it; it exists because the hub rate-limits anonymous
    # downloads per address. A token is a secret, which must not be in this file
    # or in a `start-build` call that lands in CloudTrail, so this names a
    # Secrets Manager secret instead.
    #
    # The value is a Secrets Manager reference the way CodeBuild spells one:
    # `my-secret` for the whole secret, or `my-secret:HF_TOKEN` for one key of
    # a JSON one.
    hf_token_secret: str = ""

    def __post_init__(self):
        """Refuse a dev origin Cognito would reject, here rather than in the
        middle of a twelve-minute deploy.

        Cognito allows a plain-HTTP callback for exactly one host: `localhost`.
        `http://127.0.0.1:8321` is the same machine and is not the same string,
        and the failure it produces is a CloudFormation error about callback
        URLs that says nothing about why.
        """
        cleaned = []
        for origin in self.dev_origins:
            origin = origin.strip().rstrip("/")
            if not origin:
                continue
            if not origin.startswith(("http://", "https://")):
                raise ValueError(
                    f"devOrigins entry {origin!r} is not an origin. It wants a scheme "
                    "and a host, like http://localhost:8321 -- no path.")
            host = origin.split("://", 1)[1].split("/")[0].split(":")[0]
            if origin.startswith("http://") and host != "localhost":
                raise ValueError(
                    f"devOrigins entry {origin!r} is plain HTTP on {host!r}. Cognito "
                    "allows that for `localhost` and nothing else -- not even "
                    "127.0.0.1, which is the same machine and a different string. "
                    f"Use http://localhost:{origin.rsplit(':', 1)[-1]} instead, or https.")
            cleaned.append(origin)
        self.dev_origins = cleaned

    @property
    def app_url(self) -> str:
        return self.origin.rstrip("/") + "/" + self.app_path.strip("/") + "/"

    @property
    def origins(self) -> list:
        """Every origin allowed to call the API or read a result.

        The site, plus whatever `devOrigins` adds. Empty by default, because an
        origin on this list can make authenticated cross-origin requests with a
        token it obtained -- which is fine for a laptop you control and not
        something to leave switched on.
        """
        return [self.origin] + self.dev_origins

    @property
    def callback_urls(self) -> list:
        """Where Cognito may send somebody back to after signing in.

        A dev origin serves the viewer at its root -- `python3 -m proteincad`
        does -- so its callbacks are the root, not the /proteincad/ subpath the
        site uses.
        """
        urls = [self.app_url, self.app_url + "index.html"]
        for origin in self.dev_origins:
            urls += [origin + "/", origin + "/index.html"]
        return urls

    @property
    def logout_urls(self) -> list:
        return [self.app_url] + [origin + "/" for origin in self.dev_origins]


class ProteincadStack(Stack):
    def __init__(self, scope: Construct, name: str, settings: Settings, **kwargs):
        super().__init__(scope, name, **kwargs)
        self.settings = settings

        self.network()
        self.storage()
        self.identity()
        self.machine()
        self.builders()
        self.functions()
        self.api()
        self.permissions()
        self.budget()
        self.outputs()

    # ------------------------------------------------------------- network

    def network(self) -> None:
        """Three public subnets and no NAT gateway.

        The GPU machine needs to reach out -- SQS, S3, DynamoDB, ECR -- and
        nothing needs to reach in. A private subnet would mean a NAT gateway at
        about thirty-two dollars a month to achieve exactly what a security
        group with no inbound rules already achieves for nothing.
        """
        self.vpc = ec2.Vpc(
            self, "Vpc",
            # Three, because a zone with no g4dn left is an ordinary Tuesday
            # and the answer is another zone. They cost nothing without a NAT
            # gateway, and the launcher walks them in order.
            max_azs=3,
            nat_gateways=0,
            ip_addresses=ec2.IpAddresses.cidr("10.30.0.0/16"),
            subnet_configuration=[ec2.SubnetConfiguration(
                name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24)],
        )

        self.worker_sg = ec2.SecurityGroup(
            self, "WorkerSecurityGroup",
            vpc=self.vpc,
            description="proteinCAD GPU worker: outbound only, nothing inbound",
            allow_all_outbound=True,
        )
        # Deliberately no add_ingress_rule call anywhere in this file. Not SSH,
        # not the old port 8000. The only way to give this box work is the
        # queue. To get a shell on it, use Session Manager.

    # ------------------------------------------------------------- storage

    def storage(self) -> None:
        self.bucket = s3.Bucket(
            self, "Data",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
            # The browser fetches results through a presigned link, which is a
            # cross-origin GET to this bucket. Without this rule the link is
            # valid and the fetch still fails, which reads as an expired link.
            cors=[s3.CorsRule(
                allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                allowed_origins=self.settings.origins,
                allowed_headers=["*"],
                max_age=3000,
            )],
            lifecycle_rules=[
                # A spec is only of interest until its job finishes, and a
                # result until somebody has looked at it. Neither is worth
                # paying to keep for a year.
                #
                # Both are scoped to a prefix, and that is load-bearing:
                # weights/ holds the only copy of the model weights this
                # deployment has, and an expiry rule that reached them would
                # quietly disarm every GPU machine thirty days later. There is
                # a check in tools/check_stack.py that asserts it.
                s3.LifecycleRule(prefix="specs/", expiration=Duration.days(7)),
                s3.LifecycleRule(prefix="results/", expiration=Duration.days(30)),
                # Not an expiry: this cleans up the debris of an upload that
                # died halfway, which is exactly what publishing twelve
                # gigabytes of weights can leave behind.
                s3.LifecycleRule(abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
        )

        # The weights are the one thing in this bucket that cannot be
        # regenerated by re-running something: they came from four different
        # places on the internet, and publishing them again is a manual
        # afternoon. So nothing in the account may delete them -- not the
        # worker, not a Lambda, not CDK's own deployment helper, which is
        # granted DeleteObject on this bucket as a matter of course.
        #
        # Overwriting still works, because re-publishing is a PutObject. Only
        # deletion is refused, and only the account root can lift it.
        self.bucket.add_to_resource_policy(iam.PolicyStatement(
            sid="WeightsAreNotDeletable",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:DeleteObject", "s3:DeleteObjectVersion"],
            resources=[self.bucket.arn_for_objects("weights/*")],
            conditions={"StringNotEquals": {
                "aws:PrincipalArn": f"arn:{Aws.PARTITION}:iam::{Aws.ACCOUNT_ID}:root"}},
        ))

        self.table = dynamodb.Table(
            self, "Jobs",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Job records and the daily quota counters carry `expires`, so old
            # rows leave on their own rather than being swept up by hand.
            time_to_live_attribute="expires",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.table.add_global_secondary_index(
            index_name="by_user",
            partition_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created", type=dynamodb.AttributeType.NUMBER),
            # Everything a listing shows. Projecting all of it would copy the
            # whole record, including the bucket keys, into a second table.
            projection_type=dynamodb.ProjectionType.INCLUDE,
            non_key_attributes=["job_id", "status", "kind", "mode", "model", "progress",
                                "total", "stage", "error", "started", "finished",
                                "target", "hotspots", "designs", "command", "source"],
        )

        self.dead_letters = sqs.Queue(
            self, "DeadLetters",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        self.queue = sqs.Queue(
            self, "JobQueue",
            # Long enough for the worker to claim a job and start beating; it
            # extends this itself while a job runs.
            visibility_timeout=Duration.seconds(900),
            retention_period=Duration.days(1),
            enforce_ssl=True,
            # Standard, not FIFO: the conditional claim in DynamoDB is what
            # stops a job being run twice, and ordering does not matter when
            # one worker takes one job at a time.
            #
            # A job that fails is failed by the worker and its message deleted,
            # so this only ever catches a message that has killed the worker
            # twice -- which is the one thing worth looking at by hand.
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=2, queue=self.dead_letters),
        )

        # The worker's own code, so a machine that exists for twenty minutes
        # has something to run without anybody logging into it. `cdk deploy`
        # ships it; there is no step where you copy a checkout onto a box,
        # because there is no box to copy it onto.
        s3deploy.BucketDeployment(
            self, "WorkerCode",
            sources=[s3deploy.Source.asset(str(REPO), exclude=NOT_IN_THE_BUNDLE),
                     s3deploy.Source.asset(str(REPO / "deploy" / "aws" / "worker"))],
            destination_bucket=self.bucket,
            destination_key_prefix="code/",
            prune=False,
            retain_on_delete=False,
            memory_limit=512,
        )

        # Which image a machine should run, written by the image CodeBuild
        # rather than by this stack -- the digest does not exist until
        # something has been built. A parameter rather than a file on a disk,
        # because there is no disk that outlives a job.
        self.image_pointer = ssm.StringParameter(
            self, "ImagePointer",
            parameter_name="/proteincad/image",
            string_value="REPLACE-ME-WITH-A-DIGEST",
            description="the model image every GPU machine pulls at boot",
        )

        self.images = ecr.Repository(
            self, "ModelImage",
            repository_name="proteincad-model",
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[ecr.LifecycleRule(
                max_image_count=3, description="keep the last three builds")],
        )

    # ------------------------------------------------------------ identity

    def identity(self) -> None:
        """Cognito, open to anybody who can confirm an email address.

        Self-signup is **on** by default: the hosted login page shows a Sign up
        link, and a new account is unusable until Cognito's code has been typed
        back in. That is the only gate on who gets in.
        `-c proteincad:openSignUp=false` closes it and goes back to accounts
        you create yourself.

        Which makes the quotas the whole of the cost control rather than a
        second line of it. Worth knowing the numbers, because they are now what
        stands between an open form and a bill:

            PROTEINCAD_GLOBAL_DAILY_JOBS      100  jobs by everyone, per day
            PROTEINCAD_GLOBAL_DAILY_LAUNCHES   20  machine starts by everyone
            PROTEINCAD_DAILY_JOBS              20  jobs per person
            PROTEINCAD_CONCURRENT_JOBS          2  at once, per person
            PROTEINCAD_MACHINE_STARTS           5  starts per person

        The binding one is the global daily job cap. A hundred jobs is more
        work than a day has hours to run it in, so at the default the machine
        would simply stay up -- see the note in SERVERLESS-DEPLOY.md. Every one
        of these is a context value in cdk.json.

        NOTHING ELSE ABOUT THIS POOL MAY CHANGE. `sign_in_aliases`,
        `standard_attributes` and the username configuration all map to
        CloudFormation properties whose update behaviour is Replacement: edit
        one and the next deploy makes a new pool and deletes every account in
        the old one. `self_sign_up_enabled` maps to
        AdminCreateUserConfig.AllowAdminCreateUserOnly, which updates in place.
        tools/check_stack.py asserts the values of the dangerous four so a
        future edit to them fails a check rather than a deployment.
        """
        self.users = cognito.UserPool(
            self, "Users",
            user_pool_name="proteincad",
            # Closing it again is this one value: it maps to
            # AdminCreateUserConfig.AllowAdminCreateUserOnly, which updates in
            # place, so the pool keeps its identity and every account survives
            # the round trip either way.
            self_sign_up_enabled=self.settings.open_sign_up,
            # Everything from here down is load-bearing for the identity of the
            # pool itself. Leave it alone.
            sign_in_aliases=cognito.SignInAliases(email=True),
            # With self-signup on, this is what makes the address real: Cognito
            # emails a code and the account cannot be used until it comes back.
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)),
            password_policy=cognito.PasswordPolicy(
                min_length=12, require_lowercase=True, require_uppercase=True,
                require_digits=True, require_symbols=False),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            # A user pool is the list of who may use this. Losing it to a
            # `cdk destroy` typed in the wrong terminal is not recoverable.
            removal_policy=RemovalPolicy.RETAIN,
        )

        # A Cognito domain prefix is globally unique **per region**, not per
        # account, so a first deploy into a fresh account may find the name
        # taken and fail here with "Domain already associated with another user
        # pool". The check is a DNS lookup -- an answer means taken, NXDOMAIN
        # means free:
        #
        #   host proteincad-login.auth.us-east-1.amazoncognito.com
        #
        # Which is also a trap: once YOUR stack exists, that name resolves
        # because you own it. Changing this value on a live stack replaces the
        # domain and moves the hosted UI, invalidating every config.json that
        # points at the old one. Pick it once.
        self.login = self.users.add_domain(
            "Hosted",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=self.settings.domain_prefix),
        )

        self.client = self.users.add_client(
            "Web",
            user_pool_client_name="proteincad-web",
            # No secret. This client lives in a static page, where a secret
            # would not be one; the authorization-code flow with PKCE is what
            # makes that safe rather than merely unavoidable.
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL,
                        cognito.OAuthScope.PROFILE],
                callback_urls=self.settings.callback_urls,
                logout_urls=self.settings.logout_urls,
            ),
            # "Incorrect username or password" rather than "no such user", so
            # the login form cannot be used to find out who has an account.
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(8),
            id_token_validity=Duration.hours(8),
            refresh_token_validity=Duration.days(30),
        )

    # ------------------------------------------------------------- machine

    def machine(self) -> None:
        """A launch template, and no instance.

        This is the change that takes the standing bill from seventeen dollars
        a month to about one. There is no stopped instance with a two hundred
        gigabyte disk waiting to be woken: there is a description of a machine,
        and a machine is created from it when somebody needs one and destroyed
        when they do not.

        Which means nothing in this stack can stop or terminate anything. The
        instance ends itself -- `shutdown -h now`, turned into a termination by
        the shutdown behaviour below -- so no role anywhere needs the
        permission, and the worst thing a compromised Lambda could do is start
        a machine that turns itself off half an hour later.
        """
        self.worker_role = iam.Role(
            self, "WorkerRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="proteinCAD GPU worker: one queue, one bucket, one table",
            managed_policies=[
                # For a shell on a machine with no inbound rules and no key
                # pair, when one is needed to find out why a boot failed.
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AmazonSSMManagedInstanceCore"),
            ],
        )
        profile = iam.CfnInstanceProfile(
            self, "WorkerProfile", roles=[self.worker_role.role_name])

        self.launch_template = ec2.CfnLaunchTemplate(
            self, "WorkerLaunchTemplate",
            launch_template_name=f"{self.stack_name}-gpu",
            launch_template_data=ec2.CfnLaunchTemplate.LaunchTemplateDataProperty(
                instance_type=self.settings.instance_type,
                # Resolved when this is deployed, not when it is written: an
                # AMI id hardcoded here would be stale within weeks and wrong
                # in every other region.
                image_id=ec2.MachineImage.from_ssm_parameter(
                    self.settings.ami_parameter).get_image(self).image_id,
                iam_instance_profile=ec2.CfnLaunchTemplate.IamInstanceProfileProperty(
                    arn=profile.attr_arn),
                # A security group id rather than a whole NetworkInterfaces
                # block, so RunInstances can still pick the subnet -- which is
                # how the zone fallback works. The public subnets assign an
                # address on launch, and that is the egress-only public IP.
                security_group_ids=[self.worker_sg.security_group_id],
                metadata_options=ec2.CfnLaunchTemplate.MetadataOptionsProperty(
                    http_endpoint="enabled",
                    http_tokens="required",
                    # Nothing behind the host can ask who this machine is --
                    # and the model container, which has no network at all,
                    # could not reach it even with a larger number here.
                    http_put_response_hop_limit=1,
                ),
                # The whole cost model in one property. `shutdown -h now` from
                # inside ends the instance, its volume and everything
                # downloaded to it; no API call and no permission involved.
                instance_initiated_shutdown_behavior="terminate",
                block_device_mappings=[
                    ec2.CfnLaunchTemplate.BlockDeviceMappingProperty(
                        device_name="/dev/sda1",
                        ebs=ec2.CfnLaunchTemplate.EbsProperty(
                            volume_size=self.settings.root_volume_gb,
                            volume_type="gp3",
                            encrypted=True,
                            # The weights are in S3 now, so this holds an
                            # operating system and nothing else, and it exists
                            # only while the instance does.
                            delete_on_termination=True,
                        ),
                    ),
                ],
                user_data=Fn.base64(self.boot_script()),
                tag_specifications=[
                    ec2.CfnLaunchTemplate.TagSpecificationProperty(
                        resource_type=kind,
                        tags=[CfnTag(key="Name", value="proteincad-gpu"),
                              CfnTag(key="proteincad:role", value="worker")])
                    for kind in ("instance", "volume")
                ],
            ),
        )

    def boot_script(self) -> str:
        """What a brand new machine does with its first ninety seconds.

        In order: put the instance store to work, point Docker at it, pull the
        pinned image, lay the worker down, and start it. Deliberately no
        weights -- those arrive when a job or a button asks for one, which is
        the difference between a machine that boots in a minute and one that
        boots in twenty.
        """
        return f"""#!/bin/bash
set -uxo pipefail
exec > >(tee /var/log/proteincad-boot.log | logger -t proteincad) 2>&1
echo "proteinCAD boot starting"

REGION={Aws.REGION}
BUCKET={self.bucket.bucket_name}
TABLE={self.table.table_name}

# Say what this boot is doing, so the five minutes it takes are five minutes of
# visible progress rather than a blank `booting` that somebody presses Start
# again in the middle of. Also keeps the heartbeat fresh, which is what stops
# the panel deciding the machine never arrived.
STEPS=4
say() {{
  echo "== $2"
  # Through a file rather than an inline argument. This is a bash script inside
  # a Python f-string, so a `\"` here is a `"` by the time bash sees it: the
  # value arrived as {{:s:{{S:booting}}...}}, the CLI rejected it as malformed
  # JSON, and `|| true` swallowed the complaint. Every boot reported nothing,
  # the panel sat on `step 0 of 0` for five minutes, and a machine that was
  # merely slow was indistinguishable from one that had died.
  cat > /tmp/proteincad-say.json <<JSON
{{":s":{{"S":"booting"}},":h":{{"N":"$(date +%s)"}},":g":{{"S":"$2"}},":n":{{"N":"$1"}},":t":{{"N":"$STEPS"}}}}
JSON
  aws dynamodb update-item --region "$REGION" --table-name "$TABLE" \
    --key '{{"pk":{{"S":"MACHINE"}},"sk":{{"S":"STATE"}}}}' \
    --update-expression 'SET #s = :s, #h = :h, #g = :g, #n = :n, #t = :t' \
    --expression-attribute-names \
      '{{"#s":"state","#h":"heartbeat","#g":"stage","#n":"step","#t":"steps"}}' \
    --expression-attribute-values file:///tmp/proteincad-say.json \
    || echo "!! could not report progress (the boot carries on)"
}}

# A heartbeat for the length of the boot.
#
# The steps below are minutes apart -- pulling seven gigabytes is one of them --
# so the record would otherwise go untouched for most of the boot, and the only
# thing keeping the panel from declaring the machine dead was a launch window
# long enough to cover the whole thing. That window is also how long a boot that
# really has died goes unnoticed: terminate a booting instance and the panel
# sits on `booting` until the window runs out.
#
# Beating here separates the two. The steps still say what is happening; this
# says that something is still happening at all.
beat() {{
  while :; do
    cat > /tmp/proteincad-beat.json <<JSON
{{":h":{{"N":"$(date +%s)"}}}}
JSON
    aws dynamodb update-item --region "$REGION" --table-name "$TABLE" \
      --key '{{"pk":{{"S":"MACHINE"}},"sk":{{"S":"STATE"}}}}' \
      --update-expression 'SET #h = :h' \
      --expression-attribute-names '{{"#h":"heartbeat"}}' \
      --expression-attribute-values file:///tmp/proteincad-beat.json >/dev/null 2>&1
    sleep 20
  done
}}
beat &
BEAT=$!

# --- the dead-man switch -----------------------------------------------
# First, before anything that could hang. One thing ends a GPU machine: the
# worker running `shutdown -h now`. Nothing in this account may terminate an
# instance -- that is the property the cost model rests on -- so a boot that
# wedges before the worker starts leaves an instance nothing can stop and the
# panel cannot even see. This is the floor under that: the machine is already
# scheduled to end, from its first seconds, and a living worker is what keeps
# pushing the time out. If no worker ever reports, this is what collects it.
shutdown -h +{DEADMAN_MINUTES} "proteincad: no worker has reported in; ending this machine" || true

# --- the instance store ------------------------------------------------
# Everything heavy lives under /mnt/fast: the docker data-root, twelve
# gigabytes of weights and the job's scratch. What is actually underneath it
# is decided here, and the order matters.
#
# The DLAMI mounts the instance store itself, before any of this runs -- as an
# LVM logical volume at /opt/dlami/nvme. So the device is real, `lsblk` finds
# it, and both of the commands that used to follow fail on it:
#
#     mkfs.ext4 -F /dev/nvme1n1   -> apparently in use by the system
#     mount /dev/nvme1n1 /mnt/fast -> unknown filesystem type 'LVM2_member'
#
# Neither failure was checked, so the boot carried on with /mnt/fast as an
# ordinary directory on the 100 GB root volume -- which the DLAMI has already
# filled to three quarters. The seven-gigabyte image and eight-and-a-half
# gigabytes of ESMFold then go to EBS: slower than the local disk it was
# designed around, and heading for a full root filesystem rather than 116 GB
# of unused NVMe sitting at /opt/dlami/nvme.
#
# So: use the DLAMI's mount where it exists, bind-mounted so every path below
# is unchanged; format the raw device when nothing has claimed it; and if
# neither worked, say so where somebody will see it rather than discovering it
# as a disk-full error inside a model run an hour later.
say 1 "preparing the local disk"
mkdir -p /mnt/fast
if mountpoint -q /opt/dlami/nvme; then
  echo "instance store already mounted by the AMI; binding /mnt/fast onto it"
  mkdir -p /opt/dlami/nvme/proteincad
  mount --bind /opt/dlami/nvme/proteincad /mnt/fast
else
  DEV=$(lsblk -dn -o NAME,MODEL | awk '/Instance Storage/ {{ print "/dev/"$1; exit }}')
  if [ -n "$DEV" ] && mkfs.ext4 -F -L proteincad "$DEV"; then
    mount -o discard,noatime "$DEV" /mnt/fast
  fi
fi

if mountpoint -q /mnt/fast; then
  echo "scratch is on the instance store: $(df -h /mnt/fast | tail -1)"
else
  # Not fatal -- a small job may well fit -- but it is the difference between
  # 116 GB that costs nothing and whatever is left of the root volume, and it
  # is the first thing to look at when a run dies for no visible reason.
  echo "!! /mnt/fast is NOT on the instance store; falling back to the root volume"
  echo "!! $(df -h /mnt/fast | tail -1)"
fi
mkdir -p /mnt/fast/docker /mnt/fast/weights /mnt/fast/work

# --- docker onto it ----------------------------------------------------
# The image is around twenty-five gigabytes unpacked -- it doubled when ESM3
# arrived. On the root volume that is more than the DLAMI leaves free, which is
# exactly how a boot came to die mid-pull; here it is a fifth of a disk that
# costs nothing and dies with the instance.
say 2 "setting up the container runtime"
systemctl stop docker docker.socket containerd 2>/dev/null
mkdir -p /etc/docker
echo '{{"data-root": "/mnt/fast/docker"}}' > /etc/docker/daemon.json
systemctl start docker

# --- the image ---------------------------------------------------------
IMAGE=$(aws ssm get-parameter --region "$REGION" --name {self.image_pointer.parameter_name} \
          --query Parameter.Value --output text)
case "$IMAGE" in
  ""|*REPLACE*|*:latest)
    echo "no pinned image in SSM -- run deploy/aws/worker/build.sh image"
    shutdown -c || true; shutdown -h now; exit 1;;
esac
say 3 "downloading the model software, about 15 GB"
aws ecr get-login-password --region "$REGION" | docker login --username AWS \
  --password-stdin "${{IMAGE%%/*}}"
docker pull "$IMAGE" || {{ echo "image pull failed"; shutdown -c || true; shutdown -h now; exit 1; }}

# --- a way in, for when a boot goes wrong ------------------------------
# The instance role carries AmazonSSMManagedInstanceCore, so
# `aws ssm start-session --target <id>` gets a shell on a machine with no
# inbound rules and no key pair. The DLAMI ships the agent; this only makes
# sure it is running, because a boot that fails after this line is a boot you
# can still go and look at.
systemctl enable --now amazon-ssm-agent 2>/dev/null \
  || snap start amazon-ssm-agent 2>/dev/null \
  || echo "no SSM agent -- there will be no way to get a shell on this machine"


# --- the worker --------------------------------------------------------
say 4 "starting the worker"
id proteincad >/dev/null 2>&1 || useradd --system --create-home \
  --home-dir /opt/proteincad --shell /usr/sbin/nologin proteincad
usermod -aG docker proteincad
aws s3 sync --region "$REGION" "s3://$BUCKET/code/" /opt/proteincad/src
python3 -m pip install --quiet boto3 2>/dev/null

mkdir -p /etc/proteincad
cat > /etc/proteincad/worker.env <<'ENV'
PROTEINCAD_QUEUE={self.queue.queue_url}
PROTEINCAD_TABLE={self.table.table_name}
PROTEINCAD_BUCKET={self.bucket.bucket_name}
PROTEINCAD_REGION={Aws.REGION}
PROTEINCAD_WORK=/mnt/fast/work
PROTEINCAD_WEIGHTS=/mnt/fast/weights
PROTEINCAD_JOB_TIMEOUT={self.settings.job_timeout}
PROTEINCAD_JOB_MEMORY={self.settings.job_memory}
PROTEINCAD_IDLE_MINUTES={self.settings.idle_minutes}
PYTHONPATH=/opt/proteincad/src
ENV
echo "PROTEINCAD_IMAGE=$IMAGE" >> /etc/proteincad/worker.env
# Written down rather than left to the metadata service, because the worker
# stamps it onto every model record and a blank one would make every model
# read as absent forever.
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null)
SELF=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
echo "PROTEINCAD_INSTANCE=$SELF" >> /etc/proteincad/worker.env
chown root:proteincad /etc/proteincad/worker.env
chmod 640 /etc/proteincad/worker.env
chown -R proteincad:proteincad /opt/proteincad /mnt/fast

# The one privileged thing the worker may do, and the only way this machine
# ever ends: no role in the account can terminate an instance.
# /sbin/shutdown rather than one fixed invocation: the worker both ends the
# machine and pushes the dead-man switch back, which is `-c` and `-h +N`. It
# could already turn the instance off, so this widens what it may say and not
# what it may do.
echo 'proteincad ALL=(root) NOPASSWD: /sbin/shutdown' > /etc/sudoers.d/proteincad
chmod 440 /etc/sudoers.d/proteincad

# The worker beats for itself from here, so the boot's own loop stops. Left
# running it would keep the record looking alive after this script has gone,
# which is the one thing the beat exists to prevent.
kill "$BEAT" 2>/dev/null || true

install -m 644 /opt/proteincad/src/proteincad-sqs.service \
  /etc/systemd/system/proteincad-sqs.service
systemctl daemon-reload
systemctl enable --now proteincad-sqs
echo "proteinCAD boot done"
"""

    # ------------------------------------------------------------ builders

    def builders(self) -> None:
        """Two things get built once, and neither should be built on a laptop.

        The model image is about seven gigabytes and is mostly a CUDA base
        plus torch; the weights are twelve and a half gigabytes fetched from
        four places on the internet and put in a bucket. Doing either from home
        means a long upload over a domestic connection, and building the image
        on an Apple Silicon machine produces an arm64 image that a g4dn cannot
        run at all.

        So both run in CodeBuild, in the same region as the things they are
        building for: the image is pushed to ECR without leaving AWS, and the
        weights go from the internet into S3 over a link that is not yours.
        Neither project runs on a schedule or a trigger -- they are started by
        hand, twice in the life of a deployment.
        """
        # The build context, uploaded by `cdk deploy` to the CDK staging
        # bucket. That is what makes the version CodeBuild builds the version
        # you deployed, rather than whatever is on a branch somewhere.
        context = s3assets.Asset(self, "BuildContext", path=str(REPO),
                                 exclude=BUILD_CONTEXT)
        source = codebuild.Source.s3(bucket=context.bucket,
                                     path=context.s3_object_key)
        registry = f"{Aws.ACCOUNT_ID}.dkr.ecr.{Aws.REGION}.amazonaws.com"

        # -- the model image ------------------------------------------------
        self.image_build = codebuild.Project(
            self, "ImageBuild",
            project_name=f"{self.stack_name}-image",
            description="builds the model image for linux/amd64 and pins the digest",
            source=source,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                # x86_64 by definition, which is the platform a g4dn runs.
                # `--platform` below says so again rather than relying on it.
                compute_type=codebuild.ComputeType.LARGE,
                # Docker needs it. The only thing this project builds is our
                # own Dockerfile from our own context.
                privileged=True,
            ),
            environment_variables={
                "REPO_URI": codebuild.BuildEnvironmentVariable(
                    value=self.images.repository_uri),
                "REGISTRY": codebuild.BuildEnvironmentVariable(value=registry),
                "IMAGE_PARAM": codebuild.BuildEnvironmentVariable(
                    value=self.image_pointer.parameter_name),
                "REPO_NAME": codebuild.BuildEnvironmentVariable(
                    value=self.images.repository_name),
            },
            # The torch and DGL install is most of it, and it is not quick.
            timeout=Duration.minutes(90),
            queued_timeout=Duration.hours(1),
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "pre_build": {"commands": [
                        'echo "building for linux/amd64 into $REPO_URI"',
                        "aws ecr get-login-password --region $AWS_REGION"
                        " | docker login --username AWS --password-stdin $REGISTRY",
                    ]},
                    "build": {"commands": [
                        # --platform, explicitly. The build host is x86_64, so
                        # this changes nothing today -- but saying it means the
                        # same buildspec run anywhere else, including on an
                        # Apple Silicon laptop, still produces an image a g4dn
                        # can actually run rather than an arm64 one it cannot.
                        #
                        # CODEBUILD_BUILD_NUMBER rather than a shell variable
                        # set in an earlier phase: it is always defined, and it
                        # does not depend on state carrying across phases.
                        # Nothing reads the tag anyway -- the machine is pinned
                        # to the digest.
                        "docker build --platform linux/amd64"
                        " -f deploy/aws/worker/Dockerfile"
                        ' -t "$REPO_URI:build-$CODEBUILD_BUILD_NUMBER" .',
                    ]},
                    "post_build": {"commands": [
                        'docker push "$REPO_URI:build-$CODEBUILD_BUILD_NUMBER"',
                        "DIGEST=$(aws ecr describe-images --region $AWS_REGION"
                        ' --repository-name "$REPO_NAME"'
                        ' --image-ids imageTag="build-$CODEBUILD_BUILD_NUMBER"'
                        " --query 'imageDetails[0].imageDigest' --output text)",
                        # Pinned by digest, never by tag. A machine created on
                        # demand must not be able to pull whatever happened to
                        # be pushed since somebody chose.
                        'aws ssm put-parameter --region $AWS_REGION --name "$IMAGE_PARAM"'
                        ' --type String --overwrite --value "$REPO_URI@$DIGEST"',
                        'echo "pinned $REPO_URI@$DIGEST"',
                    ]},
                },
            }),
        )
        self.images.grant_pull_push(self.image_build)
        self.image_pointer.grant_write(self.image_build)
        self.image_build.add_to_role_policy(iam.PolicyStatement(
            actions=["ecr:DescribeImages"], resources=[self.images.repository_arn]))

        # -- the weights ----------------------------------------------------
        self.weights_build = codebuild.Project(
            self, "WeightsBuild",
            project_name=f"{self.stack_name}-weights",
            description="fetches the model weights from source into the bucket",
            source=source,
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                # Twelve and a half gigabytes down and the same back up. LARGE
                # is for the disk and the network rather than the CPU.
                compute_type=codebuild.ComputeType.LARGE,
            ),
            environment_variables={
                "BUCKET": codebuild.BuildEnvironmentVariable(
                    value=self.bucket.bucket_name),
                # Overridable at start-build time, so one model can be
                # re-published without fetching the others:
                #   --environment-variables-override name=ONLY,value=esmfold
                "ONLY": codebuild.BuildEnvironmentVariable(value=""),
                # Read from Secrets Manager at build time rather than carried
                # here. Absent when no secret is configured, so the publisher
                # refuses ESM3 with the message that names the licence -- which
                # is the truth about why it cannot fetch them.
                **({"HF_TOKEN": codebuild.BuildEnvironmentVariable(
                    value=self.settings.hf_token_secret,
                    type=codebuild.BuildEnvironmentVariableType.SECRETS_MANAGER)}
                   if self.settings.hf_token_secret else {}),
            },
            timeout=Duration.hours(2),
            queued_timeout=Duration.hours(1),
            build_spec=codebuild.BuildSpec.from_object({
                "version": "0.2",
                "phases": {
                    "install": {
                        "runtime-versions": {"python": "3.11"},
                        "commands": [
                            # The only dependency in this whole deployment, and
                            # it is installed on a machine that is deleted
                            # twenty minutes later.
                            "pip install --quiet boto3 huggingface_hub",
                        ],
                    },
                    "build": {"commands": [
                        'if [ -n "$ONLY" ]; then EXTRA="--only $ONLY"; else EXTRA=""; fi',
                        "python3 deploy/aws/worker/publish-weights.py"
                        ' --bucket "$BUCKET" --region "$AWS_REGION"'
                        " --keep /tmp/weights $EXTRA",
                    ]},
                },
            }),
        )
        # Put and read, and nothing else. The bucket policy separately denies
        # deletion under weights/ to everything in the account, so a bad run
        # can overwrite but cannot destroy.
        self.weights_build.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject", "s3:GetObject"],
            resources=[self.bucket.arn_for_objects("weights/*")]))

        # And the token, when one is configured.
        #
        # CodeBuild resolves a SECRETS_MANAGER environment variable itself,
        # before the build container starts and before any command runs -- so
        # without this the build fails in under a minute having produced no log
        # events at all, which is a far more confusing failure than a missing
        # permission usually is. Declaring the variable is not enough: CDK
        # cannot infer the ARN from a name, so it grants nothing.
        if self.settings.hf_token_secret:
            self.weights_build.add_to_role_policy(iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[self.hf_secret_arn(self.settings.hf_token_secret)]))

    @staticmethod
    def hf_secret_arn(reference: str) -> str:
        """The secret an `hfTokenSecret` reference names, as an ARN to grant on.

        CodeBuild's own format is `secret-id:json-key:version-stage:version-id`,
        all but the first optional -- so the secret is whatever precedes the
        first colon. A full ARN is allowed there too and contains colons of its
        own, which is why that case is split on differently rather than at the
        first one.

        The trailing `-*` is not laziness: Secrets Manager appends six random
        characters to every secret's ARN, so a name is not enough to write the
        ARN out and a wildcard is the only way to name one secret by name.
        """
        reference = reference.strip()
        if reference.startswith("arn:"):
            # arn:aws:secretsmanager:region:account:secret:name[-suffix]
            parts = reference.split(":")
            return ":".join(parts[:7]) if len(parts) > 7 else reference
        name = reference.split(":", 1)[0]
        return (f"arn:{Aws.PARTITION}:secretsmanager:{Aws.REGION}:"
                f"{Aws.ACCOUNT_ID}:secret:{name}-*")

    # ----------------------------------------------------------- functions

    def functions(self) -> None:
        code = lambda_.Code.from_asset(str(REPO), exclude=NOT_IN_THE_BUNDLE)

        def make(name, which, timeout=30, memory=512):
            return lambda_.Function(
                self, name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="proteincad.cloud_api.handler",
                code=code,
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                environment=self.lambda_environment(which),
            )

        self.read = make("Api", "api")
        # A spec can be a few megabytes, and the fold route reads a backbone
        # out of the bucket and joins it to the target before it queues
        # anything. More memory here is also more CPU, which is most of why
        # this is not the default 128.
        self.submit = make("Submit", "submit", timeout=60, memory=1024)
        # There is no third function any more. `GET /machine` reads a DynamoDB
        # record and nothing else -- there is no instance to ask about most of
        # the time -- so it belongs with the other reads.
        self.waker = lambda_.Function(
            self, "Waker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="proteincad.cloud_api.waker",
            code=code,
            timeout=Duration.seconds(60),
            memory_size=512,
            environment=self.lambda_environment("submit"),
            description="launches a machine if the queue is not empty and there is none",
        )
        events.Rule(
            self, "WakerSchedule",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(self.waker)],
            description="a safety net: submitting a job normally starts one itself",
        )

    def lambda_environment(self, which: str) -> dict:
        """Everything a function is configured with. One place, because the
        waker and the API have to agree about the machine down to the timeout
        they report."""
        return {
            # Which half of the router this copy serves. The IAM role grants
            # exactly what those routes need, so the two lists can be read
            # against each other.
            "PROTEINCAD_FUNCTION": which,
            "PROTEINCAD_TABLE": self.table.table_name,
            "PROTEINCAD_BUCKET": self.bucket.bucket_name,
            "PROTEINCAD_QUEUE": self.queue.queue_url,
            "PROTEINCAD_REGION": Aws.REGION,
            "PROTEINCAD_LAUNCH_TEMPLATE": self.launch_template.ref,
            # In order, and tried in order when a zone has no capacity.
            "PROTEINCAD_SUBNETS": ",".join(
                subnet.subnet_id for subnet in self.vpc.public_subnets),
            "PROTEINCAD_INSTANCE_TYPE": self.settings.instance_type,
            "PROTEINCAD_MAX_DESIGNS": str(self.settings.max_designs),
            "PROTEINCAD_DAILY_JOBS": str(self.settings.daily_jobs),
            "PROTEINCAD_CONCURRENT_JOBS": str(self.settings.concurrent_jobs),
            "PROTEINCAD_GLOBAL_DAILY_JOBS": str(self.settings.global_daily_jobs),
            "PROTEINCAD_MACHINE_STARTS": str(self.settings.machine_starts),
            "PROTEINCAD_GLOBAL_DAILY_LAUNCHES": str(self.settings.global_daily_launches),
            "PROTEINCAD_IDLE_MINUTES": str(self.settings.idle_minutes),
        }

    # ----------------------------------------------------------------- api

    def api(self) -> None:
        guard = authorizers.HttpUserPoolAuthorizer(
            "Jwt", self.users, user_pool_clients=[self.client])

        self.http = apigw.HttpApi(
            self, "Http",
            api_name="proteincad",
            # The site, and nothing else unless `devOrigins` says otherwise.
            # Ordinary development needs no entry here at all: it runs
            # `python3 -m proteincad`, where the API is on the same origin as
            # the page and CORS never enters into it. `devOrigins` is for the
            # narrower case of pointing a local viewer at the *deployed* API.
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=self.settings.origins,
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST,
                               apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["content-type", "authorization"],
                max_age=Duration.hours(1),
            ),
            default_authorizer=guard,
        )

        def routes(function, paths, method=apigw.HttpMethod.GET, authorizer=None):
            for path in paths:
                self.http.add_routes(
                    path=path,
                    methods=[method],
                    integration=integrations.HttpLambdaIntegration(
                        "To" + function.node.id + path.replace("/", "").replace("{", "")
                        .replace("}", "").title(), function),
                    **({"authorizer": authorizer} if authorizer else {}),
                )

        # The one open route. Without it the viewer cannot tell "there is no
        # server" from "you are not signed in", and those have different fixes.
        routes(self.read, ["/health"], authorizer=apigw.HttpNoneAuthorizer())
        routes(self.read, ["/design/options", "/jobs", "/jobs/{job_id}",
                           "/jobs/{job_id}/designs/{index}"])
        routes(self.read, ["/jobs/{job_id}/cancel"], method=apigw.HttpMethod.POST)
        routes(self.submit, ["/design", "/jobs/{job_id}/designs/{index}/fold"],
               method=apigw.HttpMethod.POST)
        routes(self.read, ["/machine"])
        routes(self.submit, ["/machine/start", "/machine/retire",
                             "/machine/models/{name}"],
               method=apigw.HttpMethod.POST)

        # A second ceiling under the per-user quotas, for the case where the
        # quota check itself is what is being hammered.
        stage = self.http.default_stage.node.default_child
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=5, throttling_burst_limit=10)

    # --------------------------------------------------------- permissions

    def permissions(self) -> None:
        """The whole of the IAM, in one place, one paragraph per role.

        The shape to check first: **nothing here may stop or terminate an
        instance.** Not the API, not the launcher, not the machine itself. A
        GPU machine ends because it runs `shutdown -h now` and the launch
        template turns that into a termination -- which is a mechanism no
        credential can reach.

        One caveat before reading the rest as least privilege:
        ec2:DescribeInstances does not support resource-level permissions, and
        AWS requires "*" for it. It is not used by the API at all any more --
        the DynamoDB record is what the panel reads -- so it appears here only
        where an instance genuinely has to look itself up.
        """
        template_arn = self.format_arn(
            service="ec2", resource="launch-template",
            resource_name=self.launch_template.ref)

        def ec2_arn(kind, name="*", account=None):
            return self.format_arn(service="ec2", resource=kind, resource_name=name,
                                   account=account if account is not None else Aws.ACCOUNT_ID)

        def in_bucket(prefix):
            return self.bucket.arn_for_objects(prefix)

        def may(who, actions, resources, conditions=None):
            """Written out rather than using grant_read/grant_put.

            The convenience grants are generous on purpose -- grant_read adds
            s3:ListBucket, which would let a function enumerate every key in
            the bucket, and grant_put adds the object-lock and tagging writes.
            None of that is used here, so none of it is granted.
            """
            iam.Grant.add_to_principal(
                grantee=who, actions=actions, resource_arns=resources,
                conditions=conditions or {})

        def may_launch(who):
            """The right to create one machine, from one template, at one size.

            RunInstances is authorised separately against every resource it
            touches -- the instance, its volume, its network interface, the
            subnet, the security group, the image -- and **the condition keys
            available differ per resource**. `ec2:InstanceType` is in the
            request context only for the `instance` resource. Requiring it on
            the others means a StringEquals against a key that is not there,
            which is false, which denies the launch:

                not authorized to perform: ec2:RunInstances on resource:
                arn:aws:ec2:...:network-interface/* because no identity-based
                policy allows the ec2:RunInstances action

            So the conditions go where they are evaluable, and that is enough.
            Every launch must be authorised against `instance/*` as well as
            everything else, so pinning the template and the size *there*
            binds the whole call: there is no RunInstances that gets past the
            first statement with a different template or a bigger instance,
            whatever the supporting resources allow.

            The supporting resources are still held to this account and this
            region by their ARNs, and there is no `ec2:CreateSecurityGroup` or
            `ec2:CreateSubnet` anywhere, so they can only be the ones this
            stack made.
            """
            # The one that binds: template and size, both evaluable here.
            may(who, ["ec2:RunInstances"], [ec2_arn("instance")],
                conditions={
                    "ArnEquals": {"ec2:LaunchTemplate": template_arn},
                    "StringEquals": {"ec2:InstanceType": self.settings.instance_type},
                })
            # What a launch touches on the way. Unconditioned because the keys
            # to condition them on are not offered for these resource types --
            # and unnecessary, because the statement above has already decided
            # whether this launch may happen at all.
            may(who, ["ec2:RunInstances"], [
                template_arn,
                ec2_arn("volume"), ec2_arn("network-interface"),
                ec2_arn("subnet"), ec2_arn("security-group"),
                ec2_arn("image", account=""),
            ])
            # So a machine can be found in the console, and so a runaway one
            # can be told apart from everything else in the account.
            may(who, ["ec2:CreateTags"], [ec2_arn("instance"), ec2_arn("volume")],
                conditions={"StringEquals": {"ec2:CreateAction": "RunInstances"}})
            # Handing the instance role to the instance. Scoped to the one role
            # and the one service, because iam:PassRole on a wildcard is a
            # privilege-escalation path rather than a permission.
            may(who, ["iam:PassRole"], [self.worker_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}})

        # -- submit: the routes that cost money ----------------------------
        may(self.submit, ["s3:PutObject"], [in_bucket("specs/*")])
        may(self.submit, ["s3:GetObject"], [in_bucket("results/*")])
        self.table.grant(self.submit, "dynamodb:PutItem", "dynamodb:UpdateItem",
                         "dynamodb:GetItem", "dynamodb:Query")
        self.queue.grant_send_messages(self.submit)
        may_launch(self.submit)

        # -- api: reads, a cancel flag, and the machine record --------------
        may(self.read, ["s3:GetObject"], [in_bucket("results/*")])
        self.table.grant(self.read, "dynamodb:GetItem", "dynamodb:Query",
                         "dynamodb:UpdateItem")
        # No EC2 of any kind. It could not create, start, stop or destroy a
        # machine if it tried; all it can do is read what one wrote down.

        # -- waker: the five-minute safety net ------------------------------
        self.table.grant(self.waker, "dynamodb:GetItem", "dynamodb:UpdateItem",
                         "dynamodb:Query")
        may(self.waker, ["sqs:GetQueueAttributes"], [self.queue.queue_arn])
        may_launch(self.waker)

        # -- the machine itself ---------------------------------------------
        self.queue.grant_consume_messages(self.worker_role)
        may(self.worker_role, ["s3:GetObject"],
            [in_bucket("specs/*"), in_bucket("weights/*"), in_bucket("code/*")])
        may(self.worker_role, ["s3:ListBucket"], [self.bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["code/*", "weights/*"]}})
        may(self.worker_role, ["s3:PutObject"], [in_bucket("results/*")])
        self.table.grant(self.worker_role, "dynamodb:GetItem", "dynamodb:UpdateItem",
                         "dynamodb:Query")
        self.images.grant_pull(self.worker_role)
        self.image_pointer.grant_read(self.worker_role)
        # And that is the whole list. It cannot terminate itself through the
        # API -- it does not need to, because `shutdown -h now` is not an API
        # call and the launch template does the rest.

    # -------------------------------------------------------------- budget

    def budget(self) -> None:
        """A number you chose, and an email when it is passed.

        Not a safety mechanism -- a budget cannot stop anything -- but the
        difference between finding out in a day and finding out in a month.
        """
        if not self.settings.budget_email:
            return
        subscriber = budgets.CfnBudget.SubscriberProperty(
            address=self.settings.budget_email, subscription_type="EMAIL")
        budgets.CfnBudget(
            self, "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="proteincad-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=self.settings.budget_usd, unit="USD"),
                # Measure what was spent, not what was billed.
                #
                # By default a budget counts credits against the total, so an
                # account with promotional credit on it reports near zero
                # however hard the GPU is working -- and the alert arrives the
                # month the credit runs out, describing a month you cannot
                # change. Excluding them means the number tracks usage from the
                # first day. Refunds are excluded for the same reason: a refund
                # for something unrelated should not quietly raise the ceiling.
                cost_types=budgets.CfnBudget.CostTypesProperty(
                    include_credit=False,
                    include_refund=False,
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL", comparison_operator="GREATER_THAN",
                        threshold=80, threshold_type="PERCENTAGE"),
                    subscribers=[subscriber]),
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="FORECASTED", comparison_operator="GREATER_THAN",
                        threshold=100, threshold_type="PERCENTAGE"),
                    subscribers=[subscriber]),
            ],
        )

    # ------------------------------------------------------------- outputs

    def outputs(self) -> None:
        login = f"https://{self.settings.domain_prefix}.auth.{Aws.REGION}.amazoncognito.com"

        for name, value, description in [
            ("ApiUrl", self.http.api_endpoint, "web/config.json: `api`"),
            ("UserPoolId", self.users.user_pool_id, "see the AddUser output"),
            ("ClientId", self.client.user_pool_client_id, "web/config.json: auth.clientId"),
            ("LoginDomain", login, "web/config.json: auth.domain"),
            ("Bucket", self.bucket.bucket_name,
             "specs, results and the model weights"),
            ("QueueUrl", self.queue.queue_url, "what the GPU machine drains"),
            ("DeadLetterQueue", self.dead_letters.queue_url, "check this when a job vanishes"),
            ("Table", self.table.table_name, "the job and machine records"),
            ("LaunchTemplate", self.launch_template.ref,
             "the GPU machine's description; no instance exists until one is needed"),
            ("ImageParameter", self.image_pointer.parameter_name,
             "the image build writes the digest here; machines read it at boot"),
            ("AppUrl", self.settings.app_url,
             "where the viewer is served from; the `redirect` in web/config.json"),
            ("EcrRepository", self.images.repository_uri, "CodeBuild pushes here"),
            ("ImageBuildProject", self.image_build.project_name,
             "builds the model image for linux/amd64; see deploy/aws/worker/build.sh"),
            ("WeightsBuildProject", self.weights_build.project_name,
             "fetches ~12.5 GB of weights into the bucket, inside AWS"),
        ]:
            CfnOutput(self, name, value=value, description=description)

        # The two files you have to fill in, ready to paste. Getting one of
        # these wrong by hand is the most likely way this deploy goes wrong,
        # and it fails much later, as "nothing is answering".
        CfnOutput(
            self, "ConfigJson",
            description="web/config.json",
            value=(
                '{"api": "' + self.http.api_endpoint + '", "auth": {'
                '"domain": "' + login + '", '
                '"clientId": "' + self.client.user_pool_client_id + '", '
                '"redirect": "' + self.settings.app_url + '"}}'
            ),
        )
        # There is no worker.env to fill in any more -- a machine writes its
        # own at boot from the launch template -- and nothing left that has to
        # be built or uploaded from your own computer.
        CfnOutput(
            self, "BuildEverything",
            description="run these two once; neither needs Docker or your upload speed",
            value=("deploy/aws/worker/build.sh image  &&  "
                   "deploy/aws/worker/build.sh weights"),
        )

        # Who may use this, which on this deployment is a list you keep by
        # hand rather than a code anybody can pass on. Self-signup is off, so
        # this command is the whole of the admissions policy.
        CfnOutput(
            self, "AddUser",
            description="the only way somebody gets an account; run it per person",
            value=(
                "aws cognito-idp admin-create-user"
                f" --user-pool-id {self.users.user_pool_id}"
                " --username EMAIL --desired-delivery-mediums EMAIL"
                " --user-attributes Name=email,Value=EMAIL Name=email_verified,Value=true"
            ),
        )
        CfnOutput(
            self, "RemoveUser",
            description="revoking access; their queued jobs still run",
            value=("aws cognito-idp admin-delete-user"
                   f" --user-pool-id {self.users.user_pool_id} --username EMAIL"),
        )
