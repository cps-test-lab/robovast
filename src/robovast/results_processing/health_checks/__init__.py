# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Health checks shipped with RoboVAST, one module per stack family.

Discovered through the ``robovast.health_checks`` entry point like any third-party check --
never imported by name from :mod:`robovast.results_processing.run_health`. That is the whole
point of the group: the substrate learns ``level`` and nothing else, so a stack's idea of
healthy lives here and a MoveIt 2 campaign is never graded by nav2's.
"""
