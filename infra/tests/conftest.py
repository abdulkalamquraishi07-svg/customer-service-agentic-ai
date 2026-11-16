import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def lambda_package():
    """CDK assets require the built Lambda directory (no Docker needed)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from build_lambda import build

    return build()
