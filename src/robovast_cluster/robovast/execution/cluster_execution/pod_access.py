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

"""How a pod reaches the service's data plane, and what it is given to do so.

Every pod the service launches -- a scenario Job, a postprocessing Job, an aux or exec
pod, an image build -- moves its bytes through the data plane
(:mod:`robovast.service.data_app`): one ``GET`` of a tar for what it needs, one ``PUT``
of a tar for what it made. This module is the one place that knows three things every
such pod needs to agree on with the service:

* **the address**: the service's ClusterIP by its ``<service>.<namespace>.svc`` name and
  the data prefix, in ``ROBOVAST_DATA_URL``, so no pod carries a hostname that resolves
  only in one cluster;
* **the credential**: a token scoped to the campaign or the slot the pod works on
  (:func:`robovast.service.auth.scoped_token`), in ``ROBOVAST_TOKEN``. A campaign's
  token lives in a Secret the service creates before the campaign's first Job and
  deletes with its last, because a Job is created many times and a value in the pod
  spec would be readable in every one of them; a slot's token is a pod-lifetime value
  in the pod's own env, scoped to a scratch tree that is deleted with it;
* **the two shell forms** a container uses: ``curl | tar`` to land a stream on a mount,
  ``tar | curl`` to deliver one. Both stream; neither touches the pod's disk twice;
* **the retry schedule** both follow, which outlasts a service being upgraded.

A pod carries nothing else that reaches a campaign.
"""

import os
import shlex

#: Environment the pod reads its access from.
DATA_URL_ENV = "ROBOVAST_DATA_URL"
TOKEN_ENV = "ROBOVAST_TOKEN"
CAMPAIGN_ID_ENV = "ROBOVAST_CAMPAIGN_ID"

#: The key inside a campaign's token Secret.
TOKEN_KEY = "token"

#: Prefix of the per-campaign Secret name; the rest is the campaign id made label-safe.
CAMPAIGN_SECRET_PREFIX = "robovast-campaign-"


def data_url(namespace: str) -> str:
    """The data plane's in-cluster address: the service's port plus the data prefix."""
    from robovast.service.interface import Routes  # pylint: disable=import-outside-toplevel

    from .service_deploy import SERVICE_NAME, SERVICE_PORT  # pylint: disable=import-outside-toplevel
    return f"http://{SERVICE_NAME}.{namespace}.svc:{SERVICE_PORT}{Routes.DATA}"


def campaign_secret_name(campaign_id: str) -> str:
    from .cluster_execution import _label_safe_campaign  # pylint: disable=import-outside-toplevel
    return (CAMPAIGN_SECRET_PREFIX + _label_safe_campaign(campaign_id))[:63].rstrip("-.")


def campaign_secret_manifest(namespace: str, campaign_id: str, token: str) -> dict:
    """The Secret carrying one campaign's scoped token."""
    from .cluster_execution import _label_safe_campaign  # pylint: disable=import-outside-toplevel
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": campaign_secret_name(campaign_id), "namespace": namespace,
                     "labels": {"app": "robovast-campaign",
                                "campaign-id": _label_safe_campaign(campaign_id)}},
        "type": "Opaque",
        "stringData": {TOKEN_KEY: token},
    }


def ensure_campaign_secret(core, namespace: str, campaign_id: str, token: str) -> None:
    """Create the campaign's token Secret, or leave the one that exists.

    Idempotent because every batch of a search calls it, and a resumed campaign calls it
    again: the token is deterministic (an HMAC of the campaign id under the shared
    secret), so the Secret that exists holds the same value this would write.
    """
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    try:
        core.create_namespaced_secret(namespace, campaign_secret_manifest(
            namespace, campaign_id, token))
    except client.exceptions.ApiException as e:
        if e.status != 409:
            raise


def delete_campaign_secret(core, namespace: str, campaign_id: str) -> None:
    """Remove the campaign's token Secret; a Secret already gone is not an error."""
    from kubernetes import client  # pylint: disable=import-outside-toplevel
    try:
        core.delete_namespaced_secret(campaign_secret_name(campaign_id), namespace)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise


def campaign_scope(campaign_id: str) -> str:
    # Deferred like every other reach into the service layer here: this module is part of
    # the execution engine, which composes below the service and must not pull it into
    # memory at import (``tests/execution/test_layering.py``).
    from robovast.service.auth import scope_for_campaign  # pylint: disable=import-outside-toplevel
    return scope_for_campaign(campaign_id)


def staged_scope(slot: str) -> str:
    from robovast.service.auth import scope_for_staged  # pylint: disable=import-outside-toplevel
    return scope_for_staged(slot)


def campaign_pod_env(namespace: str, campaign_id: str) -> list:
    """The env a campaign's pod needs to reach the data plane: address, id, and the token
    from the campaign's Secret."""
    return [
        {"name": DATA_URL_ENV, "value": data_url(namespace)},
        {"name": CAMPAIGN_ID_ENV, "value": campaign_id},
        {"name": TOKEN_ENV, "valueFrom": {"secretKeyRef": {
            "name": campaign_secret_name(campaign_id), "key": TOKEN_KEY}}},
    ]


def staged_pod_env(namespace: str, token: str) -> list:
    """The env a pod working on a staged slot needs: address and its slot's token.

    The token is a plain value: the pod lives as long as its slot, and the slot is a
    scratch tree nothing else can be reached through.
    """
    return [
        {"name": DATA_URL_ENV, "value": data_url(namespace)},
        {"name": TOKEN_ENV, "value": token},
    ]


#: The curl every transfer uses. ``-sS`` so nothing is printed but an error, ``-f`` so
#: an HTTP failure is an exit status rather than a body that ``tar`` then fails to parse.
_CURL = f'curl -sSf -H "Authorization: Bearer ${TOKEN_ENV}"'

