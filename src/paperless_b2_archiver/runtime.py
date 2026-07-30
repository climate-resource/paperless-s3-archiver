"""
The seam between the archive logic and however paperless happens to be deployed

Almost everything this package does is arithmetic over a config and calls to two
network APIs, and none of that cares how paperless is run. Exactly two things do:
asking the application to produce an export, and asking whether the audit-mode
tunnel is up. Both are behind :class:`Runtime`.

Keeping them behind an interface is what makes a deployment change a new
implementation rather than a rewrite, and it is what lets the export and metrics
jobs be tested without a container runtime present.
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

LOG = logging.getLogger("paperless-archive")

#: An export of a few thousand documents with OCR renditions takes minutes, not
#: seconds. Bounded anyway, so a wedged instance fails the nightly job rather
#: than holding it open until the next one starts.
EXPORT_TIMEOUT_SECONDS = 3 * 60 * 60


@runtime_checkable
class Runtime(Protocol):
    """What the archive jobs need from the deployment paperless runs under."""

    def run_document_exporter(self, destination: str) -> None:
        """
        Run paperless's `document_exporter` into a path inside the application

        Parameters
        ----------
        destination
            The destination directory, as the *application* sees it. The caller
            reads the result back through the shared data directory.
        """
        ...

    def tunnel_running(self) -> bool:
        """
        Whether this entity's audit-mode tunnel is currently up

        Returns
        -------
        :
            True while the archive is reachable from the internet.
        """
        ...


class DockerRuntime:
    """
    paperless as a docker compose project on the local host

    The jobs run on the host, outside the compose project, holding credentials
    that never enter a container. Reaching into the project to run a management
    command is therefore an explicit `docker compose exec`, not an in-process
    call.
    """

    def __init__(self, *, entity: str, compose_dir: Path) -> None:
        self.entity = entity
        self.compose_dir = compose_dir

    def run_document_exporter(self, destination: str) -> None:
        """
        Run `document_exporter` in the webserver container

        Parameters
        ----------
        destination
            The destination directory inside the container.

        Raises
        ------
        subprocess.CalledProcessError
            If the exporter exits non-zero.
        """
        subprocess.run(  # noqa: S603
            [
                self._docker(),
                "compose",
                "--project-directory",
                str(self.compose_dir),
                "exec",
                "-T",
                "webserver",
                "document_exporter",
                destination,
                "--split-manifest",
                "--delete",
                "--no-thumbnail",
            ],
            check=True,
            timeout=EXPORT_TIMEOUT_SECONDS,
        )

    def tunnel_running(self) -> bool:
        """
        Whether this entity's cloudflared container is running

        Returns
        -------
        :
            True while the tunnel is up. False on any failure to ask, because a
            metric that cannot be established is reported as "not exposed" and
            the accompanying liveness metric is what catches a broken check.
        """
        try:
            proc = subprocess.run(  # noqa: S603
                [
                    self._docker(),
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    f"paperless-{self.entity}-cloudflared",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.error("could not ask docker whether the tunnel is up: %s", exc)
            return False
        return proc.stdout.strip() == "true"

    @staticmethod
    def _docker() -> str:
        """
        The absolute path to the docker binary

        Returns
        -------
        :
            The resolved path.

        Raises
        ------
        FileNotFoundError
            When docker is not on the path, which on this deployment means the
            job is running somewhere it was not meant to.
        """
        found = shutil.which("docker")
        if found is None:
            raise FileNotFoundError("docker is not on PATH, so the compose project cannot be reached")
        return found


def get_runtime(*, name: str, entity: str, compose_dir: Path | None) -> Runtime:
    """
    Build the runtime named in the config

    Parameters
    ----------
    name
        The runtime's name. Only ``docker`` exists today.
    entity
        The entity slug, which names the containers.
    compose_dir
        The compose project directory, required by the docker runtime.

    Returns
    -------
    :
        The runtime.

    Raises
    ------
    ValueError
        For an unknown runtime, or a docker runtime with no compose directory.
    """
    if name == "docker":
        if compose_dir is None:
            raise ValueError("the docker runtime needs `compose_dir` in the config")
        return DockerRuntime(entity=entity, compose_dir=compose_dir)
    raise ValueError(f"unknown runtime '{name}'")
