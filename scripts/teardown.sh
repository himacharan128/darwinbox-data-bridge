#!/usr/bin/env bash
# Remove every AWS resource this project created. Nothing else is touched:
# every name is prefixed dbx-, so the blast radius is legible.
set -uo pipefail
REGION=${AWS_REGION:-ap-south-1}
say() { printf '  %-46s %s\n' "$1" "$2"; }

echo "Tearing down in $REGION"

aws ecs update-service --cluster dbx-data-bridge --service dbx-console \
  --desired-count 0 --region "$REGION" >/dev/null 2>&1 && say "service scaled to zero" ok
aws ecs delete-service --cluster dbx-data-bridge --service dbx-console --force \
  --region "$REGION" >/dev/null 2>&1 && say "service deleted" ok

LISTENERS=$(aws elbv2 describe-load-balancers --names dbx-console-alb \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null)
if [ -n "$LISTENERS" ] && [ "$LISTENERS" != "None" ]; then
  aws elbv2 delete-load-balancer --load-balancer-arn "$LISTENERS" >/dev/null 2>&1 \
    && say "load balancer deleted" ok
  sleep 20   # the ENIs must release before the security groups will go
fi
TG=$(aws elbv2 describe-target-groups --names dbx-console-tg \
     --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null)
[ -n "$TG" ] && [ "$TG" != "None" ] && aws elbv2 delete-target-group \
  --target-group-arn "$TG" >/dev/null 2>&1 && say "target group deleted" ok

aws ecs delete-cluster --cluster dbx-data-bridge --region "$REGION" >/dev/null 2>&1 \
  && say "cluster deleted" ok

for sg in dbx-alb-sg dbx-data-bridge-sg; do
  ID=$(aws ec2 describe-security-groups --filters Name=group-name,Values=$sg \
       --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
  [ -n "$ID" ] && [ "$ID" != "None" ] && aws ec2 delete-security-group --group-id "$ID" \
    >/dev/null 2>&1 && say "security group $sg deleted" ok
done

aws ecr delete-repository --repository-name dbx-data-bridge --force \
  --region "$REGION" >/dev/null 2>&1 && say "ecr repository deleted" ok
aws logs delete-log-group --log-group-name /ecs/dbx-data-bridge \
  --region "$REGION" >/dev/null 2>&1 && say "log group deleted" ok

aws iam delete-role-policy --role-name dbx-ecs-task --policy-name dbx-bedrock-invoke >/dev/null 2>&1
aws iam delete-role --role-name dbx-ecs-task >/dev/null 2>&1 && say "task role deleted" ok
aws iam detach-role-policy --role-name dbx-ecs-execution \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null 2>&1
aws iam delete-role --role-name dbx-ecs-execution >/dev/null 2>&1 && say "execution role deleted" ok

echo
echo "The dbx-migration-agent IAM user and its Bedrock key are left alone —"
echo "they are for local development, not this deployment. Remove with:"
echo "  aws iam delete-user-policy --user-name dbx-migration-agent --policy-name dbx-bedrock-invoke"
echo "  aws iam delete-service-specific-credential --user-name dbx-migration-agent --service-specific-credential-id <id>"
echo "  aws iam delete-user --user-name dbx-migration-agent"
