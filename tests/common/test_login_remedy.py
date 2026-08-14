# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast login`` must name the right remedy, not the most common one.

Every failure used to end in "Check the URL and the token the operator gave you." For an
untrusted TLS certificate that is worse than saying nothing: the token is fine, and the
same URL opens in a browser as soon as the warning is clicked away — so the CLI looks
like the broken half and the user re-checks the one thing that was never wrong.

Observed live against ``https://robovast.example.org`` before its certificate existed:

    could not authenticate against https://robovast.example.org: ...
    certificate verify failed: self-signed certificate (_ssl.c:1000)
    Check the URL and the token the operator gave you.
"""

import pytest
import requests

from robovast.common.cli.cli import _login_remedy

#: The real exception `requests` raises, reproduced verbatim from the live run.
TLS_FAILURE = requests.exceptions.SSLError(
    "HTTPSConnectionPool(host='robovast.example.org', port=443): Max retries exceeded with "
    "url: /version (Caused by SSLError(SSLCertVerificationError(1, '[SSL: "
    "CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate "
    "(_ssl.c:1000)')))")


def test_an_untrusted_certificate_does_not_blame_the_token():
    remedy = _login_remedy(TLS_FAILURE)
    assert "certificate" in remedy.lower()
    assert "not about the token" in remedy
    # It must also explain why the browser managed and this did not, since that
    # discrepancy is what makes the CLI look at fault.
    assert "browser" in remedy.lower()


def test_an_unreachable_address_says_the_token_was_never_used():
    exc = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='nope.invalid', port=443): Max retries exceeded "
        "(Caused by NewConnectionError('Failed to resolve nope.invalid'))")
    remedy = _login_remedy(exc)
    assert "never used" in remedy
    assert "certificate" not in remedy.lower()


def test_a_rejected_token_is_the_one_case_that_does_blame_the_token():
    remedy = _login_remedy(RuntimeError("service returned 401 Unauthorized"))
    assert "rejected the token" in remedy


def test_an_unrecognised_failure_keeps_the_general_advice():
    """The fallback must stay: a narrower guess would be wrong more often than useful."""
    remedy = _login_remedy(RuntimeError("something nobody anticipated"))
    assert "Check the URL and the token" in remedy


@pytest.mark.parametrize("exc", [TLS_FAILURE,
                                 requests.exceptions.ConnectionError("Connection refused"),
                                 RuntimeError("401"),
                                 RuntimeError("?")])
def test_every_remedy_is_a_usable_sentence(exc):
    remedy = _login_remedy(exc)
    assert remedy and remedy[0].isupper() and remedy.rstrip().endswith(".")
