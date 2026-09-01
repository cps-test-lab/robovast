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

"""The Postgres RoboVAST runs for itself, in the object-store pod.

The central index holds every campaign's rows, which is what makes comparing the nine
campaigns of a search arm one query instead of materialising ~10 GB of per-campaign
databases. It runs as a container in the **``robovast`` pod** -- the one
``vast cluster setup`` creates for the object store -- and is reached through that pod's
existing ClusterIP Service.

**Why not the service pod.** ``robovast-service`` is a Deployment, and every
``vast service upgrade`` rolls it; a Postgres living there is restarted by each upgrade,
including the ones that only bump the controller image. The store pod is created once at
cluster setup, is node-pinned, and already holds the campaign data these rows index -- so
the index sits beside what it indexes and goes away only with
``vast cluster cleanup``. Losing it there is an accepted, deliberate cost: every row is
re-derivable from the campaign directories in the object store.

**It is a cache with a long memory, not a system of record.** ``campaign.db`` holds the
dimensions and the artifacts hold the metrics, so losing the index costs a re-ingest, not
data -- which is why this is one replica with no HA, no backup, no replication and no PVC.
What it must not do is lose the volume on a *routine* action, because re-ingesting the
corpus is hours; ``vast cluster cleanup`` is not routine, an upgrade is.

The price of the move is that the service no longer reaches it on ``127.0.0.1``: it is a
different pod, so the connection goes over the pod network to
:func:`index_host`. Nothing else changes -- one namespace, one Secret, one client.
"""

#: Postgres' own port. Published on the store pod's ClusterIP Service and nowhere else:
#: no Ingress rule routes to it, so it is reachable from inside the cluster network and not
#: from outside it. Exposing it further would be a database on the internet with one shared
#: password, which is a different security posture from the one this deployment has.
INDEX_PORT = 5432

#: Container name inside the store pod.
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

#: Name of the volume carrying :data:`INDEX_MOUNT_DIR`.
INDEX_VOLUME_NAME = "index-data"

#: Default host path backing the index. See :func:`index_volume` for why it is a hostPath
#: and not a claim.
DEFAULT_INDEX_HOST_PATH = "/var/lib/robovast-index"

#: What the scheduler reserves for Postgres, and what it may grow to.
#:
#: **The request is a floor, not an estimate.** It is subtracted from the capacity campaign
#: jobs can be admitted against (this cluster admits by quota), so a comfortable number
#: here costs parallel runs. An idle ``postgres:16-alpine`` sits at a few tens of MiB and
#: near-zero CPU; 256Mi/50m is a chosen floor above that, not a measurement of the loaded
#: server.
#:
#: **The memory limit is deliberately generous, and must not be "tidied" down.** Ingest is
#: a bulk ``COPY`` of millions of rows per campaign (a 240-run campaign is ~7.6M rows), and
#: an OOMKill in the middle of it fails postprocessing at the end of a campaign whose
#: compute has already been spent -- the most expensive moment there is to lose. This
#: container runs stock Postgres configuration: ``shared_buffers`` 128MB and
#: ``maintenance_work_mem`` 64MB, plus ``work_mem`` per sort per connection, so a limit
#: anywhere near a few hundred MiB is a configuration that looks fine idle and OOMs under
#: exactly the load it exists for. 2Gi is ~16x ``shared_buffers`` with room for index
#: builds; it is a starting point, and what would refine it is watching the container's RSS
#: through a full-corpus ingest.
#:
#: **No CPU limit.** CPU is compressible: a low limit does not kill the ingest, it throttles
#: it, turning a failure into a slow postprocessing nobody attributes to a cgroup. The
#: request is what reserves; there is nothing to protect neighbours from that the request
#: does not already handle.
INDEX_RESOURCES = {
    "requests": {"cpu": "50m", "memory": "256Mi"},
    "limits": {"memory": "2Gi"},
}

#: The Secret holding the index password, and the key inside it. A Secret rather than a
#: literal in the manifest because the manifest is printed by ``vast service manifests``
#: and lands in logs and issues; the password does not belong in either.
INDEX_SECRET_NAME = "robovast-index"
INDEX_PASSWORD_KEY = "password"


def index_host(namespace: str = "default") -> str:
    """The in-cluster DNS name the index answers on: the store pod's Service.

    One spelling, in :func:`store_pod.store_host` -- the registry's Ingress backend needs
    the same name, and a second definition would drift from it.
    """
    from . import store_pod  # pylint: disable=import-outside-toplevel

    return store_pod.store_host(namespace)


def index_dsn(password: str = "", namespace: str = "default") -> str:
    """The DSN the service uses to reach the index.

    A DNS name now, where this was ``127.0.0.1`` while the index shared the service's pod.
    The IPv4-literal trick that avoided is gone with it: ``.svc`` resolves to the Service's
    ClusterIP, and kube-proxy forwards that to the container's IPv4 port whatever Postgres'
    ``listen_addresses`` covers.
    """
    parts = [f"host={index_host(namespace)}", f"port={INDEX_PORT}",
             f"dbname={INDEX_DB_NAME}", f"user={INDEX_DB_USER}"]
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
    """The Postgres container to run in the object-store pod."""
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
        "resources": INDEX_RESOURCES,
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


def index_volume(storage_path: str = "") -> dict:
    """The volume backing the index: an ``emptyDir``, living exactly as long as the pod.

    **The index must not outlive the campaign results it is derived from.** That is the
    whole reason for this choice, and it is structural rather than enforced by cleanup
    code: the index sits in the same pod as the object store, on a volume of the same
    kind, so the two are created and destroyed together and no sequence of restarts,
    evictions or operator mistakes can separate them.

    This deliberately reverses an earlier decision, and the reasoning that overturned it
    is worth keeping. A hostPath was chosen so a pod restart would not "drop the index,
    and re-ingesting the corpus from the object store is hours". That premise was wrong:
    ``minio-storage`` is itself an ``emptyDir``, so the results are destroyed by the very
    same restart. Re-ingest was never slow -- it was impossible, because the source had
    gone at the same instant.

    Observed on a live cluster before this was fixed: 66 campaigns' directories vanished
    while the index still served one campaign's 10448 pose rows. A derived index outliving
    its sources is worse than no index, because it answers questions about campaigns that
    can no longer be reproduced, re-ingested or checked -- confidently, and with nothing to
    compare against.

    *storage_path* is accepted and ignored, so the callers that thread a node path through
    do not have to change if this reverts. It is not a lie about behaviour: an emptyDir is
    on the node's own filesystem either way, which is what that path was steering.
    """
    del storage_path  # see above: an emptyDir has no path to place
    return {"name": INDEX_VOLUME_NAME, "emptyDir": {}}


def index_host_path(workspaces_storage_path: str = "") -> str:
    """Where the index hostPath goes: beside the deployment's other node-local data.

    A deployer who moved the workspaces store to another disk moved this deployment's
    node-local state, and the index is part of it. Pinning it to
    :data:`DEFAULT_INDEX_HOST_PATH` regardless would quietly leave it on the node's root
    filesystem -- the disk they were moving off.
    """
    import pathlib  # pylint: disable=import-outside-toplevel

    if not workspaces_storage_path:
        return DEFAULT_INDEX_HOST_PATH
    return str(pathlib.PurePosixPath(workspaces_storage_path).parent / "robovast-index")
