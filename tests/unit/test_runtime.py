"""
The seam between the archive logic and the deployment
"""

import subprocess
from pathlib import Path

import pytest

from paperless_b2_archiver.runtime import DockerRuntime, Runtime, get_runtime


class FakeRuntime:
    """What a second deployment's implementation has to provide."""

    def __init__(self, *, tunnel_up: bool = False) -> None:
        self.tunnel_up = tunnel_up
        self.exported_to: list[str] = []

    def run_document_exporter(self, destination: str) -> None:
        self.exported_to.append(destination)

    def tunnel_running(self) -> bool:
        return self.tunnel_up


class TestProtocol:
    def test_the_docker_runtime_satisfies_it(self):
        assert isinstance(DockerRuntime(entity="crs", compose_dir=Path("/opt")), Runtime)

    def test_a_deployment_only_has_to_provide_two_methods(self):
        # The whole point of the seam: a Kubernetes implementation is these two
        # methods, not a rewrite of the archive logic.
        assert isinstance(FakeRuntime(), Runtime)


class TestGetRuntime:
    def test_builds_the_docker_runtime(self):
        runtime = get_runtime(name="docker", entity="crs", compose_dir=Path("/opt/paperless/crs"))
        assert isinstance(runtime, DockerRuntime)

    def test_refuses_docker_without_a_compose_directory(self):
        with pytest.raises(ValueError, match="compose_dir"):
            get_runtime(name="docker", entity="crs", compose_dir=None)

    def test_refuses_an_unknown_runtime(self):
        with pytest.raises(ValueError, match="unknown runtime"):
            get_runtime(name="podman", entity="crs", compose_dir=Path("/opt"))


class TestDockerRuntime:
    def test_runs_the_exporter_in_the_webserver_container(self, monkeypatch: pytest.MonkeyPatch):
        calls = []
        monkeypatch.setattr("paperless_b2_archiver.runtime.shutil.which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            "paperless_b2_archiver.runtime.subprocess.run",
            lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        )

        DockerRuntime(entity="crs", compose_dir=Path("/opt/paperless/crs")).run_document_exporter(
            "/usr/src/paperless/export/full"
        )

        cmd = calls[0]
        assert cmd[:2] == ["/usr/bin/docker", "compose"]
        assert "--project-directory" in cmd
        assert "/opt/paperless/crs" in cmd
        assert "document_exporter" in cmd
        # --delete keeps the export tree from growing without bound; the other
        # two shape what the filter has to work with.
        assert {"--split-manifest", "--delete", "--no-thumbnail"} <= set(cmd)

    def test_reports_the_tunnel_as_up(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("paperless_b2_archiver.runtime.shutil.which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            "paperless_b2_archiver.runtime.subprocess.run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="true\n"),
        )
        assert DockerRuntime(entity="crs", compose_dir=Path("/opt")).tunnel_running() is True

    def test_reports_the_tunnel_as_down(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("paperless_b2_archiver.runtime.shutil.which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            "paperless_b2_archiver.runtime.subprocess.run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=""),
        )
        assert DockerRuntime(entity="crs", compose_dir=Path("/opt")).tunnel_running() is False

    def test_asks_about_this_entitys_container_only(self, monkeypatch: pytest.MonkeyPatch):
        # Reporting another entity's tunnel as this one's exposure would put the
        # wrong entity on the dashboard during an audit.
        calls = []
        monkeypatch.setattr("paperless_b2_archiver.runtime.shutil.which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            "paperless_b2_archiver.runtime.subprocess.run",
            lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="true"),
        )
        DockerRuntime(entity="cr", compose_dir=Path("/opt")).tunnel_running()
        assert "paperless-cr-cloudflared" in calls[0]

    def test_a_failure_to_ask_reports_not_exposed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("paperless_b2_archiver.runtime.shutil.which", lambda _: "/usr/bin/docker")

        def boom(*_args, **_kwargs):
            raise OSError("no docker socket")

        monkeypatch.setattr("paperless_b2_archiver.runtime.subprocess.run", boom)
        assert DockerRuntime(entity="crs", compose_dir=Path("/opt")).tunnel_running() is False

    def test_refuses_when_docker_is_not_installed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("paperless_b2_archiver.runtime.shutil.which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="not on PATH"):
            DockerRuntime(entity="crs", compose_dir=Path("/opt")).run_document_exporter("/dest")
