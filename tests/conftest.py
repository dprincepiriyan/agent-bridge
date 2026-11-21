import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Every test gets its own config/data/state/cache dirs."""
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.delenv("AGENT_BRIDGE_FAKE", raising=False)
    from agent_bridge import store
    monkeypatch.setattr(store, "legacy_dirs", lambda: [])
    return tmp_path
