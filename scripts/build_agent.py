"""Build the AgentCore Runtime direct-code-deployment zip (no Docker).

Installs dependencies as Linux arm64 wheels for Python 3.12 (AgentCore supports arm64 only),
adds the agent package and main.py at the zip root, and writes Linux permissions explicitly
(644 files / 755 dirs) because Windows does not record them.

    python scripts/build_agent.py [--force]
"""

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "agent"
DEPS = BUILD / "deps"
ZIP_PATH = BUILD / "agent.zip"
REQUIREMENTS = ROOT / "services" / "agents" / "requirements.txt"
PACKAGE = ROOT / "services" / "agents" / "src" / "csagent_agents"
ENTRYPOINT = ROOT / "services" / "agents" / "runtime" / "main.py"
MARKER = DEPS / ".deps-hash"
MAX_ZIPPED = 250 * 1024 * 1024
MAX_UNZIPPED = 750 * 1024 * 1024
SKIP_PARTS = {"__pycache__", "tests"}


def install_deps(force: bool) -> None:
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not force and MARKER.exists() and MARKER.read_text() == digest:
        return
    if DEPS.exists():
        shutil.rmtree(DEPS)
    DEPS.mkdir(parents=True)
    # uv (not pip): pip's --platform only changes wheel tags and still evaluates environment
    # markers against the HOST, so on Windows it tries to install `pywin32` (mcp declares
    # `pywin32; sys_platform == "win32"`) for Linux and fails. uv resolves markers for the target.
    subprocess.run(  # noqa: S603 - fixed argument list, no untrusted input
        [
            sys.executable, "-m", "uv", "pip", "install", "--quiet",
            "--target", str(DEPS),
            "--python-platform", "aarch64-manylinux2014",
            "--python-version", "3.12",
            "--only-binary", ":all:",
            "-r", str(REQUIREMENTS),
        ],
        check=True,
    )
    MARKER.write_text(digest)


EPOCH = (1980, 1, 1, 0, 0, 0)  # fixed timestamps: identical inputs give a byte-identical zip,
# so CDK sees no change (and AgentCore creates no new runtime version) when only mtimes differ.


def _add(zf: zipfile.ZipFile, path: Path, arcname: str) -> int:
    info = zipfile.ZipInfo(arcname, date_time=EPOCH)
    info.external_attr = (0o100644 << 16)  # regular file, rw-r--r--
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, path.read_bytes())
    return path.stat().st_size


def _add_tree(zf: zipfile.ZipFile, base: Path, prefix: str = "") -> int:
    total = 0
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if SKIP_PARTS & set(rel.parts) or path.suffix == ".pyc" or path.name == ".deps-hash":
            continue
        arc = f"{prefix}{rel.as_posix()}"
        if path.is_dir():
            info = zipfile.ZipInfo(arc + "/", date_time=EPOCH)
            info.external_attr = (0o40755 << 16) | 0x10  # directory, rwxr-xr-x
            zf.writestr(info, b"")
        else:
            total += _add(zf, path, arc)
    return total


def build(force: bool = False) -> Path:
    install_deps(force)
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        unzipped = _add_tree(zf, DEPS)
        unzipped += _add_tree(zf, PACKAGE, "csagent_agents/")
        unzipped += _add(zf, ENTRYPOINT, "main.py")
    zipped = ZIP_PATH.stat().st_size
    if zipped > MAX_ZIPPED or unzipped > MAX_UNZIPPED:
        raise SystemExit(f"package too large: {zipped} zipped / {unzipped} unzipped bytes")
    print(f"Built {ZIP_PATH} ({zipped / 1e6:.1f} MB zipped, {unzipped / 1e6:.1f} MB unzipped)")
    return ZIP_PATH


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    build(parser.parse_args().force)
