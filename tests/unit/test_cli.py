"""
The command line: dispatch, config resolution, and how a refusal exits
"""

import json
from pathlib import Path

import pytest

from paperless_s3_archiver.cli import COMMANDS, build_parser, entry_point, main
from paperless_s3_archiver.s3 import MissingCredentials

from ..conftest import config_dict


class TestParser:
    def test_every_command_is_reachable(self):
        parser = build_parser()
        for name in COMMANDS:
            args = parser.parse_args([name, *(["target"] if "export" in name and name != "export" else [])])
            assert args.command == name

    def test_requires_a_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_extend_retention_defaults_to_a_dry_run(self):
        args = build_parser().parse_args(["extend-retention"])
        assert args.apply is False
        assert args.reason == ""

    def test_the_class_filter_is_spelled_class_on_the_command_line(self):
        args = build_parser().parse_args(["extend-retention", "--class", "receipts"])
        assert args.retention_class == "receipts"

    def test_peers_are_repeatable(self):
        args = build_parser().parse_args(["validate", "--peer", "a.json", "--peer", "b.json"])
        assert args.peer == ["a.json", "b.json"]


class TestConfigResolution:
    def test_reads_the_config_named_on_the_command_line(
        self, config_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("PAPERLESS_ARCHIVE_CONFIG", raising=False)
        assert main(["--config", str(config_file), "validate"]) == 0

    def test_falls_back_to_the_environment(self, config_file: Path, monkeypatch: pytest.MonkeyPatch):
        # This is how the systemd units and the container supply it.
        monkeypatch.setenv("PAPERLESS_ARCHIVE_CONFIG", str(config_file))
        assert main(["validate"]) == 0

    def test_refuses_with_no_config_at_all(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("PAPERLESS_ARCHIVE_CONFIG", raising=False)
        with pytest.raises(SystemExit):
            main(["validate"])

    def test_the_runtime_flag_overrides_the_config(self, config_file: Path, monkeypatch: pytest.MonkeyPatch):
        seen = {}
        monkeypatch.setattr(
            "paperless_s3_archiver.cli.COMMANDS",
            {**COMMANDS, "validate": lambda cfg, _args: seen.update(runtime=cfg.runtime) or 0},
        )
        main(["--config", str(config_file), "--runtime", "docker", "validate"])
        assert seen["runtime"] == "docker"


class TestValidateCommand:
    def test_passes_a_sound_config(self, config_file: Path, capsys):
        assert main(["--config", str(config_file), "validate"]) == 0
        # It always says which mode a run would write under.
        assert "Object Lock mode" in capsys.readouterr().out

    def test_fails_a_config_with_an_error(self, tmp_path: Path, capsys):
        path = tmp_path / "archive.json"
        path.write_text(
            json.dumps(config_dict(tmp_path, grants={"futura": {"final_payment_year": 2030}})),
            encoding="utf-8",
        )
        assert main(["--config", str(path), "validate"]) == 1
        assert "ERROR" in capsys.readouterr().out

    def test_reports_a_divergent_shared_grant_without_failing(self, tmp_path: Path, capsys):
        # Two entities are allowed to hold the same grant on different periods.
        # It has to be seen, not refused.
        crs = tmp_path / "crs.json"
        cr = tmp_path / "cr.json"
        crs.write_text(
            json.dumps(
                config_dict(
                    tmp_path, entity="crs", grants={"quicca": {"final_payment_year": 2031, "years": 5}}
                )
            ),
            encoding="utf-8",
        )
        cr.write_text(
            json.dumps(
                config_dict(
                    tmp_path, entity="cr", grants={"quicca": {"final_payment_year": 2030, "years": 5}}
                )
            ),
            encoding="utf-8",
        )

        assert main(["--config", str(crs), "validate", "--peer", str(cr)]) == 0
        out = capsys.readouterr().out
        assert "DIVERGENT" in out
        assert "quicca" in out


class TestEntryPoint:
    def test_a_refusal_exits_two_with_a_message_and_no_traceback(
        self, config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        # A refusal is this program declining to do something permanent on
        # incomplete information. A traceback would suggest a bug instead.
        def refuse(_cfg, _args):
            raise MissingCredentials("No writer credentials in the environment for crs")

        monkeypatch.setenv("PAPERLESS_ARCHIVE_CONFIG", str(config_file))
        monkeypatch.setattr("paperless_s3_archiver.cli.COMMANDS", {**COMMANDS, "tap": refuse})
        monkeypatch.setattr("sys.argv", ["paperless-archive", "tap"])

        with pytest.raises(SystemExit) as exit_info:
            entry_point()

        assert exit_info.value.code == 2
        assert "refused: No writer credentials" in capsys.readouterr().err

    def test_passes_a_commands_exit_status_through(self, config_file: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PAPERLESS_ARCHIVE_CONFIG", str(config_file))
        monkeypatch.setattr("paperless_s3_archiver.cli.COMMANDS", {**COMMANDS, "tap": lambda _cfg, _args: 1})
        monkeypatch.setattr("sys.argv", ["paperless-archive", "tap"])

        with pytest.raises(SystemExit) as exit_info:
            entry_point()
        assert exit_info.value.code == 1
