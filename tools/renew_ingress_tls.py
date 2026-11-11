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

"""Renew the Let's Encrypt certificate a published service serves from a TLS secret.

For a service published with ``vast cluster setup --tls-secret``: nothing in the cluster
renews that secret, and a Let's Encrypt certificate lasts 90 days. This reads the host and
the secret from the service's Ingress, has certbot (in a container) request a new
certificate by DNS-01 -- it shows the TXT record to set at the DNS provider and waits for
it -- and, once the person confirms, writes the certificate into the secret. The ingress
controller serves it from there without a restart.

A service whose Ingress names a cert-manager issuer is renewed by cert-manager and refused
here.

Settings, each read from the environment, the project ``.env`` or the user's env file:

* ``ROBOVAST_KUBE_CONTEXT`` / ``ROBOVAST_NAMESPACE`` -- where the service runs
  (``--context`` / ``--namespace``);
* ``ROBOVAST_TLS_EMAIL`` -- the Let's Encrypt account address (``--email``; asked when unset);
* ``ROBOVAST_TLS_DIR`` -- certbot's account and certificates (``--dir``; default
  ``~/.config/robovast/letsencrypt``).

Usage:
    python tools/renew_ingress_tls.py [--check] [--staging] [--context CTX] [--namespace NS]
"""

import argparse
import base64
import datetime
import os
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path

from robovast.common.env_file import load_env_file
from robovast.execution.cluster_execution.kube_client import load_kube_config
from robovast.execution.cluster_execution.service_deploy import SERVICE_NAME

#: The certbot image that requests the certificate.
CERTBOT_IMAGE = "certbot/certbot:v3.0.1"

#: Annotations by which cert-manager manages an Ingress's certificate itself.
CERT_MANAGER_ANNOTATIONS = ("cert-manager.io/issuer", "cert-manager.io/cluster-issuer")

#: Days of validity left below which `--check` exits 1; Let's Encrypt renews at 30.
RENEW_WITHIN_DAYS = 30


def _confirm(prompt):
    return input(f"{prompt} [y/N] ").strip().lower().startswith("y")


def ingress_tls(ingress):
    """The host and the TLS secret an Ingress serves, or SystemExit saying why it has none to renew."""
    annotations = ingress.metadata.annotations or {}
    managed = [a for a in CERT_MANAGER_ANNOTATIONS if a in annotations]
    if managed:
        raise SystemExit(f"The Ingress {ingress.metadata.name} names {managed[0]}="
                         f"{annotations[managed[0]]}: cert-manager renews its certificate.")
    tls = ingress.spec.tls or []
    if not tls:
        raise SystemExit(f"The Ingress {ingress.metadata.name} serves no TLS; nothing to renew.")
    if len(tls) != 1 or len(tls[0].hosts or []) != 1 or not tls[0].secret_name:
        raise SystemExit(f"The Ingress {ingress.metadata.name} serves TLS other than one host "
                         "from one secret, which this does not renew.")
    return tls[0].hosts[0], tls[0].secret_name


def not_after(pem):
    """When a PEM certificate expires, as an aware UTC datetime."""
    out = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem, text=True,
                         capture_output=True, check=False)
    if out.returncode or not out.stdout.startswith("notAfter="):
        raise SystemExit(f"openssl could not read the certificate: {out.stderr.strip()}")
    stamp = " ".join(out.stdout.removeprefix("notAfter=").split())
    return datetime.datetime.strptime(stamp, "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=datetime.timezone.utc)


def _served(host):
    """When the certificate the host serves expires, or why that could not be read."""
    try:
        pem = ssl.get_server_certificate((host, 443), timeout=10)
    except OSError as err:
        return None, f"could not be read ({err})"
    expiry = not_after(pem)
    days = (expiry - datetime.datetime.now(datetime.timezone.utc)).days
    state = "expired" if days < 0 else f"{days} days left"
    return expiry, f"valid until {expiry:%Y-%m-%d %H:%M} UTC ({state})"


def _certbot(host, email, state_dir, staging):
    """Request a certificate for `host` by DNS-01, certbot asking the person in the terminal."""
    state_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm", "-it", "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{state_dir}:/le", CERTBOT_IMAGE, "certonly",
           "--manual", "--preferred-challenges", "dns",
           "--config-dir", "/le/config", "--work-dir", "/le/work", "--logs-dir", "/le/logs",
           "--cert-name", host, "-d", host, "--email", email]
    if staging:
        cmd.append("--staging")
    if subprocess.run(cmd, check=False).returncode:  # noqa: S603
        raise SystemExit("certbot did not issue a certificate; the secret is unchanged.")
    live = state_dir / "config" / "live" / host
    return (live / "fullchain.pem").read_text(), (live / "privkey.pem").read_text()


