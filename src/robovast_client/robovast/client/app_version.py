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

    The distributions are tried in order of how much they say about the running code:
    ``robovast`` when it is installed, then ``robovast-client``, which is installed in
    *every* case and so is the last answer that is still a fact. ``0.0.0+unknown`` remains
    only for a source tree with no metadata at all — it used to be what a perfectly good
    client-only install reported, which read as a broken install rather than a small one.
    """
    try:
        from robovast.common.execution import \
            get_app_version  # pylint: disable=import-outside-toplevel
        return get_app_version()
    except Exception:  # noqa: BLE001 - version reporting must never break the handshake
        for dist in ("robovast", "robovast-client"):
            try:
                return _pkg_version(dist)
            except PackageNotFoundError:
                continue
        return "0.0.0+unknown"  # a source tree with no metadata
