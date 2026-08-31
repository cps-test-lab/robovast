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

"""The Postgres RoboVAST runs for itself, beside the service.

The central index holds every campaign's rows, which is what makes comparing the nine
campaigns of a search arm one query instead of materialising ~10 GB of per-campaign
databases. It runs as a **third container in the robovast-service pod**, exactly as the
registry does (:mod:`.registry_deploy`) -- same pod, so the service reaches it on
``localhost`` with no Service, no network policy and no credential in flight between nodes.

**It is a cache with a long memory, not a system of record.** Every row in it is derivable
from the campaign directories in the object store: ``campaign.db`` for the dimensions, the
artifacts for the metrics. Losing the volume costs a re-ingest, not data -- which is why
this is one replica with no HA, no backup story, and no replication. What it must not do is
lose the volume *casually*, because re-ingesting 153 campaigns is hours; hence a PVC or
hostPath and never ``emptyDir``.

**Why not a StatefulSet.** One replica, no ordinal identity, no peer discovery, and the
volume is claimed by name -- a StatefulSet's guarantees are for a set. Running it in the
service's own pod additionally means it starts and stops with the service, so there is no
window where the service is up and its index is not yet reachable through a Service
object's endpoints.

The trade that buys: the index restarts whenever the service does, because they share a
pod and every upgrade rolls it (``service_deploy.RESTART_ANNOTATION``). That is acceptable
because the service is the only client -- nothing else is mid-query when it goes -- and it
is the same trade the registry already makes.
"""

#: Postgres' own port, inside the pod only. Nothing publishes it: the service talks to
#: ``localhost`` in the same pod, and no Ingress rule routes to it. An index reachable from
#: outside the pod would be a database on the network with one shared password, which is a
#: different security posture from the one this deployment has.
INDEX_PORT = 5432

#: Container name inside the robovast-service pod.
INDEX_CONTAINER_NAME = "index"

#: Upstream Postgres. Pinned to a major tag rather than a digest by the same rule as the
#: registry: it is infrastructure a campaign never runs *in*, so no result depends on which
#: patch release stored the rows. The major IS pinned, because a major upgrade rewrites the
#: on-disk format and would strand the volume.
INDEX_IMAGE = "postgres:16-alpine"

#: The database, role and mount point. The role is not ``postgres``: a superuser connection
#: would make the read-only role in :mod:`robovast.results_processing.data_query` a
#: formality, since anything could grant itself back what it was denied.
INDEX_DB_NAME = "robovast"
INDEX_DB_USER = "robovast"

#: Postgres wants its data directory to be a subdirectory of the mount, not the mount
#: itself: a PVC's root often carries a ``lost+found``, and initdb refuses a non-empty
#: directory. Getting this wrong produces a CrashLoopBackOff whose log says only
#: "directory exists but is not empty", which reads like a bug in the volume.
INDEX_MOUNT_DIR = "/var/lib/postgresql/data"
INDEX_DATA_DIR = f"{INDEX_MOUNT_DIR}/pgdata"

#: Name of the volume carrying :data:`INDEX_MOUNT_DIR`, and of its PVC.
INDEX_VOLUME_NAME = "index-data"

#: Default host path backing the index when no StorageClass is available -- a stock RKE2
#: ships none, so a PVC there stays Pending forever.
DEFAULT_INDEX_HOST_PATH = "/var/lib/robovast-index"

#: The Secret holding the index password, and the key inside it. A Secret rather than a
#: literal in the manifest because the manifest is printed by ``vast service manifests``
#: and lands in logs and issues; the password does not belong in either.
INDEX_SECRET_NAME = "robovast-index"
INDEX_PASSWORD_KEY = "password"


def index_dsn(password: str = "", host: str = "127.0.0.1") -> str:
    """The DSN the service uses to reach the index.

    ``127.0.0.1`` rather than ``localhost`` on purpose: ``localhost`` resolves to ``::1``
    first in a dual-stack pod, and Postgres' default ``listen_addresses`` covers IPv4 only,
    so the first connection attempt fails and the retry succeeds -- an intermittent
    start-up error that looks like a race and is not.
    """
    parts = [f"host={host}", f"port={INDEX_PORT}", f"dbname={INDEX_DB_NAME}",
             f"user={INDEX_DB_USER}"]
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


