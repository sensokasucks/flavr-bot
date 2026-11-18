import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_custom_groups():
    """Each test starts with only FlaVR and Friends (flavrbot/groups.py keeps its store in a module global)."""
    from flavrbot import groups
    groups.init(None)
    yield
    groups.init(None)
