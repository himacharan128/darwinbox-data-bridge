#!/usr/bin/env bash
# Build, push and roll out. Idempotent: run it after any change.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION=${AWS_REGION:-ap-south-1}
ACCT=$(aws sts get-caller-identity --query Account --output text)
REPO=dbx-data-bridge
IMAGE="$ACCT.dkr.ecr.$REGION.amazonaws.com/$REPO"
TAG=${1:-v$(date +%Y%m%d%H%M)}

echo "==> building $IMAGE:$TAG (arm64)"
docker build --platform linux/arm64 -t "$IMAGE:$TAG" -t "$IMAGE:latest" .

echo "==> pushing"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCT.dkr.ecr.$REGION.amazonaws.com"
docker push "$IMAGE:$TAG"
docker push "$IMAGE:latest"

echo "==> registering task definition"
python3 - "$IMAGE:$TAG" "$ACCT" <<'PY'
import json, subprocess, sys, pathlib
image, account = sys.argv[1], sys.argv[2]
# The checked-in task definition carries a placeholder, so no account id is
# published in the repo. The real one comes from whoever is deploying.
raw = pathlib.Path("infra/taskdef.json").read_text().replace("ACCOUNT_ID", account)
spec = json.loads(raw)
for container in spec["containerDefinitions"]:
    container["image"] = image
pathlib.Path("/tmp/dbx-taskdef.json").write_text(json.dumps(spec))
subprocess.run(["aws", "ecs", "register-task-definition",
                "--cli-input-json", "file:///tmp/dbx-taskdef.json",
                "--query", "taskDefinition.revision", "--output", "text"], check=True)
PY

echo "==> rolling out"
aws ecs update-service --cluster dbx-data-bridge --service dbx-console \
  --task-definition dbx-data-bridge --force-new-deployment \
  --region "$REGION" --query 'service.status' --output text

DNS=$(aws elbv2 describe-load-balancers --names dbx-console-alb \
      --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)
[ -n "$DNS" ] && echo "==> http://$DNS"
