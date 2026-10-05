#!/usr/bin/env bash
#
# Turn a fresh GPU instance into a proteinCAD worker that comes back by itself
# after every stop, and stops itself when nothing needs it.
#
#     sudo ./setup.sh                    # everything, about an hour
#     sudo ./setup.sh --skip-models      # just the services, to test the wiring
#     sudo ./setup.sh --weights all      # fetch all 8 RFdiffusion checkpoints
#
# Run it once, by hand, on an instance you launched from an Ubuntu 22.04 GPU
# AMI with the NVIDIA driver already on it. Everything it installs lives on the
# root volume, which survives a stop -- so this is a one-time cost, and every
# later start is a sixty-second boot.
#
# Idempotent: re-running only does what is still missing. Safe to run again
# after changing worker.env or pulling a newer colab_worker.py.

set -euo pipefail

HOME_DIR=/opt/proteincad
ENV_FILE=/etc/proteincad/worker.env
USER_NAME=proteincad
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

worker=""
weights=core
skip_models=0

while [ $# -gt 0 ]; do
  case "$1" in
    --worker) worker=$2; shift 2 ;;
    --weights) weights=$2; shift 2 ;;
    --skip-models) skip_models=1; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }

# The worker is one standalone file with no proteinCAD imports, which is what
# makes this possible: copy it and nothing else.
if [ -z "$worker" ]; then
  for candidate in "$HERE/colab_worker.py" "$HERE/../../proteincad/colab_worker.py"; do
    [ -f "$candidate" ] && { worker=$candidate; break; }
  done
fi
if [ -z "$worker" ] || [ ! -f "$worker" ]; then
  echo "cannot find colab_worker.py. Copy it next to this script, or pass" >&2
  echo "  --worker /path/to/colab_worker.py" >&2
  exit 1
fi

say() { printf '\n=== %s\n' "$1"; }

# Ask the instance about itself. Newer instances default to IMDSv2, where an
# unauthenticated read gets a 401 rather than an answer, so take a token first
# and fall back to IMDSv1 for older ones.
imds() {
  local token
  token=$(curl -fsS --max-time 2 -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)
  curl -fsS --max-time 2 ${token:+-H "X-aws-ec2-metadata-token: $token"} \
    "http://169.254.169.254/latest/meta-data/$1" 2>/dev/null || true
}

say "1/6  system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-dev git curl build-essential

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "  ! no nvidia-smi on this machine. RFdiffusion needs a GPU with a driver;"
  echo "    relaunch from a GPU AMI that has one (see deploy/aws/README.md)."
  echo "    Carrying on so the services get installed, but jobs will fail."
else
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | sed 's/^/  /'
fi

say "2/6  the proteincad user and its home"
id -u "$USER_NAME" >/dev/null 2>&1 || \
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$HOME_DIR"
install -o "$USER_NAME" -g "$USER_NAME" -m 0644 "$worker" "$HOME_DIR/colab_worker.py"
chown -R "$USER_NAME:$USER_NAME" "$HOME_DIR"

say "3/6  settings"
mkdir -p "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
  install -m 0640 "$HERE/worker.env.example" "$ENV_FILE"
  # A token nobody has to think of, generated once and kept. The port is open
  # to whoever the security group allows; without this, so is the GPU.
  token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  sed -i "s|^PROTEINCAD_WORKER_TOKEN=.*|PROTEINCAD_WORKER_TOKEN=$token|" "$ENV_FILE"
  echo "  wrote $ENV_FILE with a fresh token"
else
  echo "  keeping the $ENV_FILE that is already here"
fi
chown root:"$USER_NAME" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

say "4/6  python environment"
if [ "${RFDIFFUSION_PYTHON}" = "$HOME_DIR/env/bin/python" ] && [ ! -x "$RFDIFFUSION_PYTHON" ]; then
  sudo -u "$USER_NAME" python3 -m venv "$HOME_DIR/env"
  sudo -u "$USER_NAME" "$HOME_DIR/env/bin/pip" install --quiet --upgrade pip setuptools wheel
fi
[ -x "$RFDIFFUSION_PYTHON" ] || {
  echo "  ! RFDIFFUSION_PYTHON=$RFDIFFUSION_PYTHON is not executable. Point it at the" >&2
  echo "    interpreter the models should run in, in $ENV_FILE." >&2
  exit 1
}
echo "  $("$RFDIFFUSION_PYTHON" -V) at $RFDIFFUSION_PYTHON"

say "5/6  the models"
if [ "$skip_models" -eq 1 ]; then
  echo "  skipped. Run this later, as the proteincad user:"
  echo "    sudo -u $USER_NAME $RFDIFFUSION_PYTHON $HOME_DIR/colab_worker.py setup \\"
  echo "        --python $RFDIFFUSION_PYTHON --rfdiffusion $RFDIFFUSION_DIR \\"
  echo "        --proteinmpnn $PROTEINMPNN_DIR"
else
  echo "  this downloads several GB and takes the best part of an hour"
  sudo -u "$USER_NAME" "$RFDIFFUSION_PYTHON" "$HOME_DIR/colab_worker.py" setup \
    --python "$RFDIFFUSION_PYTHON" \
    --rfdiffusion "$RFDIFFUSION_DIR" \
    --proteinmpnn "$PROTEINMPNN_DIR" \
    --weights "$weights" || echo "  ! setup reported problems; see above"
fi

say "6/6  services"
install -m 0755 "$HERE/proteincad-idle.sh" /usr/local/bin/proteincad-idle
install -m 0644 "$HERE/proteincad-worker.service" /etc/systemd/system/
install -m 0644 "$HERE/proteincad-idle.service" /etc/systemd/system/
install -m 0644 "$HERE/proteincad-idle.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now proteincad-worker.service
systemctl enable --now proteincad-idle.timer
systemctl restart proteincad-worker.service

sleep 3
if curl -fsS --max-time 10 "http://127.0.0.1:${PROTEINCAD_WORKER_PORT}/health" >/dev/null; then
  health=ok
else
  health="not answering yet — journalctl -u proteincad-worker -n 50"
fi

cat <<EOF

Done. The worker is enabled, so it comes back on its own every time this
instance starts, and proteincad-idle.timer powers the machine off after
${PROTEINCAD_IDLE_LIMIT}s with nothing to do.

  worker on port ${PROTEINCAD_WORKER_PORT}: ${health}
  token: $(sed -n 's/^PROTEINCAD_WORKER_TOKEN=//p' "$ENV_FILE")

On the machine that runs proteinCAD:

  export PROTEINCAD_EC2_INSTANCE=$(imds instance-id || true)
  export PROTEINCAD_EC2_REGION=$(imds placement/region || true)
  export PROTEINCAD_EC2_TOKEN=$(sed -n 's/^PROTEINCAD_WORKER_TOKEN=//p' "$ENV_FILE")
  python3 -m proteincad

Then stop this instance -- the app starts it again when it needs it.
EOF
