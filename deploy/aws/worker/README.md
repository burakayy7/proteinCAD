# The GPU machine

The worker that drains the queue, and the container it runs the model in.

There is no box here to log into, and nothing here is built on your computer. A
machine is created from a launch template when somebody needs one and destroyed
when they stop needing it; the image and the weights are made by CodeBuild,
inside AWS; and everything else is baked into the image or read from S3 at
boot.

Nothing reaches a machine from the internet. Its security group has no inbound
rules at all — not port 22, not port 8000, nothing. The only way to give it work
is to put a message on the queue, and the only thing that can do that is the
submit Lambda, on behalf of somebody who signed in.

Full instructions are in [../SERVERLESS-DEPLOY.md](../SERVERLESS-DEPLOY.md).
This file is the short version and the caveats.

## The files

| | |
|---|---|
| `Dockerfile` | the model image: python, torch, dgl, the two checkouts. **No weights.** |
| `model_runner.py` | what runs inside it. An adapter around `colab_worker.py`, not a second copy. |
| `proteincad-sqs.service` | systemd unit, `Restart=always`, installed by user data |
| `build.sh` | starts either CodeBuild and waits. `build.sh image` / `build.sh weights` |
| `publish-weights.py` | what the weights build runs: fetch from source, hash, upload |

## Building it

```sh
deploy/aws/worker/build.sh image      # ~25 min: docker build, push, pin the digest
deploy/aws/worker/build.sh weights    # ~40 min: 12.5 GB from source into the bucket
deploy/aws/worker/build.sh weights esmfold    # just one model
deploy/aws/worker/build.sh weights esm3       # the second engine, 5.5 GB
```

Both run in CodeBuild rather than locally, for two different reasons.

**The image, because of the platform.** A `docker build` on an Apple Silicon
laptop produces an `arm64` image, and a `g4dn` cannot run one at all — the
failure is a container that exits instantly with `exec format error`, on a
machine that terminates itself fifteen seconds later. The buildspec passes
`--platform linux/amd64` explicitly, and `tools/check_stack.py` asserts that it
does.

**The weights, because of the size.** Twelve and a half gigabytes down and the
same back up is an evening on a domestic connection and forty minutes on a build
host beside the bucket, where the upload never leaves AWS.

Both projects build the context that `cdk deploy` uploaded, so what they build
is the version you deployed. Change the Dockerfile or the publisher, and
`cdk deploy` before rebuilding.

## What a machine does with its first ninety seconds

The boot script is in the CDK stack (`machine()` in
[`../cdk/proteincad_stack.py`](../cdk/proteincad_stack.py)), because it is part
of the launch template rather than a file anybody installs. In order:

1. Find the instance store — by `lsblk … | grep 'Instance Storage'`, not by
   guessing `/dev/nvme1n1`, because on Nitro the EBS root is NVMe too — format
   it, mount it at `/mnt/fast`.
2. Point Docker's data root at it. The image is 7 GB unpacked; on the root
   volume that would be most of it, and here it is 6% of a disk that costs
   nothing and dies with the instance.
3. Make sure the SSM agent is running, so there is a way in if the next step
   fails. Then read the pinned image digest from SSM, log in to ECR, and pull.
4. Sync the worker from `s3://…/code/`, write `worker.env`, install the unit,
   and a sudoers line allowing exactly `/sbin/shutdown -h now`.
5. Start the worker.

**No weights.** That is the difference between a machine that is useful in
ninety seconds and one that is useful in twenty minutes. Weights arrive because
somebody pressed Download, or because a job needs one and nobody did.

## What the container may do

```
--network none          no network. Not a firewall rule; no interface at all.
--read-only             no writes outside the mounts
--cap-drop ALL          no capabilities
--security-opt no-new-privileges
--memory 12g            of the instance's 16 GiB
--pids-limit 512
-v /mnt/fast/work/<job>:rw                            the only writable path
-v /mnt/fast/weights/rfdiffusion:ro                   checkpoints
-v /mnt/fast/weights/proteinmpnn:ro                   sequence weights
-v /mnt/fast/weights/esmfold:ro                       folding weights
-v /mnt/fast/weights/esm3:ro                         ESM3, as a hub cache
```

It has no AWS credentials. It cannot reach the metadata service, because it has
no network to reach it over — and the launch template's hop limit of one means
it could not even if it had.

Each set of weights is mounted where its own tool already looks, so nothing has
to be told a new path: RFdiffusion reads `<repo>/models`, ProteinMPNN reads
`<repo>/vanilla_model_weights`, and ESMFold is handed a directory instead of a
Hugging Face repo id. That last one is why there is no HF cache anywhere in
this design: a directory needs no cache layout, no symlinks and no lock files.

> ProteinMPNN checks its weights into its own git repository, so the image build
> deletes them after cloning. Two copies of a weights file where only one is
> mounted over is exactly how a machine ends up running weights nobody
> published and nobody can name.

## Three things worth knowing

**The worker user is in the `docker` group, which is root-equivalent on the
host.** Anyone who can run `docker` can mount the host filesystem into a
container. The container is locked down; the process that launches it is not.
A deliberate trade for a prototype, and written here so it is a decision rather
than an oversight. It matters less than it did: the host now lives for minutes
and holds an IAM role scoped to one queue, one bucket and one table.

**A failed job is not retried.** A container that exits non-zero or hits the
time limit fails the job and the message is deleted. The only retry is the one
worth having: the worker dying mid-job, the visibility timeout lapsing, and SQS
handing the message back. So the dead letter queue collects exactly one kind of
thing — a message that has killed the worker twice.

**A weight that does not match its hash fails the model.** Loudly, with both
hashes, and the file is deleted rather than kept. A truncated checkpoint loads
without complaint and produces designs that mean nothing, which is a far worse
failure than one that stops.

## Getting a shell

There is no SSH and no key pair — the security group has no inbound rules at
all. The instance role carries `AmazonSSMManagedInstanceCore`, so Session
Manager works instead, and the boot script starts the agent before anything
that might fail:

```sh
aws ec2 describe-instances \
  --filters Name=tag:proteincad:role,Values=worker \
            Name=instance-state-name,Values=pending,running \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,Placement.AvailabilityZone]' \
  --output table

aws ssm start-session --target i-…
```

Once in:

```sh
sudo cat /var/log/proteincad-boot.log     # the ninety seconds above
journalctl -u proteincad-sqs -f           # the worker
docker ps                                  # is a job running
ls -la /mnt/fast/weights                   # which models have arrived
df -h /mnt/fast                            # the instance store
nvidia-smi                                 # the card
```

There is usually nothing to connect to. A machine exists only while somebody is
designing, and it will shut itself down under you after the idle timeout —
your session dying is that working, not a fault.

## When something is wrong

| what you see | what it is |
|---|---|
| the worker exits at once, "will not run `latest`" | the SSM parameter holds a tag. Re-run `build.sh image`. |
| a job fails on a missing checkpoint | the weights mount is empty and the download failed — check the machine record |
| the machine goes away mid-session | that is `PROTEINCAD_IDLE_MINUTES`, and it is the only cost control |
| the machine never goes away | the worker is not running, or it cannot read the queue depth. Look now. |
| jobs sit queued and nothing starts | no machine — check the `proteincad-Waker` logs, which try every five minutes |
