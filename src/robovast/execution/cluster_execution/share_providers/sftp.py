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

"""SFTP share provider for ``cluster upload-to-share``."""

import os

import click
import paramiko

from robovast.common.execution import is_campaign_dir

from .base import BaseShareProvider

__all__ = ["SftpShareProvider"]


class SftpShareProvider(BaseShareProvider):
    """Upload campaign archives to a remote host via SFTP (using paramiko).

    Authentication supports both password and private-key.  When
    ``ROBOVAST_SFTP_KEY_FILE`` is set it takes precedence over
    ``ROBOVAST_SFTP_PASSWORD``.

    Required ``.env`` variables:

    .. list-table::
       :header-rows: 1

       * - Variable
         - Description
       * - ``ROBOVAST_SHARE_TYPE``
         - Must be ``sftp``
       * - ``ROBOVAST_SFTP_HOST``
         - SFTP server hostname or IP address
       * - ``ROBOVAST_SFTP_USER``
         - SFTP username
       * - ``ROBOVAST_SFTP_REMOTE_DIR``
         - Absolute path of the remote directory where archives are stored

    Optional ``.env`` variables:

    .. list-table::
       :header-rows: 1

       * - Variable
         - Description
       * - ``ROBOVAST_SFTP_PORT``
         - Server port (default: ``22``)
       * - ``ROBOVAST_SFTP_PASSWORD``
         - Password for password-based auth (use key auth when possible)
       * - ``ROBOVAST_SFTP_KEY_FILE``
         - Path to a PEM-encoded private-key file for key-based auth
    """

    SHARE_TYPE = "sftp"

    # ------------------------------------------------------------------
    # BaseShareProvider interface
    # ------------------------------------------------------------------

    def required_env_vars(self) -> dict[str, str]:
        return {
            "ROBOVAST_SFTP_HOST": "SFTP server hostname or IP address",
            "ROBOVAST_SFTP_USER": "SFTP username",
            "ROBOVAST_SFTP_REMOTE_DIR": (
                "Absolute remote directory path for storing archives"
            ),
        }

    def build_pod_env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "ROBOVAST_SFTP_HOST": os.environ["ROBOVAST_SFTP_HOST"],
            "ROBOVAST_SFTP_USER": os.environ["ROBOVAST_SFTP_USER"],
            "ROBOVAST_SFTP_REMOTE_DIR": os.environ["ROBOVAST_SFTP_REMOTE_DIR"],
        }

        port = os.environ.get("ROBOVAST_SFTP_PORT", "")
        if port:
            env["ROBOVAST_SFTP_PORT"] = port

        password = os.environ.get("ROBOVAST_SFTP_PASSWORD", "")
        if password:
            env["ROBOVAST_SFTP_PASSWORD"] = password

        key_file = os.environ.get("ROBOVAST_SFTP_KEY_FILE", "")
        if key_file:
            if not os.path.isfile(key_file):
                raise click.UsageError(
                    f"ROBOVAST_SFTP_KEY_FILE: file not found: {key_file}"
                )
            try:
                with open(key_file) as fh:
                    env["ROBOVAST_SFTP_KEY_PEM"] = fh.read()
            except OSError as exc:
                raise click.UsageError(
                    f"ROBOVAST_SFTP_KEY_FILE: could not read {key_file!r}: {exc}"
                ) from exc

        if not password and not key_file:
            raise click.UsageError(
                "Either ROBOVAST_SFTP_PASSWORD or ROBOVAST_SFTP_KEY_FILE must be set "
                "for share type 'sftp'."
            )

        return env

    # ------------------------------------------------------------------
    # Internal helper: open a paramiko SFTP connection
    # ------------------------------------------------------------------

    def _connect(self) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        """Open an SSH/SFTP connection using the current environment.

        Returns:
            A ``(ssh_client, sftp_client)`` tuple.  The caller is responsible
            for closing both when done.
        """
        host = os.environ["ROBOVAST_SFTP_HOST"]
        port = int(os.environ.get("ROBOVAST_SFTP_PORT", "22"))
        user = os.environ["ROBOVAST_SFTP_USER"]
        password = os.environ.get("ROBOVAST_SFTP_PASSWORD", "") or None
        # Host-side uses a key *file*; inside the controller pod the launcher
        # injects the key as inline PEM (ROBOVAST_SFTP_KEY_PEM) via build_pod_env.
        key_file = os.environ.get("ROBOVAST_SFTP_KEY_FILE", "") or None
        key_pem = os.environ.get("ROBOVAST_SFTP_KEY_PEM", "") or None

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = {"username": user, "port": port}
        if key_pem:
            connect_kwargs["pkey"] = self._load_pkey(key_pem)
        elif key_file:
            connect_kwargs["key_filename"] = key_file
        elif password:
            connect_kwargs["password"] = password

        ssh.connect(host, **connect_kwargs)
        sftp = ssh.open_sftp()
        return ssh, sftp

    @staticmethod
    def _load_pkey(pem: str):
        """Parse an inline PEM private key, trying the common key types."""
        import io  # pylint: disable=import-outside-toplevel

        for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
            try:
                return cls.from_private_key(io.StringIO(pem))
            except paramiko.SSHException:
                continue
        raise click.UsageError(
            "Could not parse ROBOVAST_SFTP_KEY_PEM as a known key type.")

    def upload_archive(
        self,
        archive_path: str,
        object_name: str,
        progress_callback=None,
    ) -> None:
        """Upload *archive_path* to the remote directory via SFTP ``put``."""
        if not os.path.isfile(archive_path):
            raise click.UsageError(f"archive not found: {archive_path}")

        remote_dir = os.environ["ROBOVAST_SFTP_REMOTE_DIR"].rstrip("/")
        remote_path = f"{remote_dir}/{object_name}"

        # paramiko's put callback is already (bytes_transferred, total_bytes) —
        # the same shape as progress_callback, so forward it directly.
        cb = progress_callback

        ssh, sftp = self._connect()
        try:
            sftp.put(archive_path, remote_path, callback=cb)
        finally:
            sftp.close()
            ssh.close()

    def verify_access(self) -> None:
        """Confirm the SFTP server accepts the credentials and the remote dir exists.

        Connects with the configured auth and ``stat``s
        ``ROBOVAST_SFTP_REMOTE_DIR``; any auth/connection/path error raises so
        the campaign does not start with broken delivery.
        """
        remote_dir = os.environ["ROBOVAST_SFTP_REMOTE_DIR"]
        try:
            ssh, sftp = self._connect()
        except Exception as exc:  # paramiko raises a variety of types
            raise click.UsageError(
                f"Cannot connect to SFTP server "
                f"{os.environ.get('ROBOVAST_SFTP_HOST')!r}: {exc}\n"
                "Check ROBOVAST_SFTP_HOST / _USER / _PASSWORD / _KEY_FILE."
            ) from exc
        try:
            sftp.stat(remote_dir)
        except IOError as exc:
            raise click.UsageError(
                f"SFTP remote directory {remote_dir!r} is not accessible: {exc}\n"
                "Check ROBOVAST_SFTP_REMOTE_DIR exists and is writable."
            ) from exc
        finally:
            sftp.close()
            ssh.close()

    # ------------------------------------------------------------------
    # Optional download interface
    # ------------------------------------------------------------------

    def list_campaign_archives_with_size(self) -> list[tuple[str, int]]:
        """List all ``campaign-*.tar.gz`` files in the remote directory.

        Returns:
            List of ``(filename, size_in_bytes)`` tuples sorted by filename.
        """
        remote_dir = os.environ["ROBOVAST_SFTP_REMOTE_DIR"]
        ssh, sftp = self._connect()
        try:
            entries = sftp.listdir_attr(remote_dir)
            result = [
                (attr.filename, attr.st_size if attr.st_size is not None else -1)
                for attr in entries
                if attr.filename.endswith(".tar.gz")
                and is_campaign_dir(attr.filename[: -len(".tar.gz")])
            ]
            result.sort(key=lambda t: t[0])
            return result
        finally:
            sftp.close()
            ssh.close()

    def download_archive(
        self,
        object_name: str,
        dest_path: str,
        progress_callback=None,
        resume_offset: int = 0,
    ) -> None:
        """Download *object_name* from the remote directory to *dest_path*.

        Args:
            object_name: Filename (not full path) of the archive on the server.
            dest_path: Local destination path.
            progress_callback: Optional ``(bytes_received, total_bytes)`` callable.
            resume_offset: Byte offset to resume downloading from.
        """
        remote_dir = os.environ["ROBOVAST_SFTP_REMOTE_DIR"]
        remote_path = f"{remote_dir.rstrip('/')}/{object_name}"

        ssh, sftp = self._connect()
        try:
            if resume_offset > 0:
                stat = sftp.stat(remote_path)
                total = stat.st_size or 0
                chunk_size = 256 * 1024
                received = resume_offset
                with sftp.open(remote_path, "rb") as remote_fh:
                    remote_fh.seek(resume_offset)
                    with open(dest_path, "ab") as local_fh:
                        while True:
                            chunk = remote_fh.read(chunk_size)
                            if not chunk:
                                break
                            local_fh.write(chunk)
                            received += len(chunk)
                            if progress_callback:
                                progress_callback(received, total)
            else:
                def _cb(transferred: int, total_bytes: int) -> None:
                    if progress_callback:
                        progress_callback(transferred, total_bytes)

                sftp.get(remote_path, dest_path, callback=_cb if progress_callback else None)
        finally:
            sftp.close()
            ssh.close()

    def remove_archive(self, object_name: str) -> None:
        """Remove *object_name* from the remote directory.

        Args:
            object_name: Filename (not full path) of the archive to remove.
        """
        remote_dir = os.environ["ROBOVAST_SFTP_REMOTE_DIR"]
        remote_path = f"{remote_dir.rstrip('/')}/{object_name}"

        ssh, sftp = self._connect()
        try:
            sftp.remove(remote_path)
        finally:
            sftp.close()
            ssh.close()
