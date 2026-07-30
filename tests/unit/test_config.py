"""
Loading a config, which refuses the mistakes that cannot be undone
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from paperless_s3_archiver.config import Config, load_config

from ..conftest import config_dict


class TestLoadConfig:
    def test_loads_a_rendered_config(self, config_file: Path):
        cfg = load_config(config_file)
        assert cfg.entity == "crs"
        assert "receipts" in cfg.retention_classes

    def test_ignores_the_comment_keys_the_template_writes(self, tmp_path: Path):
        raw = config_dict(tmp_path)
        raw["_comment"] = ["Managed by Ansible. Do not edit on the host."]
        raw["_retention_comment"] = ["The clock starts at the end of the calendar year."]
        path = tmp_path / "archive.json"
        path.write_text(json.dumps(raw), encoding="utf-8")

        assert load_config(path).entity == "crs"


class TestRetentionTableIsPerEntity:
    def test_refuses_an_empty_class_table(self, tmp_path: Path):
        # The table is per entity and has no default: periods come from that
        # entity's own law. Inheriting another entity's table by omission is
        # exactly the mistake that becomes permanent.
        with pytest.raises(ValidationError, match="retention_classes"):
            Config.model_validate(config_dict(tmp_path, retention_classes={}))

    def test_refuses_an_archived_document_year_class_with_no_period(self, tmp_path: Path):
        with pytest.raises(ValidationError, match="positive `years`"):
            Config.model_validate(
                config_dict(
                    tmp_path,
                    retention_classes={"books": {"archive": True, "clock": "document_year", "years": None}},
                )
            )

    def test_refuses_an_archived_employment_class_with_no_period(self, tmp_path: Path):
        with pytest.raises(ValidationError, match="positive `years`"):
            Config.model_validate(
                config_dict(
                    tmp_path,
                    retention_classes={
                        "hr-contract": {"archive": True, "clock": "employment_end", "years": None}
                    },
                )
            )

    def test_allows_a_grant_class_with_no_period(self, tmp_path: Path):
        # The period comes from each grant agreement, so `years` on the class
        # would be a number nothing reads.
        cfg = Config.model_validate(
            config_dict(
                tmp_path,
                retention_classes={"eu-grant": {"archive": True, "clock": "grant"}},
            )
        )
        assert cfg.retention_classes["eu-grant"].years is None

    def test_allows_a_never_archived_class_with_no_period(self, tmp_path: Path):
        cfg = Config.model_validate(
            config_dict(
                tmp_path,
                retention_classes={"hr-file": {"archive": False, "clock": "none"}},
            )
        )
        assert cfg.archived_classes() == set()

    def test_refuses_an_unknown_field_in_a_class(self, tmp_path: Path):
        # A typo in a class key would otherwise be silently ignored, and a class
        # that quietly lost its `restricted: true` is a personnel leak.
        with pytest.raises(ValidationError):
            Config.model_validate(
                config_dict(
                    tmp_path,
                    retention_classes={
                        "books": {
                            "archive": True,
                            "clock": "document_year",
                            "years": 11,
                            "restrictd": True,
                        }
                    },
                )
            )

    def test_refuses_an_unknown_clock(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            Config.model_validate(
                config_dict(
                    tmp_path,
                    retention_classes={"books": {"archive": True, "clock": "moon", "years": 11}},
                )
            )


class TestExposure:
    def test_refuses_an_open_window_with_no_expiry(self, tmp_path: Path):
        # Anything that relies on somebody remembering to close a hole is a hole
        # that stays open.
        with pytest.raises(ValidationError, match="public_until"):
            Config.model_validate(config_dict(tmp_path, public_enabled=True, public_until=""))

    def test_accepts_an_open_window_with_an_expiry(self, tmp_path: Path):
        cfg = Config.model_validate(config_dict(tmp_path, public_enabled=True, public_until="2027-01-01"))
        assert cfg.public_until == "2027-01-01"


class TestBurnIn:
    def test_governance_is_burn_in(self, burn_in_cfg: Config):
        assert burn_in_cfg.burn_in is True

    def test_compliance_is_not(self, cfg: Config):
        assert cfg.burn_in is False

    def test_anything_that_is_not_compliance_is_treated_as_burn_in(self, tmp_path: Path):
        # Fail safe: an unrecognised mode must not be read as "write real dates".
        cfg = Config.model_validate(config_dict(tmp_path, object_lock_mode="nonsense"))
        assert cfg.burn_in is True

    def test_the_mode_is_case_insensitive(self, tmp_path: Path):
        cfg = Config.model_validate(config_dict(tmp_path, object_lock_mode="compliance"))
        assert cfg.burn_in is False


class TestClassSets:
    def test_archived_classes_excludes_the_never_archived(self, cfg: Config):
        assert "hr-file" not in cfg.archived_classes()
        assert {"books", "receipts", "eu-grant", "hr-contract", "hr-pension"} == cfg.archived_classes()

    def test_auditor_classes_exclude_personnel_material(self, cfg: Config):
        assert cfg.tax_and_grant_classes() == {"books", "receipts", "eu-grant"}


class TestDerivedPaths:
    def test_hang_off_the_data_directory(self, cfg: Config):
        assert cfg.spool_dir == cfg.data_dir / "spool"
        assert cfg.media_dir == cfg.data_dir / "media"
        assert cfg.export_dir == cfg.data_dir / "export"
        assert cfg.journal_dir == cfg.data_dir / "journal"

    def test_state_is_per_entity(self, cfg: Config):
        assert cfg.state_dir.name == cfg.entity
