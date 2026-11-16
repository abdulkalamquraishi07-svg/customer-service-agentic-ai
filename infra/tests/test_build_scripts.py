"""Build outputs must be reproducible: CDK hashes them, so noise means needless deployments."""

import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import build_agent  # noqa: E402


def _zip(tree: Path, out: Path) -> bytes:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        build_agent._add_tree(zf, tree)
    return out.read_bytes()


def test_zip_is_byte_identical_when_only_modification_times_change(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "main.py").write_text("print('hi')\n")
    (tree / "pkg" / "mod.py").write_text("X = 1\n")
    first = _zip(tree, tmp_path / "a.zip")

    for path in (tree / "main.py", tree / "pkg" / "mod.py", tree / "pkg", tree):
        os.utime(path, (1_900_000_000, 1_900_000_000))  # different mtime
    assert _zip(tree, tmp_path / "b.zip") == first


def test_zip_changes_when_content_changes_and_has_linux_permissions(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "main.py").write_text("A\n")
    first = _zip(tree, tmp_path / "a.zip")
    (tree / "main.py").write_text("B\n")
    assert _zip(tree, tmp_path / "b.zip") != first

    info = zipfile.ZipFile(tmp_path / "b.zip").getinfo("main.py")
    assert info.external_attr >> 16 == 0o100644 and info.date_time == build_agent.EPOCH


def test_pycache_and_tests_are_excluded(tmp_path):
    tree = tmp_path / "tree"
    for sub in ("__pycache__", "tests", "pkg"):
        (tree / sub).mkdir(parents=True)
        (tree / sub / "f.py").write_text("x\n")
    (tree / "pkg" / "m.pyc").write_bytes(b"\0")
    archive = tmp_path / "x.zip"
    _zip(tree, archive)
    listed = zipfile.ZipFile(archive).namelist()
    assert "pkg/f.py" in listed
    excluded = [
        n for n in listed
        if n.startswith("tests") or "__pycache__" in n or n.endswith(".pyc")
    ]
    assert not excluded
