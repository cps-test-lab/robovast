# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``tools/renew_ingress_tls.py``: which Ingress it renews, and how it reads a certificate's expiry."""

import datetime
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "renew_ingress_tls.py"


@pytest.fixture(name="tool")
def _tool():
    spec = importlib.util.spec_from_file_location("_tool_renew_ingress_tls", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ingress(annotations=None, tls=None):
    return SimpleNamespace(metadata=SimpleNamespace(name="robovast-service", annotations=annotations),
                           spec=SimpleNamespace(tls=tls))


def tls(hosts, secret):
    return SimpleNamespace(hosts=hosts, secret_name=secret)


def test_the_host_and_the_secret_come_from_the_ingress(tool):
    assert tool.ingress_tls(ingress(tls=[tls(["robovast.example.org"], "robovast-tls")])) \
        == ("robovast.example.org", "robovast-tls")


@pytest.mark.parametrize("annotation", ["cert-manager.io/issuer", "cert-manager.io/cluster-issuer"])
def test_an_ingress_cert_manager_renews_is_refused(tool, annotation):
    with pytest.raises(SystemExit, match="cert-manager renews"):
        tool.ingress_tls(ingress({annotation: "robovast-ca"}, [tls(["robovast.example.org"], "s")]))


@pytest.mark.parametrize("entries", [None, [], [tls(["a.example.org", "b.example.org"], "s")],
                                     [tls(["a.example.org"], "s"), tls(["b.example.org"], "t")],
                                     [tls(["a.example.org"], None)]])
def test_an_ingress_without_one_host_from_one_secret_is_refused(tool, entries):
    with pytest.raises(SystemExit):
        tool.ingress_tls(ingress(tls=entries))


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_expiry_is_read_from_the_certificate(tool, tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "3", "-subj", "/CN=robovast.example.org"],
                   check=True, capture_output=True)
    left = tool.not_after(cert.read_text()) - datetime.datetime.now(datetime.timezone.utc)
    assert datetime.timedelta(days=2, hours=23) < left <= datetime.timedelta(days=3)


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_what_is_no_certificate_is_refused(tool):
    with pytest.raises(SystemExit, match="could not read"):
        tool.not_after("not a certificate")


def test_staging_keeps_a_certificate_of_its_own(tool, tmp_path):
    """certbot refuses to replace a trusted certificate with a staging one, so a staging run names its own."""
    real = tool.certbot_command("robovast.example.org", "ops@example.org", tmp_path, False, "1000:1000")
    staged = tool.certbot_command("robovast.example.org", "ops@example.org", tmp_path, True, "1000:1000")
    assert real[real.index("--cert-name") + 1] == "robovast.example.org" and "--staging" not in real
    assert staged[staged.index("--cert-name") + 1] == "robovast.example.org-staging" and "--staging" in staged
    for cmd in (real, staged):
        assert cmd[cmd.index("-d") + 1] == "robovast.example.org"
        assert cmd[cmd.index("--user") + 1] == "1000:1000" and f"{tmp_path}:/le" in cmd


@pytest.mark.parametrize("extra", [["--staging"], ["--email", "ops@example.org"], ["--dir", "/tmp/le"]])
def test_check_refuses_what_only_a_renewal_uses(extra):
    """`--check` reports and returns, so an option it would not use is refused rather than ignored."""
    result = subprocess.run([sys.executable, str(SCRIPT), "--check", *extra], capture_output=True, text=True,
                            check=False, env={**os.environ, "KUBECONFIG": "/nonexistent"})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--check only reports" in result.stderr
