#!/bin/sh
# Build the model image, or fetch the weights. Both happen inside AWS.
#
#     deploy/aws/worker/build.sh image      about 25 minutes
#     deploy/aws/worker/build.sh weights    about 40 minutes
#     deploy/aws/worker/build.sh weights esmfold     just that one
#     deploy/aws/worker/build.sh weights esm3        the second engine, 5.5 GB
#
# ESM3's weights are public and need no token. `weights` with no model named
# leaves them out, because they are 5.5 GB that a deployment using only
# RFdiffusion has no use for -- so ask for them by name.
#
# If the hub ever rate-limits the anonymous download, a token helps: put one in
# Secrets Manager and name it at deploy time, which is optional and off by
# default:
#
#     cdk deploy -c proteincad:hfTokenSecret=proteincad/hf-token
#
# Neither needs Docker on this machine, and neither uploads anything from it.
# The image is built for linux/amd64 and pushed straight to ECR; the weights
# are fetched from their official sources straight into the bucket. The only
# thing that leaves here is a `start-build` call.
#
# That matters twice over. Building the image on an Apple Silicon laptop
# produces an arm64 image, which a g4dn cannot run at all -- and the weights
# are twelve and a half gigabytes each way, which over a domestic connection is
# most of an evening.
#
# Both projects read the build context `cdk deploy` uploaded, so what they
# build is the version you deployed. If you have changed the Dockerfile or the
# publisher since, `cdk deploy` again first.

set -eu

WHAT=${1:-}
ONLY=${2:-}
STACK=${STACK:-proteincad}
REGION=${REGION:-${AWS_DEFAULT_REGION:-us-east-1}}

case "$WHAT" in
  image)   KEY=ImageBuildProject ;;
  weights) KEY=WeightsBuildProject ;;
  *) echo "usage: $0 image|weights [model]"; exit 1 ;;
esac

project=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$KEY'].OutputValue" --output text)
[ -n "$project" ] && [ "$project" != "None" ] || {
  echo "no $KEY output on stack $STACK in $REGION -- has it been deployed?"; exit 1
}

set -- --region "$REGION" --project-name "$project"
if [ -n "$ONLY" ]; then
  [ "$WHAT" = "weights" ] || { echo "a model name only applies to: $0 weights"; exit 1; }
  set -- "$@" --environment-variables-override "name=ONLY,value=$ONLY,type=PLAINTEXT"
  echo "fetching only: $ONLY"
fi

id=$(aws codebuild start-build "$@" --query 'build.id' --output text)
echo "started $id"
echo "  logs: aws logs tail /aws/codebuild/$project --follow"
echo

# Poll rather than `codebuild wait`, which does not exist. A minute between
# looks: these take tens of minutes and a tighter loop only costs API calls.
while :; do
  status=$(aws codebuild batch-get-builds --region "$REGION" --ids "$id" \
    --query 'builds[0].buildStatus' --output text)
  phase=$(aws codebuild batch-get-builds --region "$REGION" --ids "$id" \
    --query 'builds[0].currentPhase' --output text)
  case "$status" in
    IN_PROGRESS) printf '  %s  %s\n' "$(date -u +%H:%M:%S)" "$phase"; sleep 60 ;;
    SUCCEEDED)   break ;;
    *) printf '\n%s: %s in phase %s\n' "$project" "$status" "$phase"
       echo "  aws logs tail /aws/codebuild/$project --since 2h"
       exit 1 ;;
  esac
done

printf '\n%s succeeded.\n\n' "$project"
if [ "$WHAT" = "image" ]; then
  param=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='ImageParameter'].OutputValue" --output text)
  pinned=$(aws ssm get-parameter --region "$REGION" --name "$param" \
    --query Parameter.Value --output text)
  cat <<EOF
Every GPU machine created from now on pulls:

  $pinned

There is nothing to restart. A machine running right now keeps the image it
booted with; the next one picks this up.
EOF
else
  bucket=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='Bucket'].OutputValue" --output text)
  aws s3 ls --region "$REGION" --summarize --human-readable --recursive \
    "s3://$bucket/weights/" | tail -3
fi
