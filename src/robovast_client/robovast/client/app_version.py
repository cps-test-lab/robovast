# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The version of the code *this process is running*.

Lives here rather than beside the in-process service because both sides of the version
handshake need it: the HTTP client reports its own version, the service reports the one it
loaded. Keeping it in ``local_transport`` meant a client could not ask "what am I running?"
without importing the whole in-process server.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def running_version() -> str:
    """The running code's version: git revision if available, else package metadata.

    The preference is the point. A service is long-lived and loads its code once, so a
    client needs to tell "the fix I just made is loaded" from "this process predates it".
    The packaged version alone cannot — it stays ``2.0.0`` across every edit.

    Never raises. The git-revision path needs ``robovast.common.execution``, which a
    client-only install does not have, so the import is inside the ``try`` with everything
    else: a version string is diagnostic, and failing to produce one must not break the
    handshake it is part of.
    """
    try:
        from robovast.common.execution import \
            get_app_version  # pylint: disable=import-outside-toplevel
        return get_app_version()
    except Exception:  # noqa: BLE001 - version reporting must never break the handshake
        try:
            return _pkg_version("robovast")
        except PackageNotFoundError:  # editable/source without metadata
            return "0.0.0+unknown"
