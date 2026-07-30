"""
The metrics file and the journal, which is what an alert rule reads
"""

import json

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.observability import Metric, journal, write_metrics
from paperless_s3_archiver.state import State


def _read(cfg: Config, job: str) -> str:
    return (cfg.metrics_dir / f"paperless_{cfg.entity}_{job}.prom").read_text(encoding="utf-8")


class TestWriteMetrics:
    def test_writes_node_exporter_textfile_format(self, cfg: Config):
        write_metrics(
            cfg,
            job="ingest",
            metrics=[
                Metric(
                    name="paperless_ingest_spool_depth",
                    help_text="Spool jobs still waiting",
                    metric_type="gauge",
                    value=3.0,
                )
            ],
        )
        text = _read(cfg, "ingest")
        assert "# HELP paperless_ingest_spool_depth Spool jobs still waiting" in text
        assert "# TYPE paperless_ingest_spool_depth gauge" in text
        assert 'paperless_ingest_spool_depth{entity="crs"} 3.0' in text

    def test_every_series_carries_the_entity_label(self, cfg: Config):
        # A single unlabelled series would let a healthy instance mask a broken
        # one, which is the classic way a second tenant goes unmonitored.
        write_metrics(
            cfg,
            job="ingest",
            metrics=[
                Metric(name="a", help_text="x", metric_type="gauge", value=1.0),
                Metric(name="b", help_text="y", metric_type="counter", value=2.0),
            ],
        )
        body = [line for line in _read(cfg, "ingest").splitlines() if not line.startswith("#")]
        assert all('entity="crs"' in line for line in body)

    def test_one_file_per_job_so_jobs_do_not_clobber_each_other(self, cfg: Config):
        write_metrics(
            cfg, job="ingest", metrics=[Metric(name="a", help_text="x", metric_type="gauge", value=1.0)]
        )
        write_metrics(
            cfg, job="export", metrics=[Metric(name="b", help_text="y", metric_type="gauge", value=2.0)]
        )
        assert "a{" in _read(cfg, "ingest")
        assert "b{" in _read(cfg, "export")

    def test_replaces_the_previous_run(self, cfg: Config):
        write_metrics(
            cfg, job="ingest", metrics=[Metric(name="a", help_text="x", metric_type="gauge", value=1.0)]
        )
        write_metrics(
            cfg, job="ingest", metrics=[Metric(name="a", help_text="x", metric_type="gauge", value=9.0)]
        )
        assert _read(cfg, "ingest").count("a{") == 1
        assert "9.0" in _read(cfg, "ingest")

    def test_leaves_no_temporary_file_behind(self, cfg: Config):
        # The collector reads the whole directory, so a stray temp file would be
        # scraped as a duplicate series.
        write_metrics(
            cfg, job="ingest", metrics=[Metric(name="a", help_text="x", metric_type="gauge", value=1.0)]
        )
        assert [p.name for p in cfg.metrics_dir.iterdir()] == ["paperless_crs_ingest.prom"]

    def test_creates_the_directory(self, cfg: Config):
        assert not cfg.metrics_dir.exists()
        write_metrics(cfg, job="ingest", metrics=[])
        assert cfg.metrics_dir.is_dir()


class TestJournal:
    def test_appends_one_json_object_per_line(self, cfg: Config):
        journal(cfg, {"event": "archived", "document_id": 1})
        journal(cfg, {"event": "reject", "document_id": 2})
        lines = (cfg.journal_dir / "tap.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "archived"

    def test_stamps_the_time_and_the_entity(self, cfg: Config):
        journal(cfg, {"event": "archived"})
        record = json.loads((cfg.journal_dir / "tap.jsonl").read_text(encoding="utf-8"))
        assert record["entity"] == "crs"
        assert record["ts"].endswith("+00:00")

    def test_survives_a_record_holding_something_json_cannot_encode(self, cfg: Config):
        # The journal is an audit trail. Losing a decision because a value was
        # awkward would be worse than recording its repr.
        journal(cfg, {"event": "archived", "path": cfg.data_dir})
        record = json.loads((cfg.journal_dir / "tap.jsonl").read_text(encoding="utf-8"))
        assert str(cfg.data_dir) in record["path"]


class TestState:
    def test_counters_survive_a_restart(self, cfg: Config):
        first = State(cfg)
        first.bump("archived_total", 3)
        first.save()
        # Alerts fire on increases, so a counter that resets to zero looks like
        # a drop and hides whatever it was counting.
        assert State(cfg).get("archived_total") == 3

    def test_starts_empty_when_there_is_no_file(self, cfg: Config):
        assert State(cfg).get("archived_total") == 0

    def test_bump_returns_the_new_value(self, cfg: Config):
        state = State(cfg)
        assert state.bump("x") == 1
        assert state.bump("x", 4) == 5

    def test_leaves_no_temporary_file_behind(self, cfg: Config):
        state = State(cfg)
        state.set("x", 1)
        state.save()
        assert [p.name for p in cfg.state_dir.iterdir()] == ["state.json"]
