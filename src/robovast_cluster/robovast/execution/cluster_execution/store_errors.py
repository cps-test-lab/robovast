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

"""One translation of "the object store did not answer" for every S3 caller.

botocore reports a stalled tunnel, a store that went away, a reset mid-response and a
refused connection as four unrelated transport exceptions, none of which a caller can
interpret differently: there was no answer. Left raw they surface as a traceback
through urllib3 and botocore's retry handler that names nothing
:class:`~robovast.common.errors.ObjectStoreUnreachableError` does not.

The transient tuple and the sentence live here, not beside one caller, because a
caller that enumerates transport exceptions itself covers the ones it happened to hit:
a list naming only ``EndpointConnectionError`` still reaches the user raw on a
connection reset. Everything that talks S3 outside ``ObjectStorage`` -- the streaming
export passes, which run in a tar writer thread where a raw traceback is even harder
to read -- goes through :func:`store_errors_as`.
"""

import contextlib

__all__ = ["transport_exceptions", "unreachable_message", "store_errors_as"]


def transport_exceptions() -> tuple:
    """The botocore exceptions that mean "no answer", as an ``except`` tuple.

    Imported lazily: botocore is a heavy import, and a caller on a code path that
    never reaches S3 should not pay for it.
    """
    from botocore.exceptions import (  # pylint: disable=import-outside-toplevel
        ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError,
        ReadTimeoutError)
    return (ReadTimeoutError, ConnectTimeoutError, EndpointConnectionError,
            ConnectionClosedError)


def unreachable_message(endpoint, what: str, cause: BaseException) -> str:
    """One sentence: which endpoint, what was being attempted, the transport's reason.

    *what* is phrased to follow "while" ("streaming a campaign to the share").
    botocore's own message quotes the endpoint of the failed *request*; the endpoint
    the client is bound to is the one an operator can probe, so it is named here --
    for a rotating port-forward the two differ.
    """
    return (f"Object store at {endpoint} is unreachable while {what}: {cause}. "
            "Check that the object store (MinIO) is running; off-cluster it is reached "
            "through a kubectl port-forward, which the service's keep-alive reopens "
            "within ~10 s of a stall — so retrying shortly may be all that is needed.")


@contextlib.contextmanager
def store_errors_as(endpoint, what: str):
    """Re-raise any transport failure in the block as ``ObjectStoreUnreachableError``.

    A ``ClientError`` passes through untouched: the store *answered*, and what it
    answered (``NoSuchKey``, ``AccessDenied``) is the caller's question to interpret.
    """
    from robovast.common.errors import \
        ObjectStoreUnreachableError  # pylint: disable=import-outside-toplevel
    try:
        yield
    except transport_exceptions() as exc:
        raise ObjectStoreUnreachableError(
            unreachable_message(endpoint, what, exc)) from exc