def index_secret_manifest(namespace: str, password: str) -> dict:
    """The Secret carrying the index password."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": INDEX_SECRET_NAME, "namespace": namespace,
                     "labels": {"app": "robovast-service"}},
        "type": "Opaque",
        "stringData": {INDEX_PASSWORD_KEY: password},
    }


def index_container() -> dict:
    """The Postgres container to run alongside the service in its pod."""
    password_ref = {"secretKeyRef": {"name": INDEX_SECRET_NAME,
                                     "key": INDEX_PASSWORD_KEY}}
    return {
        "name": INDEX_CONTAINER_NAME,
        "image": INDEX_IMAGE,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"containerPort": INDEX_PORT, "name": "index"}],
        "env": [
            {"name": "POSTGRES_DB", "value": INDEX_DB_NAME},
            {"name": "POSTGRES_USER", "value": INDEX_DB_USER},
            {"name": "POSTGRES_PASSWORD", "valueFrom": password_ref},
            # See INDEX_DATA_DIR on why this is a subdirectory of the mount.
            {"name": "PGDATA", "value": INDEX_DATA_DIR},
        ],
        "volumeMounts": [{"name": INDEX_VOLUME_NAME,
                          "mountPath": INDEX_MOUNT_DIR}],
        # pg_isready over the unix socket, not a TCP connect: a TCP port opens before
        # recovery finishes, so a port check would report ready while queries still fail.
        # -U/-d because the probe runs as root in the container and pg_isready otherwise
        # asks for a role named after the OS user, which does not exist.
        "readinessProbe": {
            "exec": {"command": ["pg_isready", "-U", INDEX_DB_USER, "-d", INDEX_DB_NAME]},
            "initialDelaySeconds": 3, "periodSeconds": 5},
        # Deliberately slower and later than readiness. A first start runs initdb, and a
        # restart after an unclean stop replays WAL; a liveness probe impatient enough to
        # kill either turns a slow start into a crash loop that never completes.
        "livenessProbe": {
            "exec": {"command": ["pg_isready", "-U", INDEX_DB_USER, "-d", INDEX_DB_NAME]},
            "initialDelaySeconds": 30, "periodSeconds": 20, "failureThreshold": 6},
    }


def index_volume(storage_path: str = DEFAULT_INDEX_HOST_PATH,
                 storage_class: str = "") -> dict:
    """The volume backing the index: a PVC when one can be provisioned, else hostPath.

    ``emptyDir`` is not offered, for a sharper reason than the registry's. Every upgrade
    restarts this pod, and with an emptyDir that would drop the index on each version bump
    -- so every campaign would need re-ingesting from the object store to be queryable
    again, which for the existing corpus is hours of work triggered by a routine deploy.
    The data is derivable; the cost of re-deriving it is not routine.

    hostPath is the default because a stock RKE2 cluster ships no StorageClass, so a PVC
    stays Pending forever. It pins the index to one node, which is why the pod carries the
    ``robovast.io/data-node`` selector (:mod:`.node_placement`) -- the same reason the
    registry and the results volume do.
    """
    if storage_class:
        return {"name": INDEX_VOLUME_NAME,
                "persistentVolumeClaim": {"claimName": INDEX_VOLUME_NAME}}
    return {"name": INDEX_VOLUME_NAME,
            "hostPath": {"path": storage_path or DEFAULT_INDEX_HOST_PATH,
                         "type": "DirectoryOrCreate"}}


def index_pvc_manifest(namespace: str, storage_class: str, size: str = "100Gi") -> dict:
    """The PVC for :func:`index_volume`, or ``None`` when backed by hostPath.

    100Gi by default. The rows are what a campaign's ``data.db`` files used to hold --
    measured at ~4-6 MB per run, so the existing 153-campaign corpus is ~100-150 GB before
    compression -- and Postgres stores them more compactly than SQLite did while adding
    indexes. It is a starting size, not a computed one: the honest number comes from
    ingesting the corpus and looking.
    """
    if not storage_class:
        return None
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": INDEX_VOLUME_NAME, "namespace": namespace,
                     "labels": {"app": "robovast-service"}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "storageClassName": storage_class,
                 "resources": {"requests": {"storage": size}}},
    }
