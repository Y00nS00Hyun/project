from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
EXPECTED_ROOT = FIXTURE_ROOT / "expected"


def fixture_path(relative: str) -> Path:
    """Resolve a fixture, honouring the HWP_POC_FIXTURE_DIR override.

    Set ``HWP_POC_FIXTURE_DIR`` to point the integration tests at a corpus kept
    outside the repository (internal documents must not be committed).
    """
    import os

    override = os.environ.get("HWP_POC_FIXTURE_DIR")
    root = Path(override) if override else FIXTURE_ROOT
    return root / relative


def require_fixture(relative: str) -> Path:
    path = fixture_path(relative)
    if not path.exists():
        pytest.skip(
            f"fixture '{relative}' is not present; "
            f"see tests/fixtures/README.md for how to add it"
        )
    return path


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURE_ROOT
