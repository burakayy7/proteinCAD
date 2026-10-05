#!/bin/sh
# What is actually deployed, and what is left to do.
#
#     deploy/aws/status.sh
#
# Read-only. It creates nothing, changes nothing and costs nothing. Written
# because "I think I deployed it" is a hard thing to check by hand across a
# stack, a parameter, a bucket, a file on disk and a folder in another
# repository -- and because a half-finished deploy looks exactly like a
# finished one until somebody presses a button on the live site.

set -eu

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../.." && pwd)
STACK=${STACK:-proteincad}
REGION=${REGION:-${AWS_DEFAULT_REGION:-us-east-1}}
SITE=${ADUMBRA_DIR:-$(cd -- "$ROOT/.." && pwd)/adumbra}

yes() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
no()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
note() { printf '    %s\n' "$1"; }

out() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

printf '\nproteinCAD — stack "%s" in %s\n\n' "$STACK" "$REGION"

# Which profile, and whether it works. Worth doing properly: an SSO login
# populates the profile you named when you configured it, while everything
# here -- and the CDK CLI -- uses `default` unless told otherwise. The two
# being different produces "not signed in" on a machine you just signed in on.
WHO=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "")
if [ -n "$WHO" ]; then
  yes "signed in as $WHO"
  note "profile: ${AWS_PROFILE:-default}"
else
  no "the AWS CLI is not signed in as ${AWS_PROFILE:-default}"
  WORKING=""
  for profile in $(aws configure list-profiles 2>/dev/null); do
    [ "$profile" = "${AWS_PROFILE:-default}" ] && continue
    if arn=$(AWS_PROFILE="$profile" aws sts get-caller-identity                --query Arn --output text 2>/dev/null); then
      WORKING="$profile"
      note "but the '$profile' profile works: $arn"
    fi
  done
  if [ -n "$WORKING" ]; then
    note ""
    note "Use it for this shell -- cdk reads the same variable:"
    note "  export AWS_PROFILE=$WORKING"
    note ""
    note "Or make it the default, so nothing has to remember:"
    note "  aws configure set --profile $WORKING region \$(aws configure get region --profile $WORKING)"
    note "  # then edit ~/.aws/config and rename [profile $WORKING] to [default]"
  else
    note "No profile works. Sign in:"
    note "  aws sso login --profile <name>     (if it is SSO)"
    note "  aws configure                      (access key)"
    note ""
    note "A profile with an access key id and no secret also reads as"
    note "'not signed in'. Check ~/.aws/credentials for a half-written block."
  fi
  exit 1
fi

# --- 1. the stack ----------------------------------------------------------
STATUS=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "")
case "$STATUS" in
  "")
    no "step 1: no stack. Nothing has been created."
    note "cd $HERE/cdk && npx aws-cdk deploy -c proteincad:budgetEmail=you@example.com"
    note "If that says 'Subprocess exited with error 1', the venv is the cause:"
    note "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 0 ;;
  *ROLLBACK*|*FAILED*)
    no "step 1: the stack exists but is $STATUS"
    note "It did not finish. The reason is the first FAILED line in:"
    note "  aws cloudformation describe-stack-events --region $REGION \\"
    note "    --stack-name $STACK --max-items 40 \\"
    note "    --query 'StackEvents[?ResourceStatus==\`CREATE_FAILED\`].[LogicalResourceId,ResourceStatusReason]' --output table"
    note "A ROLLBACK_COMPLETE stack has to be deleted before it can be retried:"
    note "  aws cloudformation delete-stack --region $REGION --stack-name $STACK"
    exit 0 ;;
  *) yes "step 1: stack is $STATUS" ;;
esac
note "api    $(out ApiUrl)"
note "login  $(out LoginDomain)"
note "app    $(out AppUrl)"

# --- 2. the image ----------------------------------------------------------
PARAM=$(out ImageParameter)
IMAGE=$(aws ssm get-parameter --region "$REGION" --name "$PARAM" \
          --query Parameter.Value --output text 2>/dev/null || echo "")
case "$IMAGE" in
  ""|*REPLACE*)
    no "step 2: no model image pinned"
    note "A GPU machine would boot and shut straight back down."
    note "$HERE/worker/build.sh image        (~25 min, inside AWS)" ;;
  *) yes "step 2: image pinned"
     note "$IMAGE" ;;
esac

# --- 3. the weights --------------------------------------------------------
BUCKET=$(out Bucket)
if aws s3 ls --region "$REGION" "s3://$BUCKET/weights/manifest.json" >/dev/null 2>&1; then
  SIZE=$(aws s3 ls --region "$REGION" --recursive --summarize --human-readable \
           "s3://$BUCKET/weights/" 2>/dev/null | sed -n 's/.*Total Size: *//p')
  yes "step 3: weights published (${SIZE:-size unknown})"
else
  no "step 3: no weights/manifest.json in $BUCKET"
  note "Every job would fail to fetch a model."
  note "$HERE/worker/build.sh weights      (~40 min, inside AWS)"
fi

# --- 4. the viewer's config ------------------------------------------------
CONFIG="$ROOT/web/config.json"
if [ -f "$CONFIG" ] && grep -q "$(out ClientId)" "$CONFIG" 2>/dev/null; then
  yes "step 4: web/config.json matches this stack"
elif [ -f "$CONFIG" ]; then
  no "step 4: web/config.json exists but is for a different stack"
  note "$HERE/go-live.sh   rewrites it from the outputs"
else
  no "step 4: no web/config.json"
  note "Without it the live site has no Account section and a dead Design tab."
  note "$HERE/go-live.sh"
fi

# --- 5. the site -----------------------------------------------------------
if [ -f "$SITE/public/proteincad/config.json" ]; then
  if diff -q "$CONFIG" "$SITE/public/proteincad/config.json" >/dev/null 2>&1; then
    yes "step 5: the site has the same config.json"
  else
    no "step 5: the site's config.json differs from web/config.json"
    note "$HERE/go-live.sh   then commit in $SITE"
  fi
elif [ -d "$SITE/public/proteincad" ]; then
  no "step 5: the site has proteinCAD but no config.json"
  note "That is the version with no sign-in. $HERE/go-live.sh"
else
  no "step 5: nothing at $SITE/public/proteincad"
fi

# --- and the thing that costs money ---------------------------------------
printf '\n'
RUNNING=$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:proteincad:role,Values=worker \
            Name=instance-state-name,Values=pending,running \
  --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null || echo "")
if [ -n "$RUNNING" ] && [ "$RUNNING" != "None" ]; then
  printf '  \033[33m●\033[0m a GPU machine is running right now: %s\n' "$RUNNING"
  note "about \$0.54/hour. It stops itself after the idle timeout."
else
  yes "no GPU machine running — nothing is costing more than pennies"
fi

POOL=$(out UserPoolId)
if [ -n "$POOL" ] && [ "$POOL" != "None" ]; then
  COUNT=$(aws cognito-idp list-users --region "$REGION" --user-pool-id "$POOL" \
            --query 'length(Users)' --output text 2>/dev/null || echo "?")
  note "$COUNT account(s) signed up so far"
fi
printf '\n'
