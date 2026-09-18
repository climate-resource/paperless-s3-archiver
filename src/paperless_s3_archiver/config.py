"""
The entity's archive configuration, and the paths it implies

One config file describes one legal entity. Everything that differs between
entities lives here, so no code in this package has to know which entity it is
running for -- which is what makes a second entity a configuration change rather
than a fork.

The file is JSON because it is generated: by Ansible today, by a Kubernetes
ConfigMap later. It is validated on load rather than trusted, because most of
what it carries feeds an arithmetic whose result cannot be corrected downwards
once it has been written to a locked object.
"""

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Where per-entity run state lives when the config does not say otherwise.
#: Counters live here rather than in the data tree, because they describe the
#: jobs rather than the archive.
DEFAULT_STATE_ROOT = Path("/var/lib/paperless-archive")

#: The clock a class's retain-until date is measured from.
#:
#: ``document_year``   the calendar year of the document itself
#: ``grant``           the final payment year of the grant it belongs to
#: ``employment_end``  the year an employment ended
#: ``none``            no clock, because the class is never archived
Clock = Literal["document_year", "grant", "employment_end", "none"]


class RetentionClass(BaseModel):
    """
    One retention class from the entity's own table

    A document carries exactly one ``class:<name>`` tag, and this is what that
    tag resolves to. The class decides the retain-until date *and* whether the
    document is archived at all.
    """

    model_config = ConfigDict(extra="forbid")

    archive: bool
    """Whether documents in this class are written to the bucket at all.

    ``False`` means indexed and searchable but never locked, which is what keeps
    material we may be obliged to erase under Art. 17 GDPR erasable.
    """

    years: int | None = None
    """Years added to whichever year `clock` names. ``None`` where the period
    comes from somewhere else, as it does for a grant."""

    clock: Clock = "document_year"

    restricted: bool = False
    """Personnel material: left out of the auditor export. Who can see it in
    paperless is the instance's own permission model, not this tool's."""

    basis: str = ""
    """The statute or agreement the period comes from. Carried into every
    sidecar, so an auditor reads the reason beside the date."""

    covers: str = ""
    """What belongs in this class, in the language the documents use."""

    @model_validator(mode="after")
    def _archived_document_year_classes_state_a_period(self) -> "RetentionClass":
        """An archived class on the document-year clock must say how many years.

        Without it the tap has no retain-until date to compute, and a class that
        cannot produce a date is a class that refuses every document in it.
        """
        if self.archive and self.clock == "document_year" and not (self.years and self.years > 0):
            raise ValueError("an archived class on the document_year clock must declare a positive `years`")
        if self.archive and self.clock == "employment_end" and not (self.years and self.years > 0):
            raise ValueError("an archived class on the employment_end clock must declare a positive `years`")
        return self


class Grant(BaseModel):
    """
    One grant agreement's retention period

    Both fields come from the agreement and are never assumed. A ``grant:<slug>``
    tag naming a slug with no entry here is a refusal, not a guess, so a typo
    costs a re-tag rather than a wrong date locked for a decade.
    """

    model_config = ConfigDict(extra="allow")

    final_payment_year: int | None = None
    years: int | None = None

    @property
    def complete(self) -> bool:
        """Whether this entry can produce a retain-until date."""
        return self.final_payment_year is not None and self.years is not None


class Employment(BaseModel):
    """
    One person's employment, for the classes whose clock starts when it ends

    ``end_year`` unset means the employment continues, so the clock has not
    started. Those documents are archived under a legal hold with a floor
    retain-until rather than left to unlock early.
    """

    model_config = ConfigDict(extra="allow")

    end_year: int | None = None


class Config(BaseModel):
    """One entity's archive configuration."""

    model_config = ConfigDict(extra="ignore")

    entity: str
    display_name: str
    hostname: str

    data_dir: Path
    metrics_dir: Path
    state_root: Path = DEFAULT_STATE_ROOT

    container_media_root: str
    container_export_root: str

    api_base: str

    bucket: str
    endpoint: str
    region: str

    object_lock_mode: str
    burn_in_retain_days: int
    export_retain_years: int

    class_tag_prefix: str = "class:"
    grant_tag_prefix: str = "grant:"
    employment_tag_prefix: str = "employment:"

    retention_classes: Annotated[dict[str, RetentionClass], Field(min_length=1)]
    """Per entity, and with no default. Periods come from that entity's own law
    and its actual grant agreements; starting from another entity's table is how
    a wrong period becomes permanent."""

    grants: dict[str, Grant] = Field(default_factory=dict)
    employments: dict[str, Employment] = Field(default_factory=dict)

    auditors_group: str = "auditors"
    breakglass_user: str = "breakglass"

    public_enabled: bool = False
    public_until: str = ""

    #: How the jobs reach the deployment they belong to. Set by the CLI from
    #: `--runtime`, not by the config file, because it describes where the
    #: program is running rather than which entity it is running for.
    runtime: str = "docker"
    compose_dir: Path | None = None

    @model_validator(mode="after")
    def _exposed_instances_carry_an_expiry(self) -> "Config":
        """Audit mode is easy to open because it closes on its own.

        An open window with no expiry date is a hole that stays open, so it is
        refused outright rather than merely reported.
        """
        if self.public_enabled and not self.public_until.strip():
            raise ValueError(
                "public_enabled is set but public_until is empty. Anything that relies on "
                'somebody remembering to close a hole is a hole that stays open. Set "YYYY-MM-DD".'
            )
        return self

    @property
    def spool_dir(self) -> Path:
        """Where the post-consume hook leaves job files for the tap to drain."""
        return self.data_dir / "spool"

    @property
    def media_dir(self) -> Path:
        """Originals and archive renditions, as paperless stores them."""
        return self.data_dir / "media"

    @property
    def export_dir(self) -> Path:
        """`document_exporter` output."""
        return self.data_dir / "export"

    @property
    def journal_dir(self) -> Path:
        """The local record of what the tap decided, including what it refused."""
        return self.data_dir / "journal"

    @property
    def state_dir(self) -> Path:
        """Per-entity counters that have to survive a restart."""
        return self.state_root / self.entity

    @property
    def burn_in(self) -> bool:
        """
        True while the bucket is in GOVERNANCE mode

        Everything written during burn-in is disposable. Nothing counts as
        archived until it is re-ingested under COMPLIANCE with real dates.
        """
        return self.object_lock_mode.upper() != "COMPLIANCE"

    def archived_classes(self) -> set[str]:
        """The classes whose documents are written to the bucket."""
        return {name for name, spec in self.retention_classes.items() if spec.archive}

    def tax_and_grant_classes(self) -> set[str]:
        """
        The classes an auditor may see

        Archived, and not personnel material. A Betriebsprüfer's access covers
        tax records, not personnel files.
        """
        return {name for name, spec in self.retention_classes.items() if spec.archive and not spec.restricted}


def load_config(path: Path) -> Config:
    """
    Read and validate one entity's config

    Parameters
    ----------
    path
        The rendered JSON config for a single entity.

    Returns
    -------
    :
        The validated config.

    Raises
    ------
    pydantic.ValidationError
        If the file describes a configuration that could not be archived
        correctly. Refusing here is much cheaper than refusing per document
        after some of them are already locked.
    """
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    # The rendered file carries `_comment` keys explaining itself to whoever
    # reads it on the host. They are documentation, not configuration.
    raw = {key: value for key, value in raw.items() if not key.startswith("_")}
    return Config.model_validate(raw)
