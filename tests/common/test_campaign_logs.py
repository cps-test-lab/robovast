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

"""The campaign infrastructure log's layout: which phases there are, and their order."""

from robovast.common.campaign_logs import HEAD_PHASES, INFRA_PHASES, REPEATABLE_PHASES


def test_the_import_is_the_first_phase_and_the_build_the_second():
    """An import precedes everything else a campaign does, and a campaign that failed
    before it ever ran explains itself in its build."""
    assert [name for name, _ in INFRA_PHASES[:2]] == ["IMPORT", "BUILD"]


def test_every_phase_file_is_named_once():
    files = [filename for _, filename in INFRA_PHASES]
    assert len(files) == len(set(files))


def test_the_head_phases_run_once_and_the_rest_can_repeat():
    """Every phase is one or the other, and the repeatable ones are the tail of the list,
    since the head is what a campaign is and the tail is what can be asked for again."""
    head = [filename for _, filename in HEAD_PHASES]
    assert head == [filename for _, filename in INFRA_PHASES if filename not in REPEATABLE_PHASES]
    assert [filename for _, filename in INFRA_PHASES[len(HEAD_PHASES):]] == list(REPEATABLE_PHASES)
