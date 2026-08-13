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

"""Bootstrap the hostname and certificate `vast exec cluster setup --ingress-host` needs.

Assembling cert-manager issuers by hand is the step most likely to stall an operator, so
this asks for what it cannot detect and prints the two things needed next: the setup
command, and a short block to hand the users.

It **detects before it asks** — the IngressClass, the node addresses, whether
cert-manager is installed — so the common case is answering one question about the
certificate and pressing enter through the rest.

On the certificate, the choice that matters:

* **Let's Encrypt DNS-01**, when a domain is available. A publicly trusted certificate
  for a name that resolves to a *private* address; Let's Encrypt reads a DNS record and
  never connects to the host. Nothing to install on any client.
* **A self-signed CA** otherwise. Fully offline, but the CA has to be trusted **per
  browser, not per machine** — macOS Keychain and the Windows store do not cover
  Firefox, and on Linux neither Firefox nor Chrome reads the system store. For a handful
  of users on mixed machines that is a recurring support cost, which is why DNS-01 is
  offered first even though it needs a domain.

Usage:
    python tools/setup_ingress_tls.py [--context CTX] [--namespace NS] [--yes]
"""

import argparse
import base64
import subprocess
import sys
from pathlib import Path

# The script lives in tools/; robovast itself is importable from the venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from robovast.common.kube import load_kube_config  # noqa: E402
from robovast.execution.cluster_execution.service_deploy import (  # noqa: E402
    SERVICE_NAME, AUTH_SECRET_NAME)

#: The cert-manager release this installs when it is missing.
CERT_MANAGER_VERSION = "v1.16.2"

#: Names created here, referenced by `vast exec cluster setup --issuer`.
SELFSIGNED_ISSUER = "robovast-selfsigned"
CA_ISSUER = "robovast-ca"
CA_SECRET = "robovast-ca-key-pair"


def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, text=True, **kwargs)  # noqa: S603


def _ask(prompt, default="", assume_yes=False):
    if assume_yes:
        return default
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _confirm(prompt, assume_yes=False):
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower().startswith("y")


def detect(core, networking):
    """What the cluster already tells us, so the operator is not asked for it."""
    facts = {}
    classes = [i.metadata.name for i in networking.list_ingress_class().items]
    facts["ingress_classes"] = classes

    addresses = []
    for node in core.list_node().items:
        for address in node.status.addresses or []:
            if address.type in ("ExternalIP", "InternalIP"):
                addresses.append(address.address)
    facts["node_addresses"] = addresses

    namespaces = [n.metadata.name for n in core.list_namespace().items]
    facts["cert_manager"] = "cert-manager" in namespaces
    return facts


def install_cert_manager(assume_yes):
    """Install cert-manager with helm, which is already a prerequisite (Kueue)."""
    if not _confirm("cert-manager is not installed. Install it now?", assume_yes):
        raise SystemExit(
            "cert-manager is required for an issued certificate. Install it, or re-run "
            "and choose an existing TLS secret.")
    _run(["helm", "repo", "add", "jetstack", "https://charts.jetstack.io"])
    _run(["helm", "repo", "update"])
    _run(["helm", "upgrade", "--install", "cert-manager", "jetstack/cert-manager",
          "--namespace", "cert-manager", "--create-namespace",
          "--version", CERT_MANAGER_VERSION, "--set", "crds.enabled=true",
          "--wait"])


def selfsigned_manifests(namespace):
    """The standard cert-manager bootstrap chain: selfsigned -> CA cert -> CA issuer."""
    return [
        {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer",
         "metadata": {"name": SELFSIGNED_ISSUER},
         "spec": {"selfSigned": {}}},
        {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
         "metadata": {"name": "robovast-ca", "namespace": "cert-manager"},
         "spec": {"isCA": True, "commonName": "RoboVAST local CA",
                  "secretName": CA_SECRET,
                  "privateKey": {"algorithm": "ECDSA", "size": 256},
                  "issuerRef": {"name": SELFSIGNED_ISSUER, "kind": "ClusterIssuer",
                                "group": "cert-manager.io"}}},
        {"apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer",
         "metadata": {"name": CA_ISSUER},
         "spec": {"ca": {"secretName": CA_SECRET}}},
    ]


def dns01_manifests(email, provider, secret_name):
    """A Let's Encrypt ClusterIssuer solving DNS-01.

    Deliberately only the shape: every provider's solver block differs, and inventing
    one for a provider nobody named would produce an issuer that silently never solves.
    """
    return [{
        "apiVersion": "cert-manager.io/v1", "kind": "ClusterIssuer",
        "metadata": {"name": CA_ISSUER},
        "spec": {"acme": {
            "server": "https://acme-v02.api.letsencrypt.org/directory",
            "email": email,
            "privateKeySecretRef": {"name": "robovast-acme-key"},
            "solvers": [{"dns01": {provider: {"apiTokenSecretRef": {
                "name": secret_name, "key": "api-token"}}}}],
        }},
    }]


def apply(manifests, assume_yes):
    """Show the manifests, then apply them. Cluster-admin work should be reviewable."""
    import yaml
    rendered = yaml.safe_dump_all(manifests, sort_keys=False)
    print("\nAbout to apply:\n")
    print(rendered)
    if not _confirm("Apply these?", assume_yes):
        raise SystemExit("Nothing applied.")
    _run(["kubectl", "apply", "-f", "-"], input=rendered)


