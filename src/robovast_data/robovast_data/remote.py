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
rows as an Arrow stream, so every column arrives in its type and a list column as a list.
``frames()`` and ``pointclouds()`` fetch each message from the service's frame and points
routes, whole. Nothing is downloaded but the answer, and nothing needs the campaign's
archive.

What differs from a campaign on disk, and says so rather than guessing:

* a query takes no parameters (write the values into the SQL);
* a loop over a run's frames or clouds is one request per message here and one pass over
  the recording there, which is what a download is for;
* ``config()`` reads a configuration's files from the campaign directory, which the service
  does not serve through these routes: download the campaign for that.

Only the standard library speaks HTTP here, so the package gains no dependency for it.
"""

from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np
import pandas as pd
import pyarrow as pa
from robovast_decode import images

from .bulk import Frame, PointCloud
from .data import Reader
from .statement import QueryError

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


def _header(headers, name: str, default: str = "") -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return default


def _ipc(frame: pd.DataFrame) -> str:
    """*frame* as an Arrow IPC stream, base64: how a caller's table travels with a query."""
    table = pa.Table.from_pandas(frame, preserve_index=False)
    out = io.BytesIO()
    with pa.ipc.new_stream(out, table.schema) as writer:
        writer.write_table(table)
    return base64.b64encode(out.getvalue()).decode("ascii")


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

    def _request(self, url: str, params: Optional[dict] = None, body: Optional[bytes] = None,
                 headers: Optional[dict] = None) -> Tuple[bytes, dict]:
        """``(body, headers)`` of one request: a GET, or a POST when *body* is given."""
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(url + query, data=body,
                                         method="POST" if body is not None else "GET")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with _open(request, self.timeout) as response:
                return response.read(), dict(getattr(response, "headers", None) or {})
        except urllib.error.HTTPError as exc:
            raise self._error(exc.code, exc.read()) from exc

    def _campaign_url(self, route: str, data_plane: bool = False) -> str:
        campaign = urllib.parse.quote(self.campaign_id, safe="")
        plane = "/data" if data_plane else ""
        return f"{self.base_url}{plane}/campaigns/{campaign}{route}"

    def _get(self, route: str, params: Optional[dict] = None) -> bytes:
        return self._request(self._campaign_url(route), params)[0]

    def _data(self, route: str, params: dict) -> Tuple[bytes, dict]:
        return self._request(self._campaign_url(route, data_plane=True), params)

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

    def _frame(self, sql: str, params=None, tables: Optional[Dict[str, pd.DataFrame]] = None
               ) -> pd.DataFrame:
        if params:
            raise QueryError("a query to a service takes no parameters; write the values "
                             "into the SQL")
        payload = {"sql": sql}
        if tables:
            payload["tables"] = {name: _ipc(frame) for name, frame in tables.items()}
        body, _ = self._request(self._campaign_url("/query.arrow"),
                                body=json.dumps(payload).encode("utf-8"),
                                headers={"Content-Type": "application/json"})
        reader = pa.ipc.open_stream(pa.py_buffer(body))
        result = reader.read_all()
        problems = json.loads((reader.schema.metadata or {}).get(b"problems", b"[]"))
        if problems:
            shown = "\n  ".join(problems[:10])
            more = f"\n  ... {len(problems) - 10} more" if len(problems) > 10 else ""
            warnings.warn(f"{len(problems)} table(s) could not be built for some runs; the "
                          f"answer leaves them out:\n  {shown}{more}", stacklevel=4)
        return result.to_pandas()

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

    def sql(self, query: str, params=None,
            tables: Optional[Dict[str, pd.DataFrame]] = None) -> pd.DataFrame:
        """Any ``SELECT`` over the campaign's tables and views, as a DataFrame.

        *tables* are DataFrames of your own the query may name beside the campaign's; they
        travel with the query and are registered for it on the service.
        """
        return self._frame(query, params, tables)

    def config(self, name: str):
        """Not served: a configuration's files are read from the campaign directory."""
        raise NotImplementedError(
            f"config({name!r}) reads a configuration's files from the campaign directory, "
            "which the service does not serve through these routes; download the campaign "
            "(`vast campaign download`) and open that")

    # -- images and point clouds: from the recording on the service --------------------------

    def frame_times(self, config: str, run: int, topic: str) -> List[float]:
        """The stamp of every frame of *topic* of run *config*/*run*, in seconds."""
        body, _ = self._data("/frame-index", {"run": f"{config}/{int(run)}", "topic": topic})
        return [float(t) for t in json.loads(body)["times"]]

    def frames(self, config: str, run: int, topic: str, start: Optional[float] = None,
               end: Optional[float] = None, every: Optional[float] = None) -> Iterator[Frame]:
        """Every frame of *topic* of run *config*/*run*, in order, one request each.

        The same frames as :meth:`Data.frames` gives from a downloaded campaign, at full
        resolution; *start*, *end* and *every* select as there. One round trip per frame,
        so a loop over a long run is what a download is for.
        """
        times = [t for t in self.frame_times(config, run, topic)
                 if (start is None or t >= start) and (end is None or t <= end)]
        next_keep = None if every is None else (start if start is not None else float("-inf"))
        for t in times:
            if next_keep is not None:
                if t < next_keep:
                    continue
                next_keep = max(next_keep, t) + every
            yield self.frame(config, run, topic, t)

    def frame(self, config: str, run: int, topic: str, t: Optional[float] = None) -> Frame:
        """The frame of *topic* at or before *t* seconds (the first when none is; the
        newest for ``None``), whole: a raw image's pixels in their own dtype, a compressed
        one decoded."""
        params = {"run": f"{config}/{int(run)}", "topic": topic, "full": "1"}
        if t is not None:
            params["t"] = repr(float(t))
        body, headers = self._data("/frame", params)
        stamp = float(_header(headers, "X-Frame-Time"))
        if _header(headers, "Content-Type").startswith("application/x-npy"):
            pixels = np.load(io.BytesIO(body), allow_pickle=False)
            encoding = _header(headers, "X-Frame-Encoding")
        else:
            pixels, encoding = images.decode_compressed(body)
        return Frame(stamp, int(round(stamp * 1e9)), topic, pixels, encoding,
                     _header(headers, "X-Frame-Id"))

    def pointclouds(self, config: str, run: int, topic: str, start: Optional[float] = None,
                    end: Optional[float] = None, every: Optional[float] = None,
                    keep_nan: bool = False) -> Iterator[PointCloud]:
        """Every point cloud of *topic* of run *config*/*run*, in order, one request each:
        the service steps through the topic from *start*, one cloud strictly after the
        last."""
        t = None if start is None else start - 1e-9
        while True:
            try:
                cloud = self._pointcloud(config, run, topic, t, keep_nan, after=True)
            except FileNotFoundError:
                return
            if end is not None and cloud.t > end:
                return
            yield cloud
            t = cloud.t if every is None else cloud.t + every - 1e-9

    def pointcloud(self, config: str, run: int, topic: str, t: Optional[float] = None,
                   keep_nan: bool = False) -> PointCloud:
        """The cloud of *topic* at or before *t* seconds (the first when none is; the last
        for ``None``): one array per field, and ``xyz`` stacked."""
        return self._pointcloud(config, run, topic, t, keep_nan, after=False)

    def _pointcloud(self, config: str, run: int, topic: str, t: Optional[float],
                    keep_nan: bool, after: bool) -> PointCloud:
        params = {"run": f"{config}/{int(run)}", "topic": topic}
        if t is not None:
            params["t"] = repr(float(t))
        if after:
            params["after"] = "1"
        body, headers = self._data("/points", params)
        table = pa.ipc.open_stream(pa.py_buffer(body)).read_all()
        fields = {}
        for name in table.column_names:
            column = table.column(name).combine_chunks()
            if pa.types.is_fixed_size_list(column.type):
                fields[name] = column.flatten().to_numpy().reshape(-1, column.type.list_size)
            else:
                fields[name] = column.to_numpy()
        stamp = float(_header(headers, "X-Frame-Time"))
        return PointCloud(stamp, int(round(stamp * 1e9)), topic, fields,
                          _header(headers, "X-Frame-Id"), keep_nan)


__all__ = ["RemoteCampaign", "is_url", "parse_campaign_url"]
