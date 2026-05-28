"""Tests for markbot.config.paths — Runtime path helpers."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from markbot.config.paths import (
    get_data_dir,
    get_runtime_subdir,
    get_media_dir,
    get_cron_dir,
    get_logs_dir,
    get_code_run_dir,
    get_artifacts_dir,
    get_gateway_dir,
    get_workspace_path,
    is_default_workspace,
    get_cli_history_path,
    get_bridge_install_dir,
    get_legacy_sessions_dir,
    _DEFAULT_WORKSPACE,
)


class TestGetWorkspacePath:
    def test_explicit_workspace(self, tmp_path):
        result = get_workspace_path(str(tmp_path))
        assert result == tmp_path

    def test_path_object_workspace(self, tmp_path):
        result = get_workspace_path(tmp_path)
        assert result == tmp_path

    def test_creates_directory(self, tmp_path):
        new_dir = tmp_path / "new_workspace"
        result = get_workspace_path(str(new_dir))
        assert result.exists()
        assert result.is_dir()

    def test_default_workspace_expansion(self):
        result = get_workspace_path()
        assert result.exists()
        # Should expand ~ to user home
        assert "~" not in str(result)


class TestIsDefaultWorkspace:
    def test_default_workspace(self):
        default = Path(_DEFAULT_WORKSPACE).expanduser()
        assert is_default_workspace(str(default)) is True

    def test_custom_workspace(self, tmp_path):
        assert is_default_workspace(str(tmp_path)) is False

    def test_none_uses_config_default(self):
        # When no config loaded, falls back to default
        result = is_default_workspace(None)
        assert isinstance(result, bool)


class TestGetDataDir:
    @patch("markbot.config.paths.get_config_path")
    def test_returns_parent_of_config(self, mock_get_config, tmp_path):
        config_path = tmp_path / "config" / "markbot.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.touch()
        mock_get_config.return_value = config_path
        result = get_data_dir()
        assert result == config_path.parent


class TestGetRuntimeSubdir:
    @patch("markbot.config.paths.get_data_dir")
    def test_creates_subdirectory(self, mock_data_dir, tmp_path):
        mock_data_dir.return_value = tmp_path
        result = get_runtime_subdir("test_sub")
        assert result == tmp_path / "test_sub"
        assert result.exists()


class TestGetMediaDir:
    def test_base_media_dir(self, tmp_path):
        with patch("markbot.config.paths.get_workspace_path", return_value=tmp_path):
            result = get_media_dir()
            assert result == tmp_path / "media"
            assert result.exists()

    def test_channel_media_dir(self, tmp_path):
        with patch("markbot.config.paths.get_workspace_path", return_value=tmp_path):
            result = get_media_dir("dingtalk")
            assert result == tmp_path / "media" / "dingtalk"
            assert result.exists()


class TestGetCronDir:
    def test_returns_cron_subdirectory(self, tmp_path):
        with patch("markbot.config.paths.get_workspace_path", return_value=tmp_path):
            result = get_cron_dir()
            assert result == tmp_path / "cron"


class TestGetLogsDir:
    @patch("markbot.config.paths.get_runtime_subdir")
    def test_returns_logs_subdir(self, mock_subdir, tmp_path):
        expected = tmp_path / "logs"
        mock_subdir.return_value = expected
        result = get_logs_dir()
        assert result == expected
        mock_subdir.assert_called_once_with("logs")


class TestGetCodeRunDir:
    @patch("markbot.config.paths.get_runtime_subdir")
    def test_returns_run_subdir(self, mock_subdir, tmp_path):
        expected = tmp_path / ".run"
        mock_subdir.return_value = expected
        result = get_code_run_dir()
        assert result == expected
        mock_subdir.assert_called_once_with(".run")


class TestGetArtifactsDir:
    @patch("markbot.config.paths.get_runtime_subdir")
    def test_returns_artifacts_subdir(self, mock_subdir, tmp_path):
        expected = tmp_path / ".artifacts"
        mock_subdir.return_value = expected
        result = get_artifacts_dir()
        assert result == expected
        mock_subdir.assert_called_once_with(".artifacts")


class TestGetGatewayDir:
    @patch("markbot.config.paths.get_runtime_subdir")
    def test_returns_gateway_subdir(self, mock_subdir, tmp_path):
        expected = tmp_path / "gateway"
        mock_subdir.return_value = expected
        result = get_gateway_dir()
        assert result == expected
        mock_subdir.assert_called_once_with("gateway")


class TestGetCliHistoryPath:
    @patch("markbot.config.paths.get_data_dir")
    def test_returns_history_path(self, mock_data_dir, tmp_path):
        mock_data_dir.return_value = tmp_path
        result = get_cli_history_path()
        assert result == tmp_path / "history" / "cli_history"


class TestGetBridgeInstallDir:
    @patch("markbot.config.paths.get_runtime_subdir")
    def test_returns_bridge_subdir(self, mock_subdir, tmp_path):
        expected = tmp_path / "bridge"
        mock_subdir.return_value = expected
        result = get_bridge_install_dir()
        assert result == expected
        mock_subdir.assert_called_once_with("bridge")


class TestGetLegacySessionsDir:
    @patch("markbot.config.paths.get_runtime_subdir")
    def test_returns_sessions_subdir(self, mock_subdir, tmp_path):
        expected = tmp_path / "sessions"
        mock_subdir.return_value = expected
        result = get_legacy_sessions_dir()
        assert result == expected
        mock_subdir.assert_called_once_with("sessions")