def ca_certificate(core):
    """The CA certificate to hand out, once cert-manager has issued it."""
    try:
        secret = core.read_namespaced_secret(CA_SECRET, "cert-manager")
    except Exception:  # noqa: BLE001 - not issued yet is the only interesting case
        return ""
    return base64.b64decode((secret.data or {}).get("ca.crt", "")).decode()


def handout(url, ca_path, mcp=True):
    """The block an operator pastes to their users. Short, or it does not get read."""
    lines = [f"RoboVAST is at {url}", "Password: <the access token>", ""]
    if ca_path:
        lines += [
            f"First, trust our certificate — attached: {ca_path.name}",
            "  Chrome/Edge (macOS/Windows): double-click the file, mark as trusted.",
            f"  Chrome (Linux):  certutil -d sql:$HOME/.pki/nssdb -A -t \"C,,\" "
            f"-n robovast-ca -i {ca_path.name}",
            "  Firefox (all):   Settings > Privacy & Security > Certificates > View",
            "                   Certificates > Authorities > Import, and tick",
            "                   \"Trust this CA to identify websites\".",
            "",
        ]
    lines += [f"Command line (optional):  pip install robovast && vast login {url}"]
    if mcp:
        lines += [f"Claude Code (optional):   claude mcp add --transport http robovast \\",
                  f"                            {url}/mcp --header \"Authorization: Bearer <token>\""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--context", default=None, help="kubeconfig context")
    parser.add_argument("--namespace", default="default",
                        help="namespace the robovast-service runs in")
    parser.add_argument("--yes", action="store_true",
                        help="accept every default; for a re-run or CI")
    args = parser.parse_args()

    from kubernetes import client
    load_kube_config(context=args.context)
    core = client.CoreV1Api()
    networking = client.NetworkingV1Api()

    facts = detect(core, networking)
    if not facts["ingress_classes"]:
        raise SystemExit(
            "No IngressClass in this cluster, so an Ingress would never be served.\n"
            "rke2 ships ingress-nginx unless it was disabled at install; otherwise "
            "install an ingress controller first.")
    print(f"IngressClass:   {', '.join(facts['ingress_classes'])}")
    print(f"Node addresses: {', '.join(facts['node_addresses']) or '(none found)'}")
    print(f"cert-manager:   {'installed' if facts['cert_manager'] else 'not installed'}")

    default_host = ""
    if facts["node_addresses"]:
        # sslip.io resolves <anything>.<ip>.sslip.io to that IP, which gives a real
        # hostname on a LAN with no DNS to administer.
        default_host = f"robovast.{facts['node_addresses'][0]}.sslip.io"
    host = _ask("\nHostname for RoboVAST", default_host, args.yes)
    if not host:
        raise SystemExit("A hostname is required.")
    ingress_class = _ask("IngressClass", facts["ingress_classes"][0], args.yes)

    print("\nCertificate:")
    print("  1) Let's Encrypt DNS-01  — trusted everywhere, nothing for users to install")
    print("     (needs a domain you control and a DNS provider API token)")
    print("  2) self-signed CA        — fully offline; each browser must trust the CA")
    print("  3) an existing TLS secret")
    choice = _ask("Choose", "2", args.yes)

    ca_path = None
    issuer = tls_secret = ""
    if choice == "3":
        tls_secret = _ask("TLS secret name", "", args.yes)
        if not tls_secret:
            raise SystemExit("A secret name is required for option 3.")
    else:
        if not facts["cert_manager"]:
            install_cert_manager(args.yes)
        if choice == "1":
            email = _ask("Let's Encrypt account email", "", args.yes)
            provider = _ask("DNS provider solver key (e.g. cloudflare, route53)",
                            "cloudflare", args.yes)
            secret_name = _ask("Secret holding the provider API token",
                               "robovast-dns-token", args.yes)
            if not email:
                raise SystemExit("An email address is required for Let's Encrypt.")
            apply(dns01_manifests(email, provider, secret_name), args.yes)
            print(f"\nNote: create the '{secret_name}' Secret with your provider's API "
                  "token under the key 'api-token' if it does not exist yet.")
        else:
            apply(selfsigned_manifests(args.namespace), args.yes)
            cert = ca_certificate(core)
            if cert:
                ca_path = Path("robovast-ca.crt")
                ca_path.write_text(cert)
                print(f"\nCA certificate written to {ca_path} — distribute this.")
            else:
                print("\ncert-manager has not issued the CA yet; re-run in a moment to "
                      "write out robovast-ca.crt.")
        issuer = CA_ISSUER

    scheme = "https"
    url = f"{scheme}://{host}"
    print("\n" + "=" * 72)
    print("Next, deploy the service with:\n")
    flag = f"--tls-secret {tls_secret}" if tls_secret else f"--issuer {issuer}"
    print(f"  vast exec cluster setup <flavor> \\\n"
          f"      --ingress-host {host} --ingress-class {ingress_class} {flag}")
    print("\n" + "=" * 72)
    print("Then give your users this:\n")
    text = handout(url, ca_path)
    print(text)
    Path("robovast-users.txt").write_text(text + "\n")
    print(f"\n(also written to robovast-users.txt; fill in the access token, which "
          f"`kubectl -n {args.namespace} get secret {AUTH_SECRET_NAME} "
          f"-o jsonpath='{{.data.ROBOVAST_AUTH_TOKEN}}' | base64 -d` prints)")
    print(f"\nThe service itself is {SERVICE_NAME} in namespace {args.namespace}.")


if __name__ == "__main__":
    main()
