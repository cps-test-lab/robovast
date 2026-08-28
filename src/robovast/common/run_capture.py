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

"""The run capture's format version, on the Python side.

RoboVAST defines this format -- ``docs/run_capture.rst`` is the specification, and roqsim is one
*producer* of it. There are two consumers, and they read different halves: the web reader
(``frontend/ui/src/lib/scene3d/runCapture.ts``) reads the *motion*, and
:mod:`robovast.service.scene_cache` reads the *world identity* (``world`` + ``overrides``) to obtain
matching geometry. This module is the second one's half of the version contract.

Its own module rather than a constant in ``scene_cache``: that module is about caching and already
owns a ``CACHE_FORMAT_VERSION`` of its own, which is the *cache's* version and not the capture's.
Two confusable constants two lines apart is how one gets bumped for the other's reason.

What each version changed
-------------------------

1
    The original.
2
    The ``overrides`` half of world identity addresses components by **path** (``robot.lidar``)
    rather than by plugin name. Under v1 ``{"components": {"robot": {"lidar": {...}}}}`` merged
    ``{"lidar": {...}}`` into the config of the component named ``robot``; under v2 it addresses the
    child component ``robot.lidar``. The document's *shape* is unchanged, which is exactly why the
    version had to move: the same bytes now resolve to a different world, so a consumer keying a
    cache on the document alone takes a stale hit and serves geometry compiled for something else.

Adding a version
----------------

Add a row here and to the table in ``docs/run_capture.rst`` (the specification), bump
:data:`FORMAT_VERSION`, and raise ``CAPTURE_VERSION`` in the web reader if the change touches
anything *it* reads. Then ask the one question that decides whether anything else has to happen: does
the new version change the **meaning** of a field some consumer keys an identity on? If it does, that
version must reach the key -- as v2's does, through :func:`~robovast.service.scene_cache.cache_key`.
If it only adds a field, nothing else is needed: an unknown track ``kind`` is skipped by design, so a
purely additive change does not need a version at all.

There is deliberately no migration machinery here. Nothing can rewrite a v1 override document into a
v2 one from the document alone -- telling a config key from a child component requires resolving
against the world, which lives in the campaign's image. A capture is always compiled against that
image, by the roqsim that wrote it, so no rewrite is needed either.
"""

#: The format a capture manifest must declare. A consumer refuses an unknown one rather than
#: misreading it.
FORMAT = "robovast.run_capture"

#: The highest version this side understands (see the module docstring for what each changed).
FORMAT_VERSION = 2


class CaptureFormatError(ValueError):
    """A capture manifest cannot be read, with a reason a viewer can show verbatim."""


def manifest_version(manifest: dict | None) -> int:
    """The version *manifest* declares.

    An absent ``version`` reads as 1: the spec requires the field, and the oldest format is the only
    thing that can be assumed about a manifest lacking it without guessing forward. Same rule as
    roqsim's own reader applies to its recordings.
    """
    try:
        return int((manifest or {}).get("version") or 1)
    except (TypeError, ValueError):
        raise CaptureFormatError(
            f"this capture declares a version that is not a number "
            f"({(manifest or {}).get('version')!r})") from None


def check_supported(manifest: dict | None) -> int:
    """Return the manifest's version, refusing one newer than this code implements.

    The refusal matters most on *this* side. The web reader would merely fail to animate; here the
    manifest's ``overrides`` are read to decide which geometry a run gets, so a version whose
    ``overrides`` mean something this code has not seen would be keyed and compiled anyway -- and the
    picture that comes back looks perfectly fine. That is the failure the version exists to prevent.
    """
    version = manifest_version(manifest)
    if version > FORMAT_VERSION:
        raise CaptureFormatError(
            f"this run's capture is format version {version}, and RoboVAST implements up to "
            f"{FORMAT_VERSION}. Its world identity cannot be read without knowing what v{version} "
            f"changed, so its geometry is not built rather than built from a guess. RoboVAST has to "
            f"learn v{version} — see the format versions table in docs/run_capture.rst.")
    return version
