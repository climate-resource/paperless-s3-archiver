"""
The checks that need the date, or a second entity, to decide
"""

import datetime as dt
from pathlib import Path

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.validation import (
    check_exposure_window,
    check_object_lock_mode,
    check_retention_table,
    check_shared_grants,
    validate,
)

from ..conftest import config_dict

TODAY = dt.date(2026, 7, 30)


def _errors(findings):
    return [f for f in findings if f.severity == "error"]


def _warnings(findings):
    return [f for f in findings if f.severity == "warning"]


class TestExposureWindow:
    def test_says_nothing_when_audit_mode_is_off(self, cfg: Config):
        assert check_exposure_window(cfg, today=TODAY) == []

    def test_warns_while_a_valid_window_is_open(self, tmp_path: Path):
        cfg = Config.model_validate(config_dict(tmp_path, public_enabled=True, public_until="2026-09-01"))
        findings = check_exposure_window(cfg, today=TODAY)
        assert len(_warnings(findings)) == 1
        assert "AUDIT MODE IS ON" in findings[0].message

    def test_fails_a_window_that_should_already_have_closed(self, tmp_path: Path):
        cfg = Config.model_validate(config_dict(tmp_path, public_enabled=True, public_until="2026-01-01"))
        assert len(_errors(check_exposure_window(cfg, today=TODAY))) == 1

    def test_fails_a_window_open_longer_than_the_maximum(self, tmp_path: Path):
        # An engagement that genuinely needs longer is a deliberate reopening
        # with a new date, not one long window.
        cfg = Config.model_validate(config_dict(tmp_path, public_enabled=True, public_until="2027-07-30"))
        findings = check_exposure_window(cfg, today=TODAY, max_days=90)
        assert len(_errors(findings)) == 1
        assert "90 days" in findings[0].message

    def test_accepts_a_window_exactly_at_the_maximum(self, tmp_path: Path):
        cfg = Config.model_validate(config_dict(tmp_path, public_enabled=True, public_until="2026-10-28"))
        assert _errors(check_exposure_window(cfg, today=TODAY, max_days=90)) == []

    def test_fails_an_unparseable_date(self, tmp_path: Path):
        cfg = Config.model_validate(config_dict(tmp_path, public_enabled=True, public_until="next Tuesday"))
        assert len(_errors(check_exposure_window(cfg, today=TODAY))) == 1


class TestRetentionTable:
    def test_is_quiet_on_a_sound_table(self, cfg: Config):
        assert _errors(check_retention_table(cfg)) == []

    def test_fails_an_archived_class_with_no_clock(self, tmp_path: Path):
        cfg = Config.model_validate(
            config_dict(
                tmp_path,
                retention_classes={
                    "odd": {"archive": True, "clock": "none", "basis": "?"},
                },
            )
        )
        assert len(_errors(check_retention_table(cfg))) == 1

    def test_fails_a_grant_missing_its_period(self, tmp_path: Path):
        # Every document tagged with it would be refused at ingest. Better to
        # find that here than with a stalled spool.
        cfg = Config.model_validate(config_dict(tmp_path, grants={"futura": {"final_payment_year": 2030}}))
        findings = _errors(check_retention_table(cfg))
        assert len(findings) == 1
        assert "futura" in findings[0].message

    def test_warns_when_a_grant_class_declares_years_that_nothing_reads(self, tmp_path: Path):
        cfg = Config.model_validate(
            config_dict(
                tmp_path,
                retention_classes={"eu-grant": {"archive": True, "clock": "grant", "years": 5, "basis": "x"}},
            )
        )
        assert len(_warnings(check_retention_table(cfg))) == 1

    def test_warns_when_a_grant_class_is_archived_but_no_grants_exist(self, tmp_path: Path):
        cfg = Config.model_validate(
            config_dict(
                tmp_path,
                retention_classes={"eu-grant": {"archive": True, "clock": "grant", "basis": "x"}},
                grants={},
            )
        )
        assert any("no grants are" in f.message for f in _warnings(check_retention_table(cfg)))

    def test_warns_when_a_carve_out_states_no_basis(self, tmp_path: Path):
        # A class that is deliberately never archived should say why, so the
        # carve-out is a decision on the record rather than an omission.
        cfg = Config.model_validate(
            config_dict(
                tmp_path,
                retention_classes={"hr-file": {"archive": False, "clock": "none", "basis": ""}},
            )
        )
        assert any("states no basis" in f.message for f in _warnings(check_retention_table(cfg)))


