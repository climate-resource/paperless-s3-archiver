"""
Counters that have to survive a restart, because alerts fire on increases
"""

import json
from typing import Any

from paperless_s3_archiver.config import Config


class State:
    """
    One entity's persistent counters

    Written atomically, so a job killed mid-write leaves the previous state
    rather than an unparseable file. A counter that resets to zero looks like a
    drop to Prometheus and hides whatever it was counting.
    """

    def __init__(self, cfg: Config) -> None:
        self.path = cfg.state_dir / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.data = {}

    def bump(self, key: str, amount: int = 1) -> int:
        """
        Add to a counter

        Parameters
        ----------
        key
            The counter's name.
        amount
            How much to add.

        Returns
        -------
        :
            The new value.
        """
        self.data[key] = int(self.data.get(key, 0)) + amount
        return self.data[key]

    def get(self, key: str, default: Any = 0) -> Any:
        """
        Read a counter

        Parameters
        ----------
        key
            The counter's name.
        default
            What to return when it has never been set.

        Returns
        -------
        :
            The stored value, or `default`.
        """
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """
        Overwrite a value

        Parameters
        ----------
        key
            The counter's name.
        value
            The value to store.
        """
        self.data[key] = value

    def save(self) -> None:
        """Write the state out atomically."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
