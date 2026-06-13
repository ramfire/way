"""Smoke test for Scaleway Object Storage (S3-compatible).

Reads credentials from /opt/alfaway/.env (never hardcode keys).
Run:  cd /opt/alfaway && source venv/bin/activate && python test_s3.py
"""
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from decouple import config

ACCESS = config("SCW_ACCESS_KEY")
SECRET = config("SCW_SECRET_KEY")
ENDPOINT = config("SCW_ENDPOINT", default="https://s3.fr-par.scw.cloud")
REGION = config("SCW_REGION", default="fr-par")
BUCKET = config("SCW_TEST_BUCKET", default="alfaway-dev")

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT,
    aws_access_key_id=ACCESS,
    aws_secret_access_key=SECRET,
    region_name=REGION,
    config=Config(signature_version="s3v4"),
)

# Create bucket (idempotent)
try:
    s3.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    print(f"✓ Bucket créé : {BUCKET}")
except ClientError as e:
    code = e.response["Error"]["Code"]
    if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
        print(f"✓ Bucket déjà présent : {BUCKET}")
    else:
        raise

# Upload
s3.put_object(Bucket=BUCKET, Key="test/hello.txt", Body=b"AlfaWay S3 test")
print("✓ Upload OK : test/hello.txt")

# List
objects = s3.list_objects_v2(Bucket=BUCKET)
for obj in objects.get("Contents", []):
    print(f"✓ {obj['Key']} ({obj['Size']} bytes)")