def _write_secret(core, namespace, name, cert, key):
    from kubernetes import client
    data = {"tls.crt": base64.b64encode(cert.encode()).decode(),
            "tls.key": base64.b64encode(key.encode()).decode()}
    try:
        core.patch_namespaced_secret(name, namespace, {"data": data})
    except client.ApiException as err:
        if err.status != 404:
            raise
        core.create_namespaced_secret(namespace, client.V1Secret(
            metadata=client.V1ObjectMeta(name=name), type="kubernetes.io/tls", data=data))


def main():
    try:
        load_env_file()
    except ValueError as err:
        raise SystemExit(str(err)) from err
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", default=os.environ.get("ROBOVAST_KUBE_CONTEXT"),
                        help="kubeconfig context (ROBOVAST_KUBE_CONTEXT)")
    parser.add_argument("--namespace", default=os.environ.get("ROBOVAST_NAMESPACE", "default"),
                        help="namespace the service runs in (ROBOVAST_NAMESPACE)")
    parser.add_argument("--email", default=os.environ.get("ROBOVAST_TLS_EMAIL", ""),
                        help="Let's Encrypt account address (ROBOVAST_TLS_EMAIL)")
    parser.add_argument("--dir", type=Path,
                        default=Path(os.environ.get("ROBOVAST_TLS_DIR",
                                                    "~/.config/robovast/letsencrypt")),
                        help="certbot's account and certificates (ROBOVAST_TLS_DIR)")
    parser.add_argument("--staging", action="store_true",
                        help="Let's Encrypt's staging service: an untrusted certificate, never "
                             "written into the secret; to try the DNS record")
    parser.add_argument("--check", action="store_true",
                        help=f"only report the served certificate; exit 1 within "
                             f"{RENEW_WITHIN_DAYS} days of its expiry")
    args = parser.parse_args()

    from kubernetes import client
    load_kube_config(context=args.context)
    core = client.CoreV1Api()
    if not shutil.which("openssl"):
        raise SystemExit("openssl is needed to read certificates and is not on PATH.")
    try:
        ingress = client.NetworkingV1Api().read_namespaced_ingress(SERVICE_NAME, args.namespace)
    except client.ApiException as err:
        if err.status == 404:
            raise SystemExit(f"No Ingress {SERVICE_NAME} in namespace {args.namespace}: the service "
                             "is not published here.") from err
        raise
    host, secret = ingress_tls(ingress)
    expiry, served = _served(host)
    print(f"Host:     {host}")
    print(f"Secret:   {args.namespace}/{secret} (context {args.context or 'the active one'})")
    print(f"Serving:  {served}")
    if args.check:
        left = expiry and expiry - datetime.datetime.now(datetime.timezone.utc)
        sys.exit(0 if left and left.days >= RENEW_WITHIN_DAYS else 1)

    if not shutil.which("docker"):
        raise SystemExit("docker is needed to run certbot and is not on PATH.")
    email = args.email or input("\nLet's Encrypt account email (ROBOVAST_TLS_EMAIL): ").strip()
    if not email:
        raise SystemExit("An email address is required for the Let's Encrypt account.")
    if not _confirm(f"\nRequest a new certificate for {host}"
                    f"{' from the staging service' if args.staging else ''}?"):
        raise SystemExit("Nothing requested.")
    print(f"\ncertbot shows a value for the TXT record _acme-challenge.{host}. Set it at your DNS "
          "provider, replacing any value left from an earlier request, and press Enter in certbot "
          "once a public resolver returns it:\n"
          f"  dig +short TXT _acme-challenge.{host} @1.1.1.1\n")
    cert, key = _certbot(host, email, args.dir.expanduser(), args.staging)
    issued = not_after(cert)
    print(f"\nIssued:   valid until {issued:%Y-%m-%d %H:%M} UTC")
    if args.staging:
        print("A staging certificate is not trusted by any client; the secret is unchanged.")
        return
    if not _confirm(f"Write it into {args.namespace}/{secret}?"):
        raise SystemExit(f"The secret is unchanged; the certificate is in {args.dir}.")
    _write_secret(core, args.namespace, secret, cert, key)
    for _ in range(12):
        expiry, served = _served(host)
        if expiry == issued:
            print(f"Serving:  {served}")
            return
        time.sleep(5)
    print(f"Serving:  {served} -- the ingress controller has not picked up the new secret yet; "
          f"check again with `make renew-tls CHECK=1`.")


if __name__ == "__main__":
    main()
