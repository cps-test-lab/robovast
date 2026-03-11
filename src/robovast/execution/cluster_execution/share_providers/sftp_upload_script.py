#!/usr/bin/env python3
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
"""
Upload a run archive to a remote host via SFTP.

Runs inside the archiver sidecar (python:3.12-alpine). Installs paramiko at
startup if it is not already available.
The archive must already exist at /data/{campaign}.tar.gz.

Usage: python - <campaign>  (script from stdin)
  or:  python sftp_upload_script.py <campaign>

Environment variables (all set by SftpShareProvider.build_pod_env):
  ROBOVAST_SFTP_HOST       SFTP server hostname or IP address
  ROBOVAST_SFTP_USER       SFTP username
  ROBOVAST_SFTP_REMOTE_DIR Remote directory path
  ROBOVAST_SFTP_PORT       (optional) Server port, default 22
  ROBOVAST_SFTP_PASSWORD   (optional) Password for password-based auth
  ROBOVAST_SFTP_KEY_PEM    (optional) PEM-encoded private key for key-based auth

Progress lines are written to stdout in the format:
  <campaign>  [████████░░░░░░░░░░░░]  xx.x%  X.X MiB  X.X MiB/s
"""

import os
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# Ensure paramiko is available (not bundled in the archiver image)
# ---------------------------------------------------------------------------
try:
    import paramiko  # noqa: E402
except ImportError:
    sys.stdout.write("Installing paramiko…\n")
    sys.stdout.flush()
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "paramiko"],
        check=True,
    )
    import paramiko  # noqa: E402

# ---------------------------------------------------------------------------
# Progress bar helpers (match nextcloud_upload_script.py / gcs_upload_script.py)
# ---------------------------------------------------------------------------
BAR_WIDTH = 20
CLEAR_EOL = "\033[K"


def _fmt_size(n):
    return f"{n / 1024 / 1024:.1f} MiB"


def _fmt_rate(bps):
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.1f} KiB/s"
    return f"{bps:.0f} B/s"


class _ProgressCallback:
    """Paramiko get/put callback that renders a progress bar to stdout."""

    def __init__(self, total: int, campaign: str) -> None:
        self._total = total
        self._campaign = campaign
        self._last_pct = -1.0
        self._start = time.monotonic()

    def __call__(self, transferred: int, total: int) -> None:
        if total <= 0:
            return
        pct = transferred / total * 100
        if pct - self._last_pct < 1.0 and transferred < total:
            return
        self._last_pct = pct
        elapsed = max(time.monotonic() - self._start, 1e-6)
        rate = transferred / elapsed
        filled = int(BAR_WIDTH * transferred / total)
        progress_bar = "█" * filled + "░" * (BAR_WIDTH - filled)
        line = (
            f"{self._campaign}  [{progress_bar}]  {pct:5.1f}%  "
            f"{_fmt_size(transferred)}/{_fmt_size(total)}  {_fmt_rate(rate)}"
        )
        sys.stdout.write("\r" + line + CLEAR_EOL)
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# SFTP upload
# ---------------------------------------------------------------------------

def _load_pkey(pem: str):
    """Parse a PEM-encoded private key string, trying common key types."""
    import io  # pylint: disable=import-outside-toplevel

    for cls in (
        paramiko.RSAKey,
        paramiko.Ed25519Key,
        paramiko.ECDSAKey,
    ):
        try:
            return cls.from_private_key(io.StringIO(pem))
        except paramiko.SSHException:
            continue
    raise ValueError("Could not parse ROBOVAST_SFTP_KEY_PEM as a known key type.")


def upload(campaign: str) -> None:
    archive_path = f"/data/{campaign}.tar.gz"

    if not os.path.isfile(archive_path):
        sys.stderr.write(f"ERROR: archive not found: {archive_path}\n")
        sys.exit(1)

    host = os.environ["ROBOVAST_SFTP_HOST"]
    port = int(os.environ.get("ROBOVAST_SFTP_PORT", "22"))
    user = os.environ["ROBOVAST_SFTP_USER"]
    remote_dir = os.environ["ROBOVAST_SFTP_REMOTE_DIR"].rstrip("/")
    password = os.environ.get("ROBOVAST_SFTP_PASSWORD", "") or None
    key_pem = os.environ.get("ROBOVAST_SFTP_KEY_PEM", "") or None

    if not password and not key_pem:
        sys.stderr.write(
            "ERROR: either ROBOVAST_SFTP_PASSWORD or ROBOVAST_SFTP_KEY_PEM must be set.\n"
        )
        sys.exit(1)

    total = os.path.getsize(archive_path)
    filename = os.path.basename(archive_path)
    remote_path = f"{remote_dir}/{filename}"

    sys.stdout.write(f"{campaign}  connecting to {host}:{port}…\n")
    sys.stdout.flush()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = {"username": user, "port": port}
    if key_pem:
        connect_kwargs["pkey"] = _load_pkey(key_pem)
    elif password:
        connect_kwargs["password"] = password

    try:
        ssh.connect(host, **connect_kwargs)
    except paramiko.AuthenticationException as exc:
        sys.stderr.write(f"ERROR: authentication failed for {user}@{host}: {exc}\n")
        sys.exit(1)
    except paramiko.SSHException as exc:
        sys.stderr.write(f"ERROR: SSH error connecting to {host}:{port}: {exc}\n")
        sys.exit(1)

    try:
        sftp = ssh.open_sftp()
        try:
            sys.stdout.write(f"{campaign}  uploading via SFTP…\n")
            sys.stdout.flush()

            callback = _ProgressCallback(total, campaign)
            sftp.put(archive_path, remote_path, callback=callback)
        finally:
            sftp.close()
    finally:
        ssh.close()

    sys.stdout.write(
        "\r" + f"{campaign}  uploaded ({_fmt_size(total)})  ✓" + CLEAR_EOL + "\n"
    )
    sys.stdout.flush()


def main():
    if len(sys.argv) < 2:
        sys.stderr.write(
            "Usage: python - <campaign_id>  "
            "(ROBOVAST_SFTP_HOST / _USER / _REMOTE_DIR must be set)\n"
        )
        sys.exit(1)

    campaign = sys.argv[1]

    for var in ("ROBOVAST_SFTP_HOST", "ROBOVAST_SFTP_USER", "ROBOVAST_SFTP_REMOTE_DIR"):
        if not os.environ.get(var):
            sys.stderr.write(f"ERROR: {var} environment variable is not set\n")
            sys.exit(1)

    upload(campaign)


if __name__ == "__main__":
    main()
