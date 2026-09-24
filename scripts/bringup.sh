#!/usr/bin/env bash
# Rebuild everything teardown.sh removes, from nothing, in one command.
#
# teardown.sh deletes the deployment but nothing put it back: the cluster, roles,
# network, EFS volume and load balancer were a dozen commands typed once and
# written up as prose. This is those commands, so shutting down is reversible.
#
#   ./scripts/bringup.sh          # build the infrastructure, then deploy
#
# Everything is named dbx-* and lands in the default VPC. Re-running is safe:
# each step checks for what it is about to create.
set -uo pipefail
cd "$(dirname "$0")/.."

REGION=${AWS_REGION:-ap-south-1}
ACCT=$(aws sts get-caller-identity --query Account --output text)
say() { printf '  %-44s %s\n' "$1" "$2"; }

echo "Building in $REGION"

# --- network -----------------------------------------------------------------
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true \
      --query 'Vpcs[0].VpcId' --output text --region "$REGION")
SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values="$VPC" \
          --query 'Subnets[*].SubnetId' --output text --region "$REGION" | tr '\t' ' ')
read -r SUB_A SUB_B _ <<<"$SUBNETS"
say "vpc $VPC" ok

sg_id() { aws ec2 describe-security-groups --filters Name=group-name,Values="$1" \
          Name=vpc-id,Values="$VPC" --query 'SecurityGroups[0].GroupId' \
          --output text --region "$REGION" 2>/dev/null; }

ALB_SG=$(sg_id dbx-alb-sg)
if [ "$ALB_SG" = "None" ] || [ -z "$ALB_SG" ]; then
  ALB_SG=$(aws ec2 create-security-group --group-name dbx-alb-sg --vpc-id "$VPC" \
           --description "dbx console load balancer" --query GroupId --output text --region "$REGION")
  aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" --protocol tcp \
    --port 80 --cidr 0.0.0.0/0 --region "$REGION" >/dev/null
fi
say "security group dbx-alb-sg" "$ALB_SG"

APP_SG=$(sg_id dbx-data-bridge-sg)
if [ "$APP_SG" = "None" ] || [ -z "$APP_SG" ]; then
  APP_SG=$(aws ec2 create-security-group --group-name dbx-data-bridge-sg --vpc-id "$VPC" \
           --description "dbx console tasks" --query GroupId --output text --region "$REGION")
  # the app is only reachable through the balancer, and NFS only from itself
  aws ec2 authorize-security-group-ingress --group-id "$APP_SG" --protocol tcp \
    --port 8080 --source-group "$ALB_SG" --region "$REGION" >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$APP_SG" --protocol tcp \
    --port 2049 --source-group "$APP_SG" --region "$REGION" >/dev/null
fi
say "security group dbx-data-bridge-sg" "$APP_SG"

# --- state volume ------------------------------------------------------------
EFS=$(aws efs describe-file-systems --query \
      "FileSystems[?Name=='dbx-data-bridge'].FileSystemId | [0]" --output text --region "$REGION")
if [ "$EFS" = "None" ] || [ -z "$EFS" ]; then
  EFS=$(aws efs create-file-system --performance-mode generalPurpose \
        --tags Key=Name,Value=dbx-data-bridge --query FileSystemId --output text --region "$REGION")
  until [ "$(aws efs describe-file-systems --file-system-id "$EFS" \
            --query 'FileSystems[0].LifeCycleState' --output text --region "$REGION")" = available ]; do
    sleep 5
  done
  for S in "$SUB_A" "$SUB_B"; do
    aws efs create-mount-target --file-system-id "$EFS" --subnet-id "$S" \
      --security-groups "$APP_SG" --region "$REGION" >/dev/null 2>&1
  done
fi
say "efs volume" "$EFS"

# --- roles -------------------------------------------------------------------
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if ! aws iam get-role --role-name dbx-ecs-execution >/dev/null 2>&1; then
  aws iam create-role --role-name dbx-ecs-execution --assume-role-policy-document "$TRUST" >/dev/null
  aws iam attach-role-policy --role-name dbx-ecs-execution \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null
fi
say "role dbx-ecs-execution" ok

