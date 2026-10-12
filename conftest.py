import pytest


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    """Run every test in a temp directory so logs/ is never written into the repo."""
    monkeypatch.chdir(tmp_path)