#: The retry schedule of every transfer between a pod and the data plane: attempt *n* is
#: followed by a sleep of ``n * TRANSFER_BACKOFF_S`` seconds, so a transfer keeps trying for
#: :func:`transfer_retry_window_s`.
#:
#: That window has to exceed the service's own startup budget,
#: ``service_deploy.STARTUP_PROBE_PERIOD_SECONDS * STARTUP_PROBE_FAILURE_THRESHOLD``: a
#: service being upgraded resumes every interrupted campaign before it answers, and may take
#: the whole of that budget to do so. A pod that gave up sooner would fail for the upgrade,
#: not for anything it did -- a Job that finished meanwhile, or one that was starting.
#:
#: Every attempt re-runs the whole pipeline, never a retry inside ``curl``: both ends of a
#: transfer are a pipe, which ``curl`` cannot rewind, so its own retry would send a second
#: stream after the part of the first it already passed on -- a delivery the service cannot
#: parse, or a fetch ``tar`` refuses. ``tests/execution/test_pod_transfer_retry.py`` holds
#: the window to the startup budget.
TRANSFER_ATTEMPTS = 13
TRANSFER_BACKOFF_S = 30


def transfer_retry_window_s(attempts: int = TRANSFER_ATTEMPTS,
                            backoff_s: int = TRANSFER_BACKOFF_S) -> int:
    """The seconds between the first attempt and the last, under linear backoff."""
    return backoff_s * attempts * (attempts - 1) // 2


#: A fetch, as a subshell so it composes with ``&&``. ``curl``'s status travels through a
#: file because the pipeline's is ``tar``'s, and the two say different things: a transfer
#: that failed is retried -- a refused connection while the service rolls, a stream cut
#: short, a 5xx -- unless the service answered 4xx, which a retry would only repeat; an
#: extraction that failed on a whole stream is the node's (its disk), and is not. ``23`` is
#: ``curl`` unable to write on because ``tar`` stopped reading, so ``tar`` has the cause.
#: Each attempt extracts over what the last one left: the same archive, so every file it
#: cut short is written again whole. The exit status is ``curl``'s when the transfer failed
#: and ``tar``'s when the extraction did.
_FETCH = '''mkdir -p @@DEST@@ && (
attempt=1
while :; do
    err=$(mktemp) && rcf=$(mktemp) || exit 1
    { @@CURL@@ "@@URL@@" 2>"$err"; echo $? >"$rcf"; } | tar -x -C @@DEST@@
    tar_rc=$?
    curl_rc=$(cat "$rcf")
    cat "$err" >&2
    status=$(sed -n 's/.*returned error: \\([0-9][0-9][0-9]\\).*/\\1/p' "$err" | head -n 1)
    rm -f "$err" "$rcf"
    case "$curl_rc" in 0|23) exit "$tar_rc" ;; esac
    case "$status" in 4[0-9][0-9]) exit "$curl_rc" ;; esac
    [ "$attempt" -lt @@ATTEMPTS@@ ] || exit "$curl_rc"
    echo "[fetch] curl exit $curl_rc${status:+, HTTP $status} (attempt $attempt/@@ATTEMPTS@@); retrying in $((attempt * @@BACKOFF_S@@))s" >&2
    sleep $((attempt * @@BACKOFF_S@@))
    attempt=$((attempt + 1))
done
)'''


def fetch_command(route: str, dest: str, query: str = "", *,
                  attempts: int = TRANSFER_ATTEMPTS, backoff_s: int = TRANSFER_BACKOFF_S) -> str:
    """Shell that lands the tar at ``$ROBOVAST_DATA_URL<route>[?query]`` under *dest*.

    *route* is the path **after** the data prefix (``/campaigns/<id>/inputs``); the
    prefix is what the env carries. *query* is appended verbatim and must already be
    URL-safe (the caller quotes it). *attempts* and *backoff_s* are the retry schedule and
    exist so a test can run it in seconds; a pod takes the defaults.
    """
    url = f"${DATA_URL_ENV}{route}" + (f"?{query}" if query else "")
    return (_FETCH
            .replace("@@DEST@@", shlex.quote(dest))
            .replace("@@CURL@@", _CURL)
            .replace("@@URL@@", url)
            .replace("@@ATTEMPTS@@", str(int(attempts)))
            .replace("@@BACKOFF_S@@", str(int(backoff_s))))


def deliver_command(src: str, route: str, exclude: "tuple[str, ...]" = (),
                    *, keep_name: bool = False) -> str:
    """Shell that streams *src* as a plain tar into ``PUT $ROBOVAST_DATA_URL<route>``.

    ``tar`` writes to the pipe and ``curl -T -`` reads it as a chunked body: the tree is
    never written a second time on the pod. Re-run for a retry, because a streamed body
    cannot be replayed; the caller decides how often.

    The plain form lands *src*'s contents at the slot's root. With *keep_name* the
    members start with *src*'s own basename, so the tree lands under that name inside
    the slot: what several trees delivered to one slot need to stay apart, when one pod
    -- and so one token, scoped to one slot -- serves them all.
    """
    excludes = "".join(f" --exclude={shlex.quote(pattern)}" for pattern in exclude)
    url = f"${DATA_URL_ENV}{route}"
    if keep_name:
        parent, name = os.path.split(os.path.normpath(src))
        tar = f'tar -C {shlex.quote(parent or "/")}{excludes} -cf - {shlex.quote(name)}'
    else:
        tar = f'tar -C {shlex.quote(src)}{excludes} -cf - .'
    return f'{tar} | {_CURL} -X PUT -T - -H "Content-Type: application/x-tar" "{url}"'
