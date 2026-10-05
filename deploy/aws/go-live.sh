#!/bin/sh
# Deploy the stack, wire the viewer to it, and push it to the website.
#
#     deploy/aws/go-live.sh
#
# Everything here is either read-only or something you have already decided to
# do; the one step that spends money is `cdk deploy`, and it stops and asks.
# Run it from the proteinCAD checkout. It is safe to re-run: every step either
# no-ops or does the same thing again.
#
# THE ORDER MATTERS, which is the whole reason this exists.
#
# The viewer decides what it is at load time, from web/config.json. With no
# config.json it calls relative `api/` URLs and expects its own Python server
# -- which is right on a laptop and wrong on a website, where nothing answers
# them. So config.json has to be written from the deployed stack's outputs
# BEFORE the folder is copied to the site, or the live page comes up with no
# Account section, no GPU panel, and a Design tab reporting an outage.

set -eu

HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # deploy/aws
ROOT=$(cd -- "$HERE/../.." && pwd)                 # the checkout
STACK=${STACK:-proteincad}
REGION=${REGION:-${AWS_DEFAULT_REGION:-us-east-1}}
SITE=${ADUMBRA_DIR:-$(cd -- "$ROOT/.." && pwd)/adumbra}

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

say "1/5  is the stack deployed?"
if [ -z "$(out ApiUrl)" ]; then
  cat <<EOF
  No stack called '$STACK' in $REGION yet, so there is no API to point at.

  Deploy it first -- this is the step that starts spending money, and it
  prints everything the rest of this script reads:

    cd $HERE/cdk
    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    npx aws-cdk bootstrap
    npx aws-cdk diff
    npx aws-cdk deploy -c proteincad:budgetEmail=you@example.com

  Then build the image and fetch the weights, which take about an hour
  between them and both run inside AWS:

    $HERE/worker/build.sh image
    $HERE/worker/build.sh weights

  Then run this script again.
EOF
  exit 1
fi
printf '  API      %s\n' "$(out ApiUrl)"
printf '  login    %s\n' "$(out LoginDomain)"
printf '  app      %s\n' "$(out AppUrl)"

say "2/5  has an image been built?"
IMAGE=$(aws ssm get-parameter --region "$REGION" --name "$(out ImageParameter)" \
          --query Parameter.Value --output text 2>/dev/null || echo "")
case "$IMAGE" in
  ""|*REPLACE*)
    echo "  ! no image pinned yet. A GPU machine would boot and shut straight down."
    echo "    Run: $HERE/worker/build.sh image"
    echo "    (carrying on -- the site will work, designs will not)" ;;
  *) printf '  %s\n' "$IMAGE" ;;
esac

say "3/5  are the weights in the bucket?"
BUCKET=$(out Bucket)
if aws s3 ls --region "$REGION" "s3://$BUCKET/weights/manifest.json" >/dev/null 2>&1; then
  aws s3 ls --region "$REGION" --recursive --summarize --human-readable \
    "s3://$BUCKET/weights/" | tail -2 | sed 's/^/  /'
else
  echo "  ! no weights/manifest.json in $BUCKET. Every job will fail to fetch a model."
  echo "    Run: $HERE/worker/build.sh weights"
  echo "    (carrying on)"
fi

say "4/5  writing web/config.json from the stack"
CONFIG="$ROOT/web/config.json"
cat > "$CONFIG" <<EOF
{
  "api": "$(out ApiUrl)",
  "auth": {
    "domain": "$(out LoginDomain)",
    "clientId": "$(out ClientId)",
    "redirect": "$(out AppUrl)"
  }
}
EOF
python3 -m json.tool "$CONFIG" | sed 's/^/  /'

# Everything in web/ is downloadable by anyone who loads the page. These four
# values are public identifiers by design -- a Cognito client id is not a
# secret, which is why the client is created without one -- but the check is
# cheap and the failure it catches is permanent.
if grep -qiE '(aws_secret|aws_access|AKIA[0-9A-Z]{16}|private_key|password)' "$CONFIG"; then
  echo "  ! that looks like a credential. Not transferring."
  exit 1
fi

say "5/5  checks, then transfer to $SITE"
ADUMBRA_DIR="$SITE" "$ROOT/tools/push-adumbra.sh"

cat <<EOF

Now commit it, which is what actually publishes:

  cd $SITE
  npm run dev        # look at it first, at /proteincad/
  git add -A public/proteincad
  git commit -m 'proteinCAD: open sign-up, on-demand GPU'
  git push

Then, on the live URL:

  - DESIGN tab, Account section, press Create account
  - a code arrives by email; type it back in
  - press Start GPU machine and watch it come up
  - download RFdiffusion, then run a design

If the Account section is missing on the live site, the config.json above did
not make it -- check that public/proteincad/config.json exists in $SITE.
EOF