class TestSharedGrants:
    def _entity(self, tmp_path: Path, name: str, grants: dict) -> Config:
        return Config.model_validate(config_dict(tmp_path, entity=name, grants=grants))

    def test_says_nothing_about_a_single_entity(self, cfg: Config):
        assert check_shared_grants([cfg]) == []

    def test_says_nothing_about_grants_only_one_entity_holds(self, tmp_path: Path):
        crs = self._entity(tmp_path, "crs", {"futura": {"final_payment_year": 2030, "years": 5}})
        cr = self._entity(tmp_path, "cr", {"other": {"final_payment_year": 2031, "years": 6}})
        assert check_shared_grants([crs, cr]) == []

    def test_reports_agreement_on_a_jointly_held_grant(self, tmp_path: Path):
        # A joint grant deliberately carries the same slug in both entities,
        # because the registry is per instance with no shared namespace.
        both = {"quicca": {"final_payment_year": 2030, "years": 5}}
        findings = check_shared_grants(
            [self._entity(tmp_path, "crs", both), self._entity(tmp_path, "cr", both)]
        )
        assert len(findings) == 1
        assert findings[0].severity == "info"
        assert "They agree" in findings[0].message

    def test_warns_when_the_final_payment_years_diverge(self, tmp_path: Path):
        # An amendment applied to one entity and forgotten on the other is the
        # one failure this shape cannot prevent, so it is reported every run.
        crs = self._entity(tmp_path, "crs", {"quicca": {"final_payment_year": 2031, "years": 5}})
        cr = self._entity(tmp_path, "cr", {"quicca": {"final_payment_year": 2030, "years": 5}})
        findings = check_shared_grants([crs, cr])
        assert findings[0].severity == "warning"
        assert "DIVERGENT" in findings[0].message

    def test_warns_when_the_periods_diverge(self, tmp_path: Path):
        crs = self._entity(tmp_path, "crs", {"quicca": {"final_payment_year": 2030, "years": 6}})
        cr = self._entity(tmp_path, "cr", {"quicca": {"final_payment_year": 2030, "years": 5}})
        assert _warnings(check_shared_grants([crs, cr]))

    def test_names_which_entity_stated_what(self, tmp_path: Path):
        crs = self._entity(tmp_path, "crs", {"quicca": {"final_payment_year": 2031, "years": 5}})
        cr = self._entity(tmp_path, "cr", {"quicca": {"final_payment_year": 2030, "years": 5}})
        message = check_shared_grants([crs, cr])[0].message
        assert "crs=2031" in message
        assert "cr=2030" in message


class TestObjectLockMode:
    def test_says_burn_in_output_is_disposable(self, burn_in_cfg: Config):
        message = check_object_lock_mode(burn_in_cfg)[0].message
        assert "disposable" in message

    def test_says_compliance_is_irreversible(self, cfg: Config):
        message = check_object_lock_mode(cfg)[0].message
        assert "irreversible" in message


class TestValidate:
    def test_reports_errors_first(self, tmp_path: Path):
        cfg = Config.model_validate(
            config_dict(
                tmp_path,
                public_enabled=True,
                public_until="2026-01-01",
                grants={"futura": {"final_payment_year": 2030}},
            )
        )
        findings = validate(cfg, today=TODAY)
        severities = [f.severity for f in findings]
        assert severities == sorted(severities, key=["error", "warning", "info"].index)
        assert len(_errors(findings)) == 2

    def test_always_states_the_object_lock_mode(self, cfg: Config):
        assert any("Object Lock mode" in f.message for f in validate(cfg, today=TODAY))

    def test_a_sound_config_produces_no_errors(self, cfg: Config):
        assert _errors(validate(cfg, today=TODAY)) == []
