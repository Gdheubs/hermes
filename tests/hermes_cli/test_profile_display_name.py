"""Tests for the profile display_name feature (#45624).

display_name is a presentation-only field in <profile_dir>/profile.yaml.
The canonical profile id ("default" for ~/.hermes) is never touched:
resolution, comparison, and spawn paths must be provably unaffected.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from hermes_cli import profiles
from hermes_cli.profiles import (
    create_profile,
    format_profile_label,
    get_profile_dir,
    list_profiles,
    profile_exists,
    read_profile_meta,
    rename_profile,
    resolve_profile_env,
    set_profile_display_name,
    validate_display_name,
    write_profile_meta,
)


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolated environment: Path.home() and HERMES_HOME under tmp_path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


# ===================================================================
# Metadata plumbing (read/write_profile_meta)
# ===================================================================

class TestDisplayNameMeta:
    def test_write_and_read_round_trip(self, profile_env):
        home = profile_env / ".hermes"
        write_profile_meta(home, display_name="Harumesu")
        meta = read_profile_meta(home)
        assert meta["display_name"] == "Harumesu"

    def test_missing_file_defaults_empty(self, profile_env):
        home = profile_env / ".hermes"
        assert read_profile_meta(home)["display_name"] == ""

    def test_preserves_description_fields(self, profile_env):
        home = profile_env / ".hermes"
        write_profile_meta(home, description="ops agent", description_auto=False)
        write_profile_meta(home, display_name="Harumesu")
        meta = read_profile_meta(home)
        assert meta["description"] == "ops agent"
        assert meta["description_auto"] is False
        assert meta["display_name"] == "Harumesu"

    def test_empty_string_clears_display_name(self, profile_env):
        home = profile_env / ".hermes"
        write_profile_meta(home, display_name="Harumesu")
        write_profile_meta(home, display_name="")
        meta = read_profile_meta(home)
        assert meta["display_name"] == ""
        # The key is removed from the file entirely, not left as "".
        data = yaml.safe_load((home / "profile.yaml").read_text())
        assert "display_name" not in data

    def test_unicode_round_trips(self, profile_env):
        home = profile_env / ".hermes"
        write_profile_meta(home, display_name="小助手")
        assert read_profile_meta(home)["display_name"] == "小助手"
        # And through the raw file: valid UTF-8 YAML.
        data = yaml.safe_load(
            (home / "profile.yaml").read_text(encoding="utf-8")
        )
        assert data["display_name"] == "小助手"


# ===================================================================
# Validation
# ===================================================================

class TestValidateDisplayName:
    def test_strips_whitespace(self):
        assert validate_display_name("  Harumesu  ") == "Harumesu"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_display_name("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError):
            validate_display_name("   ")

    def test_rejects_over_length(self):
        with pytest.raises(ValueError):
            validate_display_name("x" * 65)

    def test_accepts_max_length(self):
        assert validate_display_name("x" * 64) == "x" * 64

    def test_accepts_unicode(self):
        assert validate_display_name("小助手") == "小助手"


# ===================================================================
# rename_profile: default-profile branch sets display name
# ===================================================================

class TestRenameDefaultSetsDisplayName:
    def test_default_rename_sets_display_name(self, profile_env, capsys):
        home = profile_env / ".hermes"
        result = rename_profile("default", "Harumesu")
        assert result == home  # returns the (unchanged) default home
        meta = read_profile_meta(home)
        assert meta["display_name"] == "Harumesu"
        out = capsys.readouterr().out
        assert "canonical id remains 'default'" in out

    def test_default_home_dir_not_renamed(self, profile_env):
        home = profile_env / ".hermes"
        rename_profile("default", "Harumesu")
        assert home.is_dir()

    def test_default_still_default_in_list(self, profile_env):
        rename_profile("default", "Harumesu")
        infos = {p.name: p for p in list_profiles()}
        assert "default" in infos
        assert infos["default"].is_default is True
        assert infos["default"].display_name == "Harumesu"

    def test_default_rename_rejects_empty(self, profile_env):
        with pytest.raises(ValueError):
            rename_profile("default", "   ")

    def test_default_rename_rejects_over_length(self, profile_env):
        with pytest.raises(ValueError):
            rename_profile("default", "x" * 65)

    def test_rename_to_default_still_reserved(self, profile_env):
        create_profile("worker", no_alias=True)
        with pytest.raises(ValueError, match="reserved"):
            rename_profile("worker", "default")

    def test_named_profile_rename_still_real(self, profile_env):
        tmp_path = profile_env
        create_profile("oldname", no_alias=True)
        with patch(
            "hermes_cli.profiles.check_alias_collision", return_value="skip"
        ):
            new_dir = rename_profile("oldname", "newname")
        assert not (tmp_path / ".hermes" / "profiles" / "oldname").is_dir()
        assert new_dir == tmp_path / ".hermes" / "profiles" / "newname"

    def test_named_profile_display_name_survives_rename(self, profile_env):
        tmp_path = profile_env
        create_profile("oldname", no_alias=True)
        old_dir = tmp_path / ".hermes" / "profiles" / "oldname"
        write_profile_meta(old_dir, display_name="Old Friend")
        with patch(
            "hermes_cli.profiles.check_alias_collision", return_value="skip"
        ):
            new_dir = rename_profile("oldname", "newname")
        assert read_profile_meta(new_dir)["display_name"] == "Old Friend"


# ===================================================================
# set_profile_display_name (direct setter)
# ===================================================================

class TestSetProfileDisplayName:
    def test_sets_for_default(self, profile_env):
        home = profile_env / ".hermes"
        assert set_profile_display_name("default", "Harumesu") == "Harumesu"
        assert read_profile_meta(home)["display_name"] == "Harumesu"

    def test_sets_for_named_profile(self, profile_env):
        create_profile("worker", no_alias=True)
        set_profile_display_name("worker", "Workhorse")
        assert (
            read_profile_meta(get_profile_dir("worker"))["display_name"]
            == "Workhorse"
        )

    def test_clear_with_empty_string(self, profile_env):
        home = profile_env / ".hermes"
        set_profile_display_name("default", "Harumesu")
        assert set_profile_display_name("default", "") == ""
        assert read_profile_meta(home)["display_name"] == ""

    def test_missing_profile_raises(self, profile_env):
        with pytest.raises(FileNotFoundError):
            set_profile_display_name("ghost", "Boo")


# ===================================================================
# Rendering fallback (format_profile_label)
# ===================================================================

class TestFormatProfileLabel:
    def test_display_name_with_canonical_id(self):
        assert format_profile_label("default", "Harumesu") == "Harumesu (default)"

    def test_unset_falls_back_to_bare_name(self):
        # Byte-for-byte the pre-feature rendering for a no-meta profile.
        assert format_profile_label("default", "") == "default"
        assert format_profile_label("default", None) == "default"

    def test_same_as_id_collapses(self):
        assert format_profile_label("worker", "worker") == "worker"

    def test_unicode(self):
        assert format_profile_label("default", "小助手") == "小助手 (default)"


# ===================================================================
# Resolution is provably unaffected
# ===================================================================

class TestResolutionUnaffected:
    def test_get_profile_dir_ignores_display_name(self, profile_env):
        home = profile_env / ".hermes"
        rename_profile("default", "Harumesu")
        assert get_profile_dir("default") == home
        # The display name is NOT a resolvable profile id.
        assert get_profile_dir("harumesu") != home

    def test_profile_exists_ignores_display_name(self, profile_env):
        rename_profile("default", "Harumesu")
        assert profile_exists("default") is True
        assert profile_exists("harumesu") is False

    def test_resolve_profile_env_ignores_display_name(self, profile_env):
        home = profile_env / ".hermes"
        rename_profile("default", "Harumesu")
        assert resolve_profile_env("default") == str(home)
        with pytest.raises(FileNotFoundError):
            resolve_profile_env("harumesu")

    def test_list_profiles_canonical_name_unchanged(self, profile_env):
        rename_profile("default", "Harumesu")
        default_info = next(p for p in list_profiles() if p.is_default)
        assert default_info.name == "default"
        assert default_info.display_name == "Harumesu"
