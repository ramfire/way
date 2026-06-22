"""Accès S3 / Scaleway Object Storage (creds lus depuis .env via settings)."""
import boto3
from botocore.config import Config

from django.conf import settings

# Durée de vie par défaut d'une URL pré-signée (secondes) — 15 min.
PRESIGN_DEFAULT_EXPIRY = 15 * 60


def get_s3_client():
    """Client boto3 configuré pour Scaleway (signature v4 requise pour le presign)."""
    return boto3.client(
        's3',
        endpoint_url=settings.SCW_ENDPOINT,
        aws_access_key_id=settings.SCW_ACCESS_KEY,
        aws_secret_access_key=settings.SCW_SECRET_KEY,
        region_name=settings.SCW_REGION,
        config=Config(signature_version='s3v4'),
    )


def object_size(bucket, key):
    """Taille (octets) de l'objet S3, ou ``None`` si introuvable/injoignable.

    Lecture **best-effort** : toute erreur (objet absent, réseau/S3 down, creds)
    est avalée et renvoie ``None``. Utilisée à l'ingestion quand le webhook SFTPGo
    ne transmet pas la taille (son hook ``upload`` ne porte pas ``file_size``)."""
    try:
        head = get_s3_client().head_object(Bucket=bucket, Key=key)
        return head['ContentLength']
    except Exception:
        return None


def presigned_get_url(bucket, key, expires_in=PRESIGN_DEFAULT_EXPIRY, filename=None):
    """URL GET pré-signée vers un objet. ``filename`` force le nom du téléchargement."""
    params = {'Bucket': bucket, 'Key': key}
    if filename:
        params['ResponseContentDisposition'] = f'attachment; filename="{filename}"'
    return get_s3_client().generate_presigned_url(
        'get_object', Params=params, ExpiresIn=expires_in,
    )
