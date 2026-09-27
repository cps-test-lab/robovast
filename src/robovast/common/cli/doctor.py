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

"""``vast doctor``'s core checks, registered in the ``robovast.doctor_checks`` group."""

from robovast.client.doctor import Check, DoctorOptions, tool_check


def doctor_checks(_options: DoctorOptions) -> list[Check]:
    """Docker, which the core uses on this machine and campaigns do not."""
    return [tool_check("docker", "Install Docker — needed only to compose variations that "
                                 "run a helper image on this machine ('vast configuration "
                                 "generate'), not for campaigns.",
                       optional=True)]
