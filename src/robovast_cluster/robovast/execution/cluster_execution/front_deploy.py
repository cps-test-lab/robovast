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

"""The front of the service pod: one port, two processes behind it.

The service pod runs three containers. ``robovast-service`` is the control plane (the
API, the web UI, the campaign driver) and ``robovast-data`` the data plane (the tar
streams pods exchange with the service, :mod:`robovast.service.data_app`); both listen
on Unix sockets in a shared ``emptyDir``, and this nginx owns the pod's single port and
routes ``/data/`` to one and everything else to the other.

**Why a front rather than a second port.** A pod delivering gigabytes of run output must
not share a process with the run view or the admission loop, so the data plane is its own
process with its own limits. But a service that is reached on two ports is two things to
publish, two Ingress rules, two things a client has to discover; one port keeps the
address a client, a pod and an operator hold exactly what it was.

**Why nginx and not the control plane proxying.** A proxy in the control plane would copy
every uploaded byte through the event loop it exists to protect. nginx streams both ways
with request and response buffering off, so an upload reaches the data plane as it
arrives and a download reaches the browser as it is tarred.

The configuration is rendered into a ConfigMap by ``vast cluster setup`` and ``vast
service upgrade`` from :data:`FRONT_CONFIG`, so it is versioned with the code that
depends on it and never edited on the cluster.
"""

#: Container names inside the service pod.
FRONT_CONTAINER_NAME = "robovast-front"
DATA_CONTAINER_NAME = "robovast-data"

#: Upstream nginx. Pinned to a stable tag rather than a digest by the same rule as the
#: registry: it is infrastructure a campaign never runs *in*, so no result
#: depends on which patch release proxied it. The minor is pinned because a new one can
#: change defaults this configuration relies on.
FRONT_IMAGE = "nginx:1.28-alpine"

#: The ``emptyDir`` the two Python processes put their sockets in, and the sockets.
SOCKET_VOLUME_NAME = "robovast-sockets"
SOCKET_DIR = "/run/robovast"
SERVICE_SOCKET = f"{SOCKET_DIR}/service.sock"
DATA_SOCKET = f"{SOCKET_DIR}/data.sock"

#: The ConfigMap carrying :data:`FRONT_CONFIG`, and where nginx reads it.
FRONT_CONFIGMAP_NAME = "robovast-front"
FRONT_CONFIG_KEY = "nginx.conf"
FRONT_CONFIG_MOUNT = "/etc/nginx/nginx.conf"

#: What the scheduler reserves for the front. It moves bytes between sockets and does
#: nothing else; the request is a floor for its worker, the limit keeps a proxy that
#: buffers nothing from ever needing more.
FRONT_RESOURCES = {
    "requests": {"cpu": "50m", "memory": "64Mi"},
    "limits": {"memory": "256Mi"},
}

#: What the scheduler reserves for the data plane. Extraction is I/O bound; the memory
#: it holds is the bounded queue per upload and the tar pipe per download, so a limit
#: covers a burst of concurrent transfers without letting one runaway reach the node.
DATA_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "256Mi"},
    "limits": {"memory": "2Gi"},
}

#: The nginx configuration, whole. Rendered with :func:`front_config`.
#:
#: * ``client_max_body_size 0`` -- an upload is a campaign's output and has no size a
#:   proxy should know about.
#: * ``proxy_request_buffering off`` on ``/data/`` -- the upload reaches the data plane as
#:   it arrives instead of being spooled to the front's disk first, which for a multi-GB
#:   body would double the write and delay the response by the whole transfer.
#: * ``proxy_buffering off`` on ``/data/`` -- a download is tarred on the fly, and a
#:   buffered response would hold the stream back until nginx's buffers filled.
#: * ``proxy_read_timeout`` long on both -- a download of a large campaign, an SSE stream
#:   of a running one and an upload from a slow node all sit on one connection for
#:   minutes with nothing said in between.
#: * ``proxy_http_version 1.1`` with ``Connection ""`` -- keepalive to the upstream, and
#:   what lets a chunked upload pass through unchanged.
#: * ``X-Forwarded-*`` -- the control plane trusts these from the front only
#:   (``_proxy_trust``), which is how a session cookie learns it is behind TLS.
FRONT_CONFIG = """\
worker_processes 1;
error_log /dev/stderr warn;
pid /tmp/nginx.pid;
events {{ worker_connections 1024; }}
http {{
    access_log off;
    client_max_body_size 0;
    client_body_temp_path /tmp/client_body;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    upstream control {{ server unix:{service_socket}; keepalive 16; }}
    upstream data {{ server unix:{data_socket}; keepalive 16; }}
    server {{
        listen {port};
        location {data_prefix}/ {{
            proxy_pass http://data;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }}
        location / {{
            proxy_pass http://control;
            proxy_http_version 1.1;
            proxy_set_header Connection "";
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $http_x_forwarded_proto;
            proxy_buffering off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }}
    }}
}}
"""


def front_config(port: int) -> str:
    """The rendered nginx configuration for a front on *port*."""
    from robovast.service.interface import Routes  # pylint: disable=import-outside-toplevel
    return FRONT_CONFIG.format(port=port, service_socket=SERVICE_SOCKET,
                               data_socket=DATA_SOCKET, data_prefix=Routes.DATA)


def front_configmap_manifest(namespace: str, port: int) -> dict:
    """The ConfigMap the front container mounts its configuration from."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": FRONT_CONFIGMAP_NAME, "namespace": namespace},
        "data": {FRONT_CONFIG_KEY: front_config(port)},
    }


def front_container(port: int) -> dict:
    """The nginx container: the pod's one port, routing to the two sockets.

    Its readiness is the control plane's ``/healthz`` through itself, which is what the
    Service's endpoint should mean -- the front and what it fronts are both up. No
    liveness probe: nginx that dies exits, and the kubelet restarts it for that.
    """
    return {
        "name": FRONT_CONTAINER_NAME,
        "image": FRONT_IMAGE,
        "ports": [{"containerPort": port, "name": "http"}],
        "resources": FRONT_RESOURCES,
        "volumeMounts": [
            {"name": SOCKET_VOLUME_NAME, "mountPath": SOCKET_DIR},
            {"name": FRONT_CONFIGMAP_NAME, "mountPath": FRONT_CONFIG_MOUNT,
             "subPath": FRONT_CONFIG_KEY, "readOnly": True},
        ],
        "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": port},
            "initialDelaySeconds": 5, "periodSeconds": 10},
    }


def socket_probe(socket_path: str) -> dict:
    """An ``exec`` probe against a process that listens on a Unix socket.

    The kubelet's ``httpGet`` reaches the pod's IP, where only the front listens; a
    process behind it is probed the way a client would reach it, over its socket, by the
    ``curl`` the image carries.
    """
    return {"exec": {"command": ["curl", "-sf", "--unix-socket", socket_path,
                                 "http://robovast/healthz"]}}


def socket_volume() -> dict:
    return {"name": SOCKET_VOLUME_NAME, "emptyDir": {"medium": "Memory"}}


def front_config_volume() -> dict:
    return {"name": FRONT_CONFIGMAP_NAME, "configMap": {"name": FRONT_CONFIGMAP_NAME}}
