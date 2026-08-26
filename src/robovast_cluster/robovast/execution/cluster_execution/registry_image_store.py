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

"""The cluster lane's image store: experiment images in a container registry.

The counterpart of :class:`~robovast.service.image_store.LocalDockerImageStore`, and the
half of the lane that had no class of its own — its responsibilities lived as methods of
``ClusterService``, which is why a caller could reach for the *local* store on this lane and
be answered wrongly rather than not at all.

Registry knowledge stops here: the concrete ``<prefix>/<tag>:<hash>`` ref, the credentials
that read it and the CA that verifies it are this object's business, and only
:attr:`~robovast.service.image_store.ImageRef.identity` leaves it.
"""

import logging

from robovast.common.errors import ImageStoreUnavailable
from robovast.common.execution import resolve_build_base_image
from robovast.service.image_build import build_hash
from robovast.service.image_store import ImageBuildStore, ImageRef, build_identity

from .cluster_image_build import build_id_for, concrete_image_ref
from .registry_client import PRESENT, UNKNOWN, manifest_digest, manifest_state

logger = logging.getLogger(__name__)


class RegistryImageStore(ImageBuildStore):
    """Resolves and probes a cluster deployment's experiment images.

    Constructed from the two things reaching this registry needs — the cluster config and a
    Kubernetes client — rather than from the service, so it owns its credentials instead of
    borrowing them. Both arrive as callables because building either is expensive
    (off-cluster the client opens a port-forward) and a store may be constructed and never
    asked anything.
    """

    def __init__(self, namespace: str, cluster_config, k8s):
        self._namespace = namespace
        self._cluster_config = cluster_config
        self._k8s = k8s
        self._ca_paths: dict = {}

    # -- the registry this deployment pushes to -----------------------------

    def registry(self, *, require: bool = True):
        """This deployment's registry config, with its Secret/ConfigMap names filled in.

        Resolved per call rather than cached: the names come from *looking for* the objects,
        and a deployment that gains its registry Secret after the service started should not
        have to restart to be believed.
        """
        cfg = self._cluster_config()
        registry = self._resolve_registry_objects(cfg.get_registry_config())
        if require and not registry.enabled():
            raise ImageStoreUnavailable(
                f"cannot resolve a built image: {registry.why_disabled()}")
        return registry

    # -- ImageBuildStore ---------------------------------------------------

    def ref_for(self, spec, project_dir) -> ImageRef:
        registry = self.registry()
        # The registry may supply the base every experiment image builds on, so it is part
        # of what the hash covers -- which is also why this lane's hash is not the local
        # lane's for the same spec, and why re-deriving it anywhere else would drift.
        base_ref = (spec.base_image or registry.base_experiment_image
                    or resolve_build_base_image())
        # The DIGEST that ref points at, not the ref: `build_hash` asks for the base's identity
        # for the same reason the local lane resolves it to an image ID. A tag names different
        # bytes before and after the base is republished, so hashing the tag makes every rebuild
        # of the base invisible here -- and on this lane that is the lane campaigns run on: a
        # freshly published simulator or a refreshed apt snapshot would be silently ignored and
        # the store would keep serving an experiment image built on the base of some earlier day.
        # Falls back to the ref when the registry cannot answer, which is what this computed
        # before, so an unreachable registry behaves as it did rather than forcing a rebuild.
        base_identity = self.published_digest(base_ref) or base_ref
        # The resolution belongs in the key, exactly as on the local lane: without it a spec
        # naming a branch is cache-stable, so the first build's commit is served forever and
        # the record cannot say which one it was.
        image_hash = build_hash(spec, project_dir, base_identity,
                                resolved_vcs=self.resolve_vcs(spec))
        return ImageRef(
            ref=concrete_image_ref(registry.registry_prefix, spec.tag, image_hash),
            identity=build_identity(spec.tag, image_hash),
            build_id=build_id_for(spec.tag, image_hash),
            image_hash=image_hash)

    def present(self, ref: ImageRef) -> bool:
        registry = self.registry()
        state = manifest_state(
            ref.ref, dockerconfigjson=self.push_dockerconfig(registry.push_secret_name),
            insecure=registry.insecure,
            ca_path=self.ca_path(registry.ca_configmap_name))
        if state == UNKNOWN:
            # The registry did not answer. Saying "not built" here is the mistake this
            # store exists to prevent: it sends the caller to rebuild an image that may
            # well be sitting in the registry, and blames the artifact for a problem with
            # reaching it. The warning naming the cause is already in the log.
            raise ImageStoreUnavailable(
                f"cannot tell whether {ref.identity} is built: this deployment's image "
                f"registry did not answer. That is an infrastructure problem — check the "
                f"registry and its credentials (vast exec cluster setup) rather than "
                f"rebuilding.")
        return state == PRESENT

    def published_digest(self, image_ref: str) -> str:
        """What *image_ref*'s tag points at in the registry now, or ``""`` when unknown.

        Here rather than at the caller because this is where the credential triple already
        lives -- the push Secret, the insecure flag and the private CA -- and a second
        place resolving them would be a second place to get them wrong.

        ``""`` on every uncertainty, per :func:`manifest_digest`, and the caller must keep
        that distinct from "nothing newer": one is a registry that did not answer, the
        other is a service that is current. Unlike :meth:`present`, an unknown here is not
        raised: this answers "is there an upgrade?", and not knowing is a fine answer to
        show a reader, whereas not knowing whether an image is built would send them to
        rebuild one that exists.
        """
        registry = self.registry(require=False)
        return manifest_digest(
            image_ref,
            dockerconfigjson=self.push_dockerconfig(registry.push_secret_name),
            insecure=registry.insecure,
            ca_path=self.ca_path(registry.ca_configmap_name))

    # -- credentials -------------------------------------------------------

    def push_dockerconfig(self, push_secret_name: str) -> str:
        """The push Secret's ``.dockerconfigjson``, or ``""`` when unavailable.

        Same credential the build Job mounts; read here only to authenticate a read-only
        manifest probe. Never returned to a client.
        """
        if not push_secret_name:
            return ""
        from kubernetes import client
        try:
            secret = self._k8s().read_namespaced_secret(push_secret_name, self._namespace)
        except client.exceptions.ApiException as e:
            logger.warning("registry check: cannot read push secret %s: %s",
                           push_secret_name, e)
            return ""
        data = (secret.data or {}).get(".dockerconfigjson")
        if not data:
            return ""
        import base64
        try:
            return base64.b64decode(data).decode()
        except (ValueError, UnicodeDecodeError):
            logger.warning("registry check: push secret %s is not decodable",
                           push_secret_name)
            return ""

    def ca_path(self, ca_configmap_name: str) -> str:
        """Materialize the registry CA to a file for ``requests``' ``verify=``.

        Cached per ConfigMap name: this runs on every build submit, and a fresh temp file
        each time would leak one per call for the service's lifetime.
        """
        if not ca_configmap_name:
            return ""
        if ca_configmap_name in self._ca_paths:
            return self._ca_paths[ca_configmap_name]
        from kubernetes import client
        try:
            cm = self._k8s().read_namespaced_config_map(ca_configmap_name, self._namespace)
            pem = (cm.data or {}).get("ca.pem", "")
        except client.exceptions.ApiException as e:
            logger.warning("registry check: cannot read CA configmap %s: %s",
                           ca_configmap_name, e)
            pem = ""
        path = ""
        if pem:
            import tempfile
            fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 - lives for the process
                mode="w", suffix=".pem", prefix="robovast-registry-ca-", delete=False)
            fd.write(pem)
            fd.close()
            path = fd.name
        self._ca_paths[ca_configmap_name] = path
        return path

    def pull_secret_name(self) -> str:
        """The Secret a pod needs to pull an image from here, or ``""``.

        A pod running one of our built images needs this; without it the pull fails only on
        a node that has not already cached the image, which is the worst place to find out.

        **Nothing is caught here on purpose.** The predecessor of this function wrapped its
        body in a bare ``except`` and returned ``""``, which swallowed an ImportError and made
        it a no-op for its whole life — a credential-less pod, reported as "no credential
        needed". An absent Secret is the one thing that legitimately means that, and
        :meth:`_resolve_registry_objects` already answers it by looking; a cluster we cannot
        reach is a different thing and must say so.
        """
        return self.registry(require=False).pull_secret_name or ""

    # -- registry object discovery -----------------------------------------

    def git_secret_name(self) -> str:
        """The git-token Secret, when this deployment has one; ``""`` otherwise.

        Looked for rather than assumed, for the reason :meth:`_resolve_registry_objects` gives
        about the registry objects: referencing a Secret that does not exist keeps the build
        pod from starting, so "is it there?" is the only safe question. A deployment set up
        without a token then builds without one, which is right -- only a private spec needs
        it, and that case has already failed earlier, at resolution.
        """
        from kubernetes import client  # pylint: disable=import-outside-toplevel

        from .service_deploy import GIT_SECRET_NAME  # pylint: disable=import-outside-toplevel

        try:
            self._k8s().read_namespaced_secret(GIT_SECRET_NAME, self._namespace)
            return GIT_SECRET_NAME
        except client.exceptions.ApiException as e:
            if e.status not in (403, 404):
                raise
            if e.status == 403:
                # Not permitted to look: say so rather than reading it as "no token
                # configured", which would turn a permissions gap into a build that fails
                # much later, in a clone, naming credentials nobody removed.
                logger.warning(
                    "not permitted to read %r in %s; building without a git token",
                    GIT_SECRET_NAME, self._namespace)
            return ""

    def _resolve_registry_objects(self, registry):
        """Fill in the push/pull Secret and CA ConfigMap by *looking for them*.

        Their names are fixed constants written by ``vast exec cluster setup``, so the
        ``ROBOVAST_REGISTRY_{PUSH,PULL}_SECRET`` / ``_CA_CONFIGMAP`` variables were never
        carrying a name — only the fact that setup had created the object, since
        referencing a Secret that does not exist keeps the pod from starting. Setup writes
        them into the *deployed service pod's* env, so an **off-cluster** ``vast serve``
        never learned them and silently pushed anonymously to an untrusted registry.

        Checking existence covers both deployments identically. An explicitly set variable
        still wins, for a deployment that named its objects differently.
        """
        from kubernetes import client

        from .service_deploy import REGISTRY_CA_CONFIGMAP_NAME, REGISTRY_PUSH_SECRET_NAME

        def exists(read, name):
            try:
                read(name, self._namespace)
                return True
            except client.exceptions.ApiException as e:
                if e.status not in (403, 404):
                    raise
                if e.status == 403:
                    # In-pod without RBAC for this read: say so rather than treating it as
                    # absent, which would look like "no credentials configured".
                    logger.warning(
                        "not permitted to read %r in %s; cannot tell whether the registry "
                        "object exists", name, self._namespace)
                return False

        core = self._k8s()
        if not registry.push_secret_name and exists(
                core.read_namespaced_secret, REGISTRY_PUSH_SECRET_NAME):
            registry.push_secret_name = REGISTRY_PUSH_SECRET_NAME
            logger.info("using registry push Secret %r", REGISTRY_PUSH_SECRET_NAME)
        if not registry.pull_secret_name and registry.push_secret_name:
            # One dockerconfigjson serves both directions (setup wires it that way).
            registry.pull_secret_name = registry.push_secret_name
        if not registry.ca_configmap_name and exists(
                core.read_namespaced_config_map, REGISTRY_CA_CONFIGMAP_NAME):
            registry.ca_configmap_name = REGISTRY_CA_CONFIGMAP_NAME
            logger.info("using registry CA ConfigMap %r", REGISTRY_CA_CONFIGMAP_NAME)
        return registry


__all__ = ["RegistryImageStore"]
