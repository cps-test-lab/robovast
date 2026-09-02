# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every provider that embeds an object store sizes it the same.

The store is the same server doing the same work wherever it runs, so a limit raised on one
provider and not the others is not a policy difference -- it is a bump somebody applied to the
manifest they were looking at. Nothing else notices: the under-sized provider keeps working
until a batch is large enough to OOM it, and the failure surfaces in postprocessing, nowhere
near the manifest that caused it.
"""

import io

import yaml

from robovast.execution.cluster_config import azure, minikube, rke2

MANIFESTS = {"rke2": rke2.MINIO_MANIFEST_RKE2,
             "minikube": minikube.MINIO_MANIFEST_MINIKUBE,
             "azure": azure.MINIO_MANIFEST_AZURE}


def _store_resources(manifest):
    text = manifest.replace("{storage_size}", "10Gi")
    for doc in yaml.safe_load_all(io.StringIO(text)):
        if not doc or doc.get("kind") != "Pod":
            continue
        for container in doc["spec"]["containers"]:
            if container["name"] == "minio":
                return container["resources"]
    raise AssertionError("no minio container in this provider's manifest")


def test_every_provider_sizes_the_store_alike():
    sized = {name: _store_resources(m) for name, m in MANIFESTS.items()}
    assert len(set(map(repr, sized.values()))) == 1, (
        f"the providers disagree about the store's resources: {sized}")
