"""Network: private-only VPC, no NAT, VPC endpoints for AWS services."""

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_logs as logs
from config import EnvConfig, resource_name
from constructs import Construct


class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=resource_name(cfg, "vpc"),
            max_azs=cfg.max_azs,
            nat_gateways=0,  # workloads reach AWS only through VPC endpoints
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="private-isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                )
            ],
        )

        # VPC flow logs for network forensics.
        flow_log_group = logs.LogGroup(
            self,
            "FlowLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY,
        )
        self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_log_group),
            traffic_type=ec2.FlowLogTrafficType.REJECT,
        )

        # Security group for application workloads (Lambda / agent runtime): no inbound,
        # outbound limited to HTTPS inside the VPC (endpoints).
        self.app_sg = ec2.SecurityGroup(
            self,
            "AppSg",
            vpc=self.vpc,
            description="Application workloads: no inbound, HTTPS egress to VPC endpoints only",
            allow_all_outbound=False,
        )
        self.app_sg.add_egress_rule(
            ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            ec2.Port.tcp(443),
            "HTTPS to VPC endpoints",
        )

        # Gateway endpoints are free.
        self.vpc.add_gateway_endpoint(
            "DynamoDbEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB
        )
        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

        if cfg.interface_endpoints:
            endpoint_sg = ec2.SecurityGroup(
                self, "EndpointSg", vpc=self.vpc, description="VPC interface endpoints"
            )
            endpoint_sg.add_ingress_rule(
                self.app_sg, ec2.Port.tcp(443), "HTTPS from application workloads"
            )
            services = {
                "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
                "BedrockAgentRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENT_RUNTIME,
                "Sqs": ec2.InterfaceVpcEndpointAwsService.SQS,
                "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
                "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
                "Kms": ec2.InterfaceVpcEndpointAwsService.KMS,
                "StepFunctions": ec2.InterfaceVpcEndpointAwsService.STEP_FUNCTIONS,
            }
            for name, service in services.items():
                self.vpc.add_interface_endpoint(
                    f"{name}Endpoint",
                    service=service,
                    security_groups=[endpoint_sg],
                    private_dns_enabled=True,
                )
