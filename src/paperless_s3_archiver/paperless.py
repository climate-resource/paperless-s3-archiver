"""
The paperless-ngx REST API

Paperless is the index and the UI, not the archive of record. This client reads
what a document currently is, and writes back only the per-class permission
grants that keep personnel material out of general view.
"""

import os
from typing import Any

import requests

#: Long enough for a slow page of documents, short enough that a wedged instance
#: fails the job rather than hanging a timer unit forever.
TIMEOUT_SECONDS = 60

#: Paperless caps page size; 250 keeps the number of round trips low without
#: asking for a page it will refuse.
PAGE_SIZE = 250


class MissingToken(Exception):
    """No paperless API token in the environment."""


class PaperlessAPI:
    """A thin authenticated wrapper over one instance's REST API."""

    def __init__(self, *, api_base: str, entity: str, token: str | None = None) -> None:
        token = token if token is not None else os.environ.get("PAPERLESS_ARCHIVE_API_TOKEN", "")
        if not token:
            raise MissingToken(f"No paperless API token in the environment for {entity}")
        self.base = api_base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Token {token}", "Accept": "application/json"})

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        """
        GET one resource

        Parameters
        ----------
        path
            The path below the API base, starting with a slash.
        **params
            Query parameters.

        Returns
        -------
        :
            The decoded response body.
        """
        resp = self.session.get(f"{self.base}{path}", params=params, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    def patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        PATCH one resource

        Parameters
        ----------
        path
            The path below the API base, starting with a slash.
        payload
            The JSON body.

        Returns
        -------
        :
            The decoded response body.
        """
        resp = self.session.patch(f"{self.base}{path}", json=payload, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        return body

    def all_pages(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """
        Every result from a paginated collection

        Parameters
        ----------
        path
            The collection's path, starting with a slash.
        **params
            Query parameters, applied to every page.

        Returns
        -------
        :
            Every result, in the order the API returned them.
        """
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get(path, page=page, page_size=PAGE_SIZE, **params)
            out.extend(data.get("results", []))
            if not data.get("next"):
                return out
            page += 1

    def group_id(self, name: str) -> int | None:
        """
        The id of a Django group, by name

        Parameters
        ----------
        name
            The group's name.

        Returns
        -------
        :
            The id, or ``None`` when no such group exists.
        """
        for group in self.all_pages("/groups/"):
            if group.get("name") == name:
                return int(group["id"])
        return None

    def user_id(self, username: str) -> int | None:
        """
        The id of a user, by username

        Parameters
        ----------
        username
            The username.

        Returns
        -------
        :
            The id, or ``None`` when no such user exists.
        """
        for user in self.all_pages("/users/"):
            if user.get("username") == username:
                return int(user["id"])
        return None
