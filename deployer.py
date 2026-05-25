"""
AWS Deployment Tool
Handles S3 bucket creation (with lifecycle rules & metrics) and Lambda function deployment.
"""

import boto3
import json
import os
import sys
import tempfile
import urllib.request
import yaml
import logging
from pathlib import Path
from botocore.exceptions import ClientError

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("deployer")


# ─────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────
def load_config(config_path: str) -> dict:
    """Load and validate YAML configuration file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        cfg = yaml.safe_load(f)

    required_top = ["aws", "s3", "lambda"]
    for key in required_top:
        if key not in cfg:
            raise ValueError(f"Missing required config section: '{key}'")

    return cfg


# ─────────────────────────────────────────────
# AWS client factory
# ─────────────────────────────────────────────
def build_clients(cfg: dict):
    """
    Create boto3 S3 and Lambda clients.

    Supports two auth modes (set in config aws.auth_mode):
      - 'iam_user'  : uses aws_access_key_id + aws_secret_access_key
      - 'profile'   : uses a named AWS CLI profile
    """
    aws = cfg["aws"]
    region = aws.get("region", "us-east-1")
    auth_mode = aws.get("auth_mode", "iam_user")

    log.info("Building AWS clients  region=%s  auth_mode=%s", region, auth_mode)

    if auth_mode == "iam_user":
        session = boto3.Session(
            aws_access_key_id=aws["access_key_id"],
            aws_secret_access_key=aws["secret_access_key"],
            region_name=region,
        )
    elif auth_mode == "profile":
        session = boto3.Session(
            profile_name=aws["profile_name"],
            region_name=region,
        )
    else:
        raise ValueError(f"Unknown auth_mode: {auth_mode}")

    s3_client = session.client("s3")
    lambda_client = session.client("lambda")
    log.info("AWS clients created successfully.")
    return s3_client, lambda_client


# ─────────────────────────────────────────────
# S3 helpers
# ─────────────────────────────────────────────
def bucket_exists(s3, bucket_name: str) -> bool:
    try:
        s3.head_bucket(Bucket=bucket_name)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            return False
        raise


def create_bucket(s3, bucket_name: str, region: str):
    """Create a single S3 bucket (handles us-east-1 quirk)."""
    if bucket_exists(s3, bucket_name):
        log.info("  Bucket already exists, skipping: %s", bucket_name)
        return

    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket_name)
    else:
        s3.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    log.info("  Created bucket: %s", bucket_name)


def apply_lifecycle_rules(s3, bucket_name: str, lifecycle_cfg: dict):
    """
    Apply lifecycle rules from config.

    Config shape (under s3.lifecycle):
      transition_days_ia: 30       # days until STANDARD_IA
      transition_days_glacier: 90  # days until GLACIER
      expiration_days: 365         # days until deletion (0 = disabled)
      noncurrent_expiration_days: 30
    """
    rules = []

    transition_ia = lifecycle_cfg.get("transition_days_ia", 30)
    transition_glacier = lifecycle_cfg.get("transition_days_glacier", 90)
    expiration = lifecycle_cfg.get("expiration_days", 0)
    nc_expiration = lifecycle_cfg.get("noncurrent_expiration_days", 30)

    transitions = []
    if transition_ia:
        transitions.append({"Days": int(transition_ia), "StorageClass": "STANDARD_IA"})
    if transition_glacier:
        transitions.append({"Days": int(transition_glacier), "StorageClass": "GLACIER"})

    rule: dict = {
        "ID": "deployer-lifecycle-rule",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "Transitions": transitions,
        "NoncurrentVersionExpiration": {"NoncurrentDays": int(nc_expiration)},
    }

    if expiration:
        rule["Expiration"] = {"Days": int(expiration)}

    rules.append(rule)

    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration={"Rules": rules},
    )
    log.info("  Lifecycle rules applied to: %s", bucket_name)


def enable_bucket_metrics(s3, bucket_name: str, metrics_id: str = "EntireBucket"):
    """Enable S3 storage-class metrics for the whole bucket."""
    s3.put_bucket_metrics_configuration(
        Bucket=bucket_name,
        Id=metrics_id,
        MetricsConfiguration={"Id": metrics_id},
    )
    log.info("  Metrics enabled on: %s  (id=%s)", bucket_name, metrics_id)


def deploy_s3(s3, cfg: dict):
    """Orchestrate all S3 operations."""
    s3_cfg = cfg["s3"]
    region = cfg["aws"].get("region", "us-east-1")

    prefix = s3_cfg["bucket_name_prefix"]
    count = int(s3_cfg["bucket_count"])
    lifecycle_cfg = s3_cfg.get("lifecycle", {})
    metrics_id = s3_cfg.get("metrics_id", "EntireBucket")

    log.info("=== S3 Deployment  buckets=%d  prefix=%s ===", count, prefix)

    for i in range(count):
        bucket_name = f"{prefix}_{i:03d}"
        log.info("Processing bucket [%d/%d]: %s", i + 1, count, bucket_name)
        create_bucket(s3, bucket_name, region)
        apply_lifecycle_rules(s3, bucket_name, lifecycle_cfg)
        enable_bucket_metrics(s3, bucket_name, metrics_id)

    log.info("S3 deployment complete. %d bucket(s) processed.", count)


# ─────────────────────────────────────────────
# Lambda helpers
# ─────────────────────────────────────────────
def download_war_from_github(github_url: str) -> bytes:
    """
    Download a WAR (or ZIP) artifact from a GitHub URL.

    Supports:
      - Raw GitHub URLs   (https://raw.githubusercontent.com/...)
      - GitHub Releases   (https://github.com/.../releases/download/...)
    """
    log.info("Downloading artifact from: %s", github_url)
    req = urllib.request.Request(github_url, headers={"User-Agent": "aws-deployer/1.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    log.info("  Downloaded %d bytes.", len(data))
    return data


def load_env_vars(env_file_path: str) -> dict:
    """
    Load environment variables from a file.

    Supported formats:
      - KEY=VALUE  (dotenv style)
      - YAML dict
      - JSON dict
    """
    path = Path(env_file_path)
    if not path.exists():
        raise FileNotFoundError(f"Env vars file not found: {env_file_path}")

    content = path.read_text().strip()

    # Try JSON
    if content.startswith("{"):
        return json.loads(content)

    # Try YAML dict
    try:
        parsed = yaml.safe_load(content)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except yaml.YAMLError:
        pass

    # Fall back to dotenv KEY=VALUE
    env_vars = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            log.warning("  Skipping malformed env line: %s", line)
            continue
        key, _, value = line.partition("=")
        env_vars[key.strip()] = value.strip().strip('"').strip("'")

    return env_vars


def function_exists(lmb, function_name: str) -> bool:
    try:
        lmb.get_function(FunctionName=function_name)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def update_or_create_lambda(lmb, function_name: str, zip_bytes: bytes, lambda_cfg: dict):
    """Update function code if it exists; create it if it doesn't."""
    role_arn = lambda_cfg.get("role_arn", "")
    runtime = lambda_cfg.get("runtime", "java11")
    handler = lambda_cfg.get("handler", "com.example.Handler::handleRequest")
    timeout = int(lambda_cfg.get("timeout", 30))
    memory = int(lambda_cfg.get("memory_mb", 512))

    if function_exists(lmb, function_name):
        log.info("  Updating existing Lambda function: %s", function_name)
        lmb.update_function_code(
            FunctionName=function_name,
            ZipFile=zip_bytes,
        )
        # Wait for update to finish before modifying config
        waiter = lmb.get_waiter("function_updated")
        waiter.wait(FunctionName=function_name)
        lmb.update_function_configuration(
            FunctionName=function_name,
            Timeout=timeout,
            MemorySize=memory,
            Handler=handler,
        )
    else:
        if not role_arn:
            raise ValueError(
                "lambda.role_arn is required to create a new Lambda function."
            )
        log.info("  Creating new Lambda function: %s", function_name)
        lmb.create_function(
            FunctionName=function_name,
            Runtime=runtime,
            Role=role_arn,
            Handler=handler,
            Code={"ZipFile": zip_bytes},
            Timeout=timeout,
            MemorySize=memory,
        )
        waiter = lmb.get_waiter("function_active")
        waiter.wait(FunctionName=function_name)

    log.info("  Lambda code deployed: %s", function_name)


def apply_env_vars_to_lambda(lmb, function_name: str, env_vars: dict):
    """Merge new env vars into the Lambda function's existing environment."""
    # Fetch existing env vars to avoid overwriting unrelated ones
    resp = lmb.get_function_configuration(FunctionName=function_name)
    existing = resp.get("Environment", {}).get("Variables", {})
    merged = {**existing, **env_vars}

    lmb.update_function_configuration(
        FunctionName=function_name,
        Environment={"Variables": merged},
    )
    log.info(
        "  Environment variables applied to %s: %s",
        function_name,
        list(env_vars.keys()),
    )


def deploy_lambda(lmb, cfg: dict):
    """Orchestrate all Lambda operations."""
    lambda_cfg = cfg["lambda"]
    function_name = lambda_cfg["function_name"]
    github_url = lambda_cfg["artifact_github_url"]
    env_file = lambda_cfg["env_vars_file"]

    log.info("=== Lambda Deployment  function=%s ===", function_name)

    # 1. Download artifact
    zip_bytes = download_war_from_github(github_url)

    # 2. Deploy code
    update_or_create_lambda(lmb, function_name, zip_bytes, lambda_cfg)

    # 3. Load and apply env vars
    log.info("  Loading env vars from: %s", env_file)
    env_vars = load_env_vars(env_file)
    apply_env_vars_to_lambda(lmb, function_name, env_vars)

    log.info("Lambda deployment complete.")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(description="AWS S3 + Lambda Deployment Tool")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--skip-s3",
        action="store_true",
        help="Skip S3 bucket creation/configuration",
    )
    parser.add_argument(
        "--skip-lambda",
        action="store_true",
        help="Skip Lambda function deployment",
    )
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
        s3_client, lambda_client = build_clients(cfg)

        if not args.skip_s3:
            deploy_s3(s3_client, cfg)
        else:
            log.info("Skipping S3 deployment (--skip-s3 flag set).")

        if not args.skip_lambda:
            deploy_lambda(lambda_client, cfg)
        else:
            log.info("Skipping Lambda deployment (--skip-lambda flag set).")

        log.info("✅  All deployments finished successfully.")

    except FileNotFoundError as e:
        log.error("File not found: %s", e)
        sys.exit(1)
    except ValueError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)
    except ClientError as e:
        log.error("AWS API error: %s", e)
        sys.exit(1)
    except Exception as e:
        log.exception("Unexpected error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()