"""
The retention arithmetic, which is where a mistake becomes permanent
"""

import datetime as dt

import pytest

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.retention import (
    AMBIGUOUS,
    UNCLASSIFIED,
    Undecidable,
    recompute_retention,
    resolve_class,
    retention_for,
    tag_suffix,
    year_end,
    year_from_key,
)


class TestYearEnd:
    def test_is_the_last_second_of_the_year_in_utc(self):
        # The clock starts at the end of the calendar year of the last entry
        # (§ 147 (4) AO), so a period never expires part way through a year.
        assert year_end(2035) == dt.datetime(2035, 12, 31, 23, 59, 59, tzinfo=dt.UTC)

    def test_is_timezone_aware(self):
        # A naive datetime handed to boto3 is interpreted as local time, which
        # would silently shift every retain-until by the host's offset.
        assert year_end(2035).tzinfo is not None


class TestResolveClass:
    def test_one_known_class_tag_resolves(self, cfg: Config):
        assert resolve_class(cfg, ["class:receipts", "correspondent:acme"]) == "receipts"

    def test_no_class_tag_is_unclassified(self, cfg: Config):
        assert resolve_class(cfg, ["correspondent:acme"]) == UNCLASSIFIED

    def test_no_tags_at_all_is_unclassified(self, cfg: Config):
        assert resolve_class(cfg, []) == UNCLASSIFIED

    def test_two_class_tags_are_ambiguous(self, cfg: Config):
        # Refusing is the control. A document nobody has classified must not
        # fall into whichever class happens to be first in the table.
        assert resolve_class(cfg, ["class:receipts", "class:books"]) == AMBIGUOUS

    def test_an_unknown_class_tag_is_unclassified_not_a_guess(self, cfg: Config):
        assert resolve_class(cfg, ["class:invented"]) == UNCLASSIFIED

    def test_a_known_class_beside_an_unknown_one_is_ambiguous(self, cfg: Config):
        # Two class tags is two class tags, whether or not both are in the
        # table. Picking the known one would be exactly the guess we refuse.
        assert resolve_class(cfg, ["class:receipts", "class:invented"]) == AMBIGUOUS

    def test_the_prefix_comes_from_the_config(self, cfg: Config):
        relabelled = cfg.model_copy(update={"class_tag_prefix": "retention/"})
        assert resolve_class(relabelled, ["retention/books"]) == "books"
        assert resolve_class(relabelled, ["class:books"]) == UNCLASSIFIED


class TestTagSuffix:
    def test_returns_the_suffix(self):
        assert tag_suffix(["grant:futura", "class:eu-grant"], prefix="grant:") == "futura"

    def test_returns_none_when_absent(self):
        assert tag_suffix(["class:eu-grant"], prefix="grant:") is None


class TestDocumentYearClock:
    @pytest.mark.parametrize(
        ("class_name", "doc_year", "expected_year"),
        [
            ("books", 2026, 2037),  # 11 years
            ("receipts", 2026, 2035),  # 9 years
            ("receipts", 2000, 2009),
        ],
    )
    def test_adds_the_period_to_the_document_year(
        self, cfg: Config, class_name: str, doc_year: int, expected_year: int
    ):
        retain_until, legal_hold, _ = retention_for(
            cfg, class_name=class_name, doc_year=doc_year, tag_names=[]
        )
        assert retain_until == year_end(expected_year)
        assert legal_hold is False

    def test_measures_from_the_document_year_not_from_now(self, cfg: Config):
        # A 2015 receipt uploaded today is held until 2024, not until 2035.
        # Measuring from "now" would over-retain by however long the backlog was.
        retain_until, _, _ = retention_for(cfg, class_name="receipts", doc_year=2015, tag_names=[])
        assert retain_until.year == 2024

    def test_the_explanation_names_the_arithmetic(self, cfg: Config):
        _, _, why = retention_for(cfg, class_name="receipts", doc_year=2026, tag_names=[])
        assert "receipts" in why
        assert "2026" in why
        assert "9" in why


class TestGrantClock:
    def test_uses_the_grants_final_payment_year(self, cfg: Config):
        retain_until, legal_hold, why = retention_for(
            cfg,
            class_name="eu-grant",
            doc_year=2026,
            tag_names=["class:eu-grant", "grant:futura"],
        )
        # 2030 final payment + 5 years, and the document's own year is ignored.
        assert retain_until == year_end(2035)
        assert legal_hold is False
        assert "futura" in why

    def test_refuses_without_a_grant_tag(self, cfg: Config):
        with pytest.raises(Undecidable, match="grant:"):
            retention_for(cfg, class_name="eu-grant", doc_year=2026, tag_names=["class:eu-grant"])

    def test_refuses_an_unregistered_grant(self, cfg: Config):
        # A typo in a slug costs a re-tag, never a guessed date.
        with pytest.raises(Undecidable, match="futrua"):
            retention_for(cfg, class_name="eu-grant", doc_year=2026, tag_names=["grant:futrua"])

    def test_refuses_a_grant_missing_its_period(self, tmp_path, cfg: Config):
        incomplete = cfg.model_copy(
            update={"grants": {"partial": cfg.grants["futura"].model_copy(update={"years": None})}}
        )
        with pytest.raises(Undecidable, match="never assumed"):
            retention_for(incomplete, class_name="eu-grant", doc_year=2026, tag_names=["grant:partial"])


class TestGrantFloor:
    """A grant tag holds a document at least as long as its agreement requires.

    The alternative was a rule telling whoever files the document to work out
    which of two periods runs longer and pick that class. That is arithmetic a
    person should never be asked to do, it has to be redone whenever a grant's
    final payment year moves, and getting it wrong under-retains permanently.
    So the grant tag is a floor under whatever class the document actually is.
    """

    def test_raises_a_receipt_to_the_grant_period(self, cfg: Config):
        # A 2020 subcontractor invoice on FUTURA: 2020 + 9 = 2029 as a
        # Buchungsbeleg, but 2030 + 5 = 2035 under the grant.
        retain_until, legal_hold, why = retention_for(
            cfg,
            class_name="receipts",
            doc_year=2020,
            tag_names=["class:receipts", "grant:futura"],
        )
        assert retain_until == year_end(2035)  # not 2029, which receipts alone gives
        assert legal_hold is False
        assert "futura" in why
        assert "receipts" in why

    def test_leaves_a_longer_class_period_alone(self, cfg: Config):
        # The case the old "the grant class wins" rule got wrong: receipts grows
        # with the document year while a grant's date is fixed, so from some
        # year onwards the tax clock is the longer one and must not be dropped.
        retain_until, _, why = retention_for(
            cfg,
            class_name="receipts",
            doc_year=2030,
            tag_names=["class:receipts", "grant:futura"],
        )
        assert retain_until == year_end(2039)
        assert "longer than" in why

    def test_only_ever_raises(self, cfg: Config):
        # Retention can be extended and never shortened, so taking the later of
        # the two bases is the one combination that cannot be wrong.
        for doc_year in range(2015, 2040):
            alone, _, _ = retention_for(
                cfg, class_name="receipts", doc_year=doc_year, tag_names=["class:receipts"]
            )
            with_grant, _, _ = retention_for(
                cfg,
                class_name="receipts",
                doc_year=doc_year,
                tag_names=["class:receipts", "grant:futura"],
            )
            assert with_grant >= alone

    def test_the_explanation_names_both_bases(self, cfg: Config):
        # The sidecar carries this string, so an auditor reads why a document is
        # held until a date its own class would not have produced.
        _, _, why = retention_for(
            cfg,
            class_name="books",
            doc_year=2020,
            tag_names=["class:books", "grant:futura"],
        )
        assert "futura" in why
        assert "books" in why

    def test_a_document_with_no_grant_tag_is_unaffected(self, cfg: Config):
        plain, _, _ = retention_for(cfg, class_name="receipts", doc_year=2026, tag_names=["class:receipts"])
        assert plain == year_end(2035)

    def test_an_unregistered_slug_refuses_even_on_a_dated_class(self, cfg: Config):
        # Previously a grant tag on a receipt was simply ignored, so a typo lost
        # the grant basis silently. Now it costs a re-tag, which is the same
        # trade the grant clock has always made.
        with pytest.raises(Undecidable, match="never assumed"):
            retention_for(
                cfg,
                class_name="receipts",
                doc_year=2026,
                tag_names=["class:receipts", "grant:futrua"],
            )