if ! aws iam get-role --role-name dbx-ecs-task >/dev/null 2>&1; then
  aws iam create-role --role-name dbx-ecs-task --assume-role-policy-document "$TRUST" >/dev/null
  # exactly the two models the agent may call, and nothing else
  aws iam put-role-policy --role-name dbx-ecs-task --policy-name dbx-bedrock-invoke \
    --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":["arn:aws:bedrock:*::foundation-model/openai.gpt-oss-120b-1:0","arn:aws:bedrock:*::foundation-model/openai.gpt-oss-20b-1:0"]},{"Effect":"Allow","Action":"bedrock:CallWithBearerToken","Resource":"*"}]}' >/dev/null
fi
say "role dbx-ecs-task" ok

# --- registry, cluster, logs -------------------------------------------------
aws ecr describe-repositories --repository-names dbx-data-bridge --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name dbx-data-bridge --region "$REGION" >/dev/null
say "ecr repository" ok

aws ecs describe-clusters --clusters dbx-data-bridge --region "$REGION" \
  --query 'clusters[0].status' --output text 2>/dev/null | grep -q ACTIVE \
  || aws ecs create-cluster --cluster-name dbx-data-bridge --region "$REGION" >/dev/null
say "ecs cluster" ok

aws logs create-log-group --log-group-name /ecs/dbx-data-bridge --region "$REGION" >/dev/null 2>&1
aws logs put-retention-policy --log-group-name /ecs/dbx-data-bridge \
  --retention-in-days 7 --region "$REGION" >/dev/null 2>&1
say "log group" ok

# --- balancer ----------------------------------------------------------------
TG=$(aws elbv2 describe-target-groups --names dbx-console-tg \
     --query 'TargetGroups[0].TargetGroupArn' --output text --region "$REGION" 2>/dev/null)
if [ "$TG" = "None" ] || [ -z "$TG" ]; then
  TG=$(aws elbv2 create-target-group --name dbx-console-tg --protocol HTTP --port 8080 \
       --vpc-id "$VPC" --target-type ip --health-check-path /api/health \
       --health-check-interval-seconds 30 --healthy-threshold-count 2 \
       --query 'TargetGroups[0].TargetGroupArn' --output text --region "$REGION")
fi
say "target group" ok

ALB=$(aws elbv2 describe-load-balancers --names dbx-console-alb \
      --query 'LoadBalancers[0].LoadBalancerArn' --output text --region "$REGION" 2>/dev/null)
if [ "$ALB" = "None" ] || [ -z "$ALB" ]; then
  ALB=$(aws elbv2 create-load-balancer --name dbx-console-alb --type application \
        --scheme internet-facing --subnets "$SUB_A" "$SUB_B" --security-groups "$ALB_SG" \
        --query 'LoadBalancers[0].LoadBalancerArn' --output text --region "$REGION")
  aws elbv2 create-listener --load-balancer-arn "$ALB" --protocol HTTP --port 80 \
    --default-actions Type=forward,TargetGroupArn="$TG" --region "$REGION" >/dev/null
fi
say "load balancer" ok

# --- image and service -------------------------------------------------------
echo "==> building and pushing the image"
./scripts/deploy.sh >/dev/null 2>&1 || true   # first run has no service yet; it just pushes

TD=$(python3 - "$ACCT" "$EFS" <<'PY'
import json, pathlib, subprocess, sys
raw = (pathlib.Path("infra/taskdef.json").read_text()
       .replace("ACCOUNT_ID", sys.argv[1]).replace("EFS_ID", sys.argv[2]))
spec = json.loads(raw)
pathlib.Path("/tmp/dbx-td.json").write_text(json.dumps(spec))
print(subprocess.run(["aws", "ecs", "register-task-definition",
                      "--cli-input-json", "file:///tmp/dbx-td.json",
                      "--query", "taskDefinition.taskDefinitionArn", "--output", "text"],
                     capture_output=True, text=True).stdout.strip())
PY
)
say "task definition" "${TD##*/}"

if ! aws ecs describe-services --cluster dbx-data-bridge --services dbx-console \
     --region "$REGION" --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs create-service --cluster dbx-data-bridge --service-name dbx-console \
    --task-definition "$TD" --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUB_A,$SUB_B],securityGroups=[$APP_SG],assignPublicIp=ENABLED}" \
    --load-balancers "targetGroupArn=$TG,containerName=api,containerPort=8080" \
    --health-check-grace-period-seconds 90 --region "$REGION" >/dev/null
fi
say "service" ok

DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB" \
      --query 'LoadBalancers[0].DNSName' --output text --region "$REGION")
echo
echo "==> http://$DNS"
echo "    The name is new, so update the README and the write-up."
echo "    Run data does not come back with it: reimport from the backup if you kept one."
