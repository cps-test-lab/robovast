# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``tools/renew_ingress_tls.py``: which Ingress it renews, and how it reads a certificate's expiry."""

import datetime
import importlib.util
import shutil
import subprocess
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
