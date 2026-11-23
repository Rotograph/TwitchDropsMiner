import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config.settings import IdleChannelSettings, Settings
from src.web.managers.settings import SettingsManager
from tests.test_diagnostics import diagnostic_miner


def test_normalize_lowercases_strips_and_dedupes():
    assert IdleChannelSettings.normalize([" Foo ", "foo", "bar_1"]) == ["foo", "bar_1"]


@pytest.mark.parametrize(
    "value",
    ["foo", [1], ["bad-name"], [""], ["a" * 26], [f"x{i}" for i in range(21)]],
)
def test_normalize_rejects_bad_values(value):
    with pytest.raises(ValueError, match="invalid_idle_channels"):
        IdleChannelSettings.normalize(value)


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.settings.SETTINGS_PATH", tmp_path / "settings.json")
    settings = Settings()
    assert settings.idle_channels == []
    assert settings.claim_channel_points is True


def test_invalid_idle_channels_in_file_do_not_block_load(tmp_path, monkeypatch, caplog):
    path = tmp_path / "settings.json"
    path.write_text('{"idle_channels": ["My-Channel"]}', encoding="utf-8")
    monkeypatch.setattr("src.config.settings.SETTINGS_PATH", path)
    with caplog.at_level(logging.WARNING, logger="TwitchDrops"):
        settings = Settings()
    assert settings.idle_channels == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "My-Channel" not in caplog.text


def _manager():
    stored = SimpleNamespace(idle_channels=[], claim_channel_points=True, save=MagicMock())
    on_change = MagicMock()
    console = MagicMock()
    manager = SettingsManager(SimpleNamespace(emit=AsyncMock()), stored, console, on_change)
    return manager, stored, on_change, console


def _update(manager, data):
    async def run():
        return manager.update_settings(data)

    return asyncio.run(run())


def test_settings_update_saves_and_triggers_games_update(caplog):
    manager, stored, on_change, console = _manager()
    with caplog.at_level(logging.DEBUG, logger="TwitchDrops"):
        response = _update(manager, {"idle_channels": ["Foo"], "claim_channel_points": False})
    assert stored.idle_channels == ["foo"]
    assert stored.claim_channel_points is False
    assert response["idle_channels"] == ["foo"]
    assert response["claim_channel_points"] is False
    on_change.assert_called_once()
    assert "foo" not in " ".join(str(call) for call in console.print.call_args_list)
    assert "foo" not in caplog.text.lower()

    on_change.reset_mock()
    _update(manager, {"idle_channels": ["FOO"], "claim_channel_points": True})
    assert stored.claim_channel_points is True
    on_change.assert_not_called()


def test_claim_channel_points_requires_strict_bool():
    manager, stored, on_change, _ = _manager()
    with pytest.raises(ValueError, match="invalid_claim_channel_points"):
        _update(manager, {"claim_channel_points": "false"})
    assert stored.claim_channel_points is True
    stored.save.assert_not_called()


def test_invalid_idle_channels_leave_settings_untouched():
    manager, stored, _, _ = _manager()
    with pytest.raises(ValueError, match="invalid_idle_channels"):
        _update(manager, {"idle_channels": ["bad-name"]})
    stored.save.assert_not_called()


def test_diagnostics_report_counts_only(tmp_path):
    miner = diagnostic_miner(tmp_path)
    miner.settings.idle_channels = ["secretname"]
    snapshot = miner.diagnostics.snapshot(miner)
    assert snapshot["settings"]["idle_channels_total"] == 1
    assert snapshot["settings"]["claim_channel_points"] is True
    assert "secretname" not in json.dumps(snapshot)
