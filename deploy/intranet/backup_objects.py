"""Write a portable object backup without deleting or mutating source objects."""
import hashlib
import io
import json
import tarfile
from pathlib import Path

from autoeval.config import get_settings
from autoeval.datasets import storage


def main():
    settings = get_settings()
    client = storage(settings)
    rows = []
    destination = Path("/backups")
    with tarfile.open(destination / "objects.tar.gz", "x:gz") as archive:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
            for item in page.get("Contents", []):
                response = client.get_object(Bucket=settings.s3_bucket, Key=item["Key"])
                try:
                    raw = response["Body"].read()
                finally:
                    response["Body"].close()
                sha = hashlib.sha256(raw).hexdigest()
                name = "objects/" + hashlib.sha256(item["Key"].encode()).hexdigest() + ".bin"
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(raw))
                rows.append({"key": item["Key"], "sha256": sha, "size": len(raw), "path": name})
    (destination / "objects.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps({"backup_objects": len(rows)}))


if __name__ == "__main__":
    main()
