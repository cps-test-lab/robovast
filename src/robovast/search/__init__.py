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

"""Iterative, algorithm-agnostic parameter search ("phase 2").

A campaign with a ``search:`` block runs as a closed loop instead of a single
batch: a :class:`~robovast.search.strategy.SearchStrategy` proposes parameter
sets, they are composed into configs and executed (reusing the same packing and
launchers as batch mode), an :class:`~robovast.search.evaluator.Evaluator`
scores the results, and the strategy is told the outcome so it can propose the
next generation. The goal is surfacing as many failures / near-failures as
possible; quality-diversity, Optuna, grid, … are interchangeable strategies
behind one interface.
"""
