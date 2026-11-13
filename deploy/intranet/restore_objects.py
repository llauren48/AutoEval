"""Restore only manifest-listed bytes, with idempotent hash readback."""
import hashlib
import json
import tarfile
from pathlib import Path

from botocore.exceptions import ClientError

from autoeval.config import get_settings
from autoeval.datasets import storage


def main():
    settings = get_settings()
    client = storage(settings)
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as exc:
        if str(exc.response["Error"]["Code"]) not in {"404", "NoSuchBucket"}:
            raise
        client.create_bucket(Bucket=settings.s3_bucket)
    rows = json.loads(Path("/import/objects.json").read_text())
    with tarfile.open("/import/objects.tar.gz", "r:gz") as archive:
        for row in rows:
            member = archive.getmember(row["path"])
            if not member.isfile() or not row["path"].startswith("objects/"):
                raise ValueError("Unexpected archive member")
            raw = archive.extractfile(member).read()
            if hashlib.sha256(raw).hexdigest() != row["sha256"] or len(raw) != row["size"]:
                raise ValueError("Source object integrity failed")
            try:
                current = client.get_object(Bucket=settings.s3_bucket, Key=row["key"])["Body"].read()
            except ClientError as exc:
                if str(exc.response["Error"]["Code"]) not in {"404", "NoSuchKey"}:
                    raise
                client.put_object(Bucket=settings.s3_bucket, Key=row["key"], Body=raw)
            else:
                if hashlib.sha256(current).hexdigest() != row["sha256"]:
                    raise ValueError("Existing target object conflicts")
            saved = client.get_object(Bucket=settings.s3_bucket, Key=row["key"])["Body"].read()
            assert hashlib.sha256(saved).hexdigest() == row["sha256"]
    print(json.dumps({"verified_objects": len(rows)}))


if __name__ == "__main__":
    main()
