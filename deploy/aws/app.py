"""OPTIONAL AWS deployment (AWS CDK v2, Python). Skip this folder if you deploy with Docker Compose.

What it creates:
  * S3 bucket for profiles and playbooks (MAXEL_STORE=s3)
  * ECS Fargate cluster in a 2-AZ VPC with public subnets only (no NAT gateway cost)
  * API service behind an Application Load Balancer
  * One Fargate service per BAND agent role (desired count 1: one process per agent ID)
  * Reads keys from an existing Secrets Manager secret (JSON) named by `secretName`

Before `cdk deploy`, create the secret:
  aws secretsmanager create-secret --name maxel/studio --secret-string file://secret.json
with keys: FLEXPRICE_API_KEY, ANTHROPIC_API_KEY, MAXEL_ADMIN_TOKEN and the ten
<ROLE>_AGENT_ID / <ROLE>_API_KEY values.

Deploy:
  cd deploy/aws && pip install -r requirements.txt && cdk bootstrap && cdk deploy
"""

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as patterns
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as sm
from constructs import Construct

ROLES = {
    "strategist": "STRATEGIST",
    "market_analyst": "MARKET_ANALYST",
    "pricing_architect": "PRICING_ARCHITECT",
    "messaging_writer": "MESSAGING_WRITER",
    "launch_planner": "LAUNCH_PLANNER",
}


class MaxelStudioStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, *, secret_name: str, **kw) -> None:
        super().__init__(scope, cid, **kw)

        bucket = s3.Bucket(
            self, "Data",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        secret = sm.Secret.from_secret_name_v2(self, "Keys", secret_name)
        vpc = ec2.Vpc(
            self, "Vpc", max_azs=2, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC)],
        )
        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, container_insights=True)
        image = ecs.ContainerImage.from_asset("../..")  # repo root Dockerfile

        env = {
            "MAXEL_STORE": "s3",
            "MAXEL_S3_BUCKET": bucket.bucket_name,
            "MAXEL_METER": "flexprice",  # containers are ephemeral: FlexPrice is the system of record
            "MAXEL_LLM": "anthropic",
            "MAXEL_DATA_DIR": "/app/var",
        }
        common_secrets = {
            k: ecs.Secret.from_secrets_manager(secret, k)
            for k in ("FLEXPRICE_API_KEY", "ANTHROPIC_API_KEY", "MAXEL_ADMIN_TOKEN")
        }

        api = patterns.ApplicationLoadBalancedFargateService(
            self, "Api",
            cluster=cluster, cpu=512, memory_limit_mib=1024, desired_count=1,
            assign_public_ip=True, public_load_balancer=True,
            task_image_options=patterns.ApplicationLoadBalancedTaskImageOptions(
                image=image, container_port=8080, environment=env, secrets=common_secrets,
                command=["maxel", "serve", "--port", "8080"],
            ),
        )
        api.target_group.configure_health_check(path="/health")
        bucket.grant_read_write(api.task_definition.task_role)

        for role, prefix in ROLES.items():
            task = ecs.FargateTaskDefinition(self, f"{prefix.title()}Task", cpu=256, memory_limit_mib=512)
            task.add_container(
                "agent",
                image=image,
                command=["maxel", "band", "run", "--role", role],
                environment=env,
                secrets={
                    **common_secrets,
                    f"{prefix}_AGENT_ID": ecs.Secret.from_secrets_manager(secret, f"{prefix}_AGENT_ID"),
                    f"{prefix}_API_KEY": ecs.Secret.from_secrets_manager(secret, f"{prefix}_API_KEY"),
                },
                logging=ecs.LogDrivers.aws_logs(stream_prefix=f"maxel-{role}"),
            )
            bucket.grant_read_write(task.task_role)
            ecs.FargateService(
                self, f"{prefix.title()}Service",
                cluster=cluster, task_definition=task, desired_count=1, assign_public_ip=True,
                # never run two copies of the same BAND agent ID
                min_healthy_percent=0, max_healthy_percent=100,
            )

        cdk.CfnOutput(self, "ApiUrl", value=f"http://{api.load_balancer.load_balancer_dns_name}")
        cdk.CfnOutput(self, "DataBucket", value=bucket.bucket_name)


app = cdk.App()
MaxelStudioStack(app, "MaxelGtmStudio", secret_name=app.node.try_get_context("secretName") or "maxel/studio")
app.synth()
