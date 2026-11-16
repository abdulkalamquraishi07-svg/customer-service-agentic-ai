"""Build the Lambda deployment directory (build/lambda) without Docker.

Dependencies are installed as Linux/ARM64 wheels for Python 3.12 (the Lambda runtime), then
the application code (csagent_core + bizapi) is copied in. boto3 is provided by the runtime.

    python scripts/build_lambda.py [--force]
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "lambda"
REQUIREMENTS = ROOT / "services" / "business_api" / "requirements.txt"
PACKAGES = {
    "csagent_core": ROOT / "services" / "core" / "src" / "csagent_core",
    "bizapi": ROOT / "services" / "business_api" / "src" / "bizapi",
}
MARKER = OUT / ".deps-hash"


def build(force: bool = False) -> Path:
    if force and OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not MARKER.exists() or MARKER.read_text() != digest:
        subprocess.run(  # noqa: S603 - fixed argument list, no untrusted input
            [
                sys.executable, "-m", "pip", "install", "--quiet",
                "--target", str(OUT),
                "--platform", "manylinux2014_aarch64",
                "--implementation", "cp",
                "--python-version", "3.12",
                "--only-binary=:all:",
                "--upgrade",
                "--retries", "10", "--resume-retries", "10", "--timeout", "60",
                "-r", str(REQUIREMENTS),
            ],
            check=True,
        )
        MARKER.write_text(digest)

    for name, src in PACKAGES.items():
        dest = OUT / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return OUT


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    print(f"Built Lambda package at {build(parser.parse_args().force)}")
