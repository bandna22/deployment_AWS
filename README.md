# deployment_AWS
Python utility to deploy set configuration on AWS

# AWS Deployment Tool

A Python CLI tool that automates S3 bucket provisioning and Lambda function deployment.

---

## Features

| Area | What it does |
|------|--------------|
| **S3** | Creates N buckets named `<prefix>_000` … `<prefix>_NNN` |
| **S3** | Applies lifecycle rules (IA transition, Glacier transition, expiration) |
| **S3** | Enables CloudWatch storage-class metrics on every bucket |
| **Lambda** | Downloads a WAR/ZIP artifact from a GitHub URL |
| **Lambda** | Creates or updates the target Lambda function |
| **Lambda** | Reads a `.env` / JSON / YAML file and applies env vars to the function |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Edit `config.yaml`

Fill in your AWS credentials (or profile name), S3 settings, and Lambda settings.

### 3. Edit `lambda.env`

Add the environment variables you want applied to the Lambda function.

### 4. Run

```bash
# Full deployment (S3 + Lambda)
python deployer.py --config config.yaml

# S3 only
python deployer.py --skip-lambda

# Lambda only
python deployer.py --skip-s3
```

---

## Configuration Reference

### `aws` section

| Key | Description |
|-----|-------------|
| `auth_mode` | `iam_user` (access key) or `profile` (named CLI profile) |
| `access_key_id` | IAM user access key *(iam_user mode)* |
| `secret_access_key` | IAM user secret key *(iam_user mode)* |
| `profile_name` | AWS CLI profile name *(profile mode)* |
| `region` | AWS region, e.g. `us-east-1` |

### `s3` section

| Key | Description |
|-----|-------------|
| `bucket_name_prefix` | Prefix for bucket names; buckets get suffix `_000`, `_001`, … |
| `bucket_count` | Number of buckets to create |
| `metrics_id` | CloudWatch metrics config ID (default `EntireBucket`) |
| `lifecycle.transition_days_ia` | Days until objects transition to STANDARD_IA |
| `lifecycle.transition_days_glacier` | Days until objects transition to GLACIER |
| `lifecycle.expiration_days` | Days until objects are deleted (`0` = disabled) |
| `lifecycle.noncurrent_expiration_days` | Days to retain non-current object versions |

### `lambda` section

| Key | Description |
|-----|-------------|
| `function_name` | Target Lambda function name |
| `role_arn` | IAM execution role ARN (required for new function creation) |
| `artifact_github_url` | Full URL to the ZIP/WAR on GitHub (raw or release asset) |
| `env_vars_file` | Path to env-vars file (dotenv, JSON, or YAML format) |
| `runtime` | Lambda runtime identifier, e.g. `java11`, `python3.12` |
| `handler` | Handler entrypoint, e.g. `com.example.Handler::handleRequest` |
| `timeout` | Function timeout in seconds |
| `memory_mb` | Function memory in MB |

---

## Environment Variables File Formats

All three formats are auto-detected:

**dotenv (`.env`)**
```
DB_HOST=my-db.rds.amazonaws.com
LOG_LEVEL=INFO
```

**JSON**
```json
{ "DB_HOST": "my-db.rds.amazonaws.com", "LOG_LEVEL": "INFO" }
```

**YAML**
```yaml
DB_HOST: my-db.rds.amazonaws.com
LOG_LEVEL: INFO
```

---

## IAM Permissions Required

**S3**
- `s3:CreateBucket`
- `s3:HeadBucket`
- `s3:PutLifecycleConfiguration`
- `s3:PutMetricsConfiguration`

**Lambda**
- `lambda:GetFunction`
- `lambda:GetFunctionConfiguration`
- `lambda:CreateFunction`
- `lambda:UpdateFunctionCode`
- `lambda:UpdateFunctionConfiguration`
- `iam:PassRole` *(only needed when creating a new function)*
