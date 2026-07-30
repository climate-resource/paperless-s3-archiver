"""
metrics: the facts that are cheap to check often
"""

import argparse
import datetime as dt
import logging

import requests

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.observability import Metric, write_metrics
from paperless_s3_archiver.paperless import MissingToken, PaperlessAPI
from paperless_s3_archiver.runtime import get_runtime

LOG = logging.getLogger("paperless-archive")


def cmd_metrics(cfg: Config, args: argparse.Namespace) -> int:
    """
    Report exposure, break-glass logins and liveness

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments. Unused.

    Returns
    -------
    :
        0. A paperless that cannot be reached is reported through
        ``paperless_instance_up`` rather than through the exit status, because
        this job's own failure and the instance being down are different alerts.
    """
    del args
    now = dt.datetime.now(dt.UTC)

    exposure_active = 0.0
    remaining = 0.0
    if cfg.public_enabled:
        try:
            runtime = get_runtime(name=cfg.runtime, entity=cfg.entity, compose_dir=cfg.compose_dir)
            exposure_active = 1.0 if runtime.tunnel_running() else 0.0
        except (ValueError, OSError) as exc:
            LOG.error("could not establish whether the tunnel is up: %s", exc)
            exposure_active = 0.0
        if cfg.public_until:
            until = dt.datetime.fromisoformat(cfg.public_until).replace(tzinfo=dt.UTC)
            remaining = (until - now).total_seconds()

    breakglass = 0.0
    up = 0.0
    try:
        api = PaperlessAPI(api_base=cfg.api_base, entity=cfg.entity)
        for user in api.all_pages("/users/"):
            if user.get("username") == cfg.breakglass_user and user.get("last_login"):
                stamp = dt.datetime.fromisoformat(user["last_login"].replace("Z", "+00:00"))
                breakglass = stamp.timestamp()
        up = 1.0
    except (requests.RequestException, MissingToken, ValueError) as exc:
        LOG.error("metrics: paperless unreachable: %s", exc)

    write_metrics(
        cfg,
        job="status",
        metrics=[
            Metric(
                name="paperless_public_exposure_active",
                help_text="1 while this entity's Cloudflare Tunnel is running and the archive is "
                "reachable from the internet",
                metric_type="gauge",
                value=exposure_active,
            ),
            Metric(
                name="paperless_public_exposure_seconds_remaining",
                help_text="Seconds until public_until. Negative means the window should already have closed",
                metric_type="gauge",
                value=remaining,
            ),
            Metric(
                name="paperless_breakglass_last_login_timestamp_seconds",
                help_text=(
                    "Unix time the break-glass superuser last logged in. Any increase is worth noticing"
                ),
                metric_type="gauge",
                value=breakglass,
            ),
            Metric(
                name="paperless_instance_up",
                help_text="1 if the paperless API answered",
                metric_type="gauge",
                value=up,
            ),
        ],
    )
    return 0