class TestEmploymentEndClock:
    def test_uses_the_end_year_when_the_employment_has_ended(self, cfg: Config):
        retain_until, legal_hold, _ = retention_for(
            cfg,
            class_name="hr-contract",
            doc_year=2020,
            tag_names=["employment:sam"],
        )
        # 2029 end + 11 years.
        assert retain_until == year_end(2040)
        assert legal_hold is False

    def test_applies_a_legal_hold_while_the_employment_continues(self, cfg: Config):
        retain_until, legal_hold, why = retention_for(
            cfg,
            class_name="hr-contract",
            doc_year=2027,
            tag_names=["employment:alex"],
        )
        assert legal_hold is True
        # The floor is computed from the document year, so the object is still
        # locked for the full period even if nobody sets the employment end.
        assert retain_until == year_end(2038)
        assert "has not ended" in why

    def test_the_hold_is_what_stops_an_early_unlock(self, cfg: Config):
        # A contract signed in 2027 for an employment ending in 2045 must not
        # unlock in 2038. The floor date alone would; the hold is the control.
        _, legal_hold, _ = retention_for(
            cfg, class_name="hr-contract", doc_year=2027, tag_names=["employment:alex"]
        )
        assert legal_hold is True

    def test_pension_material_gets_its_own_much_longer_period(self, cfg: Config):
        retain_until, _, _ = retention_for(
            cfg, class_name="hr-pension", doc_year=2020, tag_names=["employment:sam"]
        )
        assert retain_until == year_end(2059)  # 2029 + 30

    def test_refuses_without_an_employment_tag(self, cfg: Config):
        with pytest.raises(Undecidable, match="employment:"):
            retention_for(cfg, class_name="hr-contract", doc_year=2027, tag_names=[])

    def test_an_unregistered_person_is_held_rather_than_refused(self, cfg: Config):
        # Unknown and "still employed" are the same state as far as the clock is
        # concerned: it has not started. Holding is right; refusing would leave
        # personnel material unarchived on a technicality.
        retain_until, legal_hold, _ = retention_for(
            cfg, class_name="hr-contract", doc_year=2027, tag_names=["employment:nobody"]
        )
        assert legal_hold is True
        assert retain_until == year_end(2038)


class TestUnknownClock:
    def test_refuses(self, cfg: Config):
        broken = cfg.model_copy(
            update={
                "retention_classes": {
                    **cfg.retention_classes,
                    "odd": cfg.retention_classes["books"].model_copy(update={"clock": "moon"}),
                }
            }
        )
        with pytest.raises(Undecidable, match="unknown clock"):
            retention_for(broken, class_name="odd", doc_year=2026, tag_names=[])


class TestYearFromKey:
    def test_reads_the_year_the_tap_filed_under(self):
        assert year_from_key("documents/2026/abc123") == 2026

    @pytest.mark.parametrize("key", ["documents/abc123", "documents/notayear/abc", "exports/x/y"])
    def test_refuses_a_key_it_cannot_read_a_year_off(self, key: str):
        with pytest.raises(Undecidable):
            year_from_key(key)


class TestRecomputeRetention:
    def test_reaches_the_same_answer_as_the_tap(self, cfg: Config):
        # Both read the year off the same place, so there is no second
        # implementation of "which year is this document from" to drift.
        at_ingest, _, _ = retention_for(cfg, class_name="receipts", doc_year=2026, tag_names=[])
        later, _, _ = recompute_retention(
            cfg,
            sidecar={"retention_class": "receipts", "tags": []},
            doc_key="documents/2026/abc",
        )
        assert later == at_ingest

    def test_follows_the_config_when_a_grant_period_moves(self, cfg: Config):
        sidecar = {"retention_class": "eu-grant", "tags": ["grant:futura"]}
        before, _, _ = recompute_retention(cfg, sidecar=sidecar, doc_key="documents/2026/abc")

        extended = cfg.model_copy(
            update={
                "grants": {"futura": cfg.grants["futura"].model_copy(update={"final_payment_year": 2031})}
            }
        )
        after, _, _ = recompute_retention(extended, sidecar=sidecar, doc_key="documents/2026/abc")

        assert after > before
        assert after == year_end(2036)

    def test_refuses_a_class_the_table_no_longer_has(self, cfg: Config):
        with pytest.raises(Undecidable, match="no longer has"):
            recompute_retention(
                cfg,
                sidecar={"retention_class": "retired", "tags": []},
                doc_key="documents/2026/abc",
            )

    def test_refuses_a_never_archived_class_as_a_leak(self, cfg: Config):
        # An object in a never-archived class is a leak to investigate, not a
        # retention question, so it must not be quietly given a date.
        with pytest.raises(Undecidable, match="leak"):
            recompute_retention(
                cfg,
                sidecar={"retention_class": "hr-file", "tags": []},
                doc_key="documents/2026/abc",
            )
