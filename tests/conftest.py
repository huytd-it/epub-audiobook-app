import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def _isolated_zerotts_voice_packs(tmp_path_factory, monkeypatch):
    """Importing a ZeroTTS voice copies its pack into the repo; keep tests out of it."""
    monkeypatch.setattr(settings, "zerotts_voice_packs_dir",
                        str(tmp_path_factory.mktemp("voice_packs")))
