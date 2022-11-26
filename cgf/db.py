from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient
import boto3
import botocore

db = AsyncIOMotorClient(Path('.mongodb').read_text().strip())

s3_access_key = None
s3_secret_key = None
s3_service_url = None
s3_bucket_name = None

lines = Path('.s3').read_text().strip().split('\n')
for line in lines:
    key, val = line.strip().split("=", 2)
    if key.strip() == "access-key":
        s3_access_key = val.strip()
    elif key.strip() == "secret-key":
        s3_secret_key = val.strip()
    elif key.strip() == "service-url":
        s3_service_url = val.strip()
    elif key.strip() == "bucket-name":
        s3_bucket_name = val.strip()
for n,v in [
        ('access-key', s3_access_key),
        ('secret-key', s3_secret_key),
        ('service-url', s3_service_url),
        ('bucket-name', s3_bucket_name),
    ]:
    if v is None:
        raise Exception(f'Missing S3 config: {n}.')

client_config = botocore.config.Config(
    max_pool_connections=100
)

s3 = boto3.resource('s3',
    endpoint_url=f'https://{s3_service_url}/',
    aws_access_key_id=s3_access_key,
    aws_secret_access_key=s3_secret_key,
    config=client_config
)

s3_client = boto3.client('s3',
    endpoint_url=f'https://{s3_service_url}/',
    aws_access_key_id=s3_access_key,
    aws_secret_access_key=s3_secret_key,
    config=client_config
)
