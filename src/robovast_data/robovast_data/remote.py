# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""A campaign on a RoboVAST service, read with the same calls as one on disk.

``Campaign("https://<service>/campaigns/<campaign_id>", token=...)`` answers ``runs``,
``tables``, ``table()`` and ``sql()`` by sending the SQL to the service, which builds what the
query names from the campaign's records exactly as it would for the web UI, and returns the
rows as CSV. Nothing is downloaded but the answer, and nothing needs the campaign's archive.

What differs from a campaign on disk, and says so rather than guessing:

* the frames are typed by pandas from the CSV, where a local read keeps the table's types;
* a query takes no parameters (write the values into the SQL);
* ``config()`` reads a configuration's files from the campaign directory, which the service
  does not serve through these routes: download the campaign for that.

Only the standard library speaks HTTP here, so the package gains no dependency for it.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

import pandas as pd

from .data import Reader
from .statement import QueryError

#: ``<service>/campaigns/<campaign_id>``: the service's base URL, then the campaign.
_CAMPAIGN_URL = re.compile(r"^(?P<base>https?://.+?)/campaigns/(?P<campaign>[^/?#]+)/?$")


def is_url(path) -> bool:
    """Whether *path* names a service rather than a file."""
    return isinstance(path, str) and path.lower().startswith(("http://", "https://"))


def parse_campaign_url(url: str) -> Tuple[str, str]:
    """``(service base URL, campaign id)`` from ``<service>/campaigns/<campaign_id>``."""
    match = _CAMPAIGN_URL.match(url.strip())
    if not match:
        raise ValueError(f"{url!r} does not name a campaign on a service; the form is "
                         "https://<service>/campaigns/<campaign_id>")
    return match.group("base"), urllib.parse.unquote(match.group("campaign"))


def _open(request: urllib.request.Request, timeout: float):
    """The one place a request leaves the process."""
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - http(s) only


class RemoteCampaign(Reader):
    """One campaign on a service. Build it with :class:`~robovast_data.Campaign` or
    :func:`~robovast_data.open_data` on its URL."""

    def __init__(self, url: str, token: Optional[str] = None, timeout: float = 600.0):
        self.base_url, self.campaign_id = parse_campaign_url(url)
        self.token = token
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"<RemoteCampaign {self.campaign_id} on {self.base_url}>"

    # -- transport -----------------------------------------------------------------------

    def _get(self, route: str, params: Optional[dict] = None) -> bytes:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        campaign = urllib.parse.quote(self.campaign_id, safe="")
        request = urllib.request.Request(
            f"{self.base_url}/campaigns/{campaign}{route}{query}")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with _open(request, self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise self._error(exc.code, exc.read()) from exc

    def _error(self, status: int, body: bytes) -> Exception:
        try:
            detail = json.loads(body).get("detail", "")
        except (ValueError, AttributeError):
            detail = body.decode("utf-8", "replace")[:500]
        where = f"{self.campaign_id} on {self.base_url}"
        if status in (401, 403):
            return PermissionError(
                f"{where} refused the request ({status}): pass token= (`vast service token` "
                f"prints the one this service accepts). {detail}".strip())
        if status == 404:
            return FileNotFoundError(f"{where}: {detail or 'no such campaign'}")
        if status == 400:
            return QueryError(str(detail))
        return RuntimeError(f"{where} answered {status}: {detail}")

    # -- the calls a campaign on disk answers ---------------------------------------------

    def _frame(self, sql: str, params=None) -> pd.DataFrame:
        if params:
            raise QueryError("a query to a service takes no parameters; write the values "
                             "into the SQL")
        body = self._get("/query.csv", {"sql": sql})
        if not body.strip():
            return pd.DataFrame()
        return pd.read_csv(io.BytesIO(body))

    @property
    def runs(self) -> pd.DataFrame:
        """One row per run: outcome, host, ``param_*`` per varied factor."""
        return self._frame("SELECT * FROM runs ORDER BY campaign_id, config_name, run_id "
                           "NULLS LAST")

    @property
    def tables(self) -> pd.DataFrame:
        """What can be read here: every table, how many runs it is built for, and the views."""
        described = json.loads(self._get("/describe"))
        rows = [{"name": t["table"] if t.get("schema") in (None, "", "main")
                 else f"{t['schema']}.{t['table']}",
                 "kind": t.get("kind"), "runs": t.get("runs"), "built": t.get("built"),
                 "failed": len(t.get("failed") or {}), "columns": len(t.get("columns") or [])}
                for t in described.get("tables", [])]
        return pd.DataFrame(rows).sort_values("name", ignore_index=True) if rows \
            else pd.DataFrame(rows)

    def sql(self, query: str, params=None) -> pd.DataFrame:
        """Any ``SELECT`` over the campaign's tables and views, as a DataFrame."""
        return self._frame(query, params)

    def config(self, name: str):
        """Not served: a configuration's files are read from the campaign directory."""
        raise NotImplementedError(
            f"config({name!r}) reads a configuration's files from the campaign directory, "
            "which the service does not serve through these routes; download the campaign "
            "(`vast campaign download`) and open that")


__all__ = ["RemoteCampaign", "is_url", "parse_campaign_url"]
