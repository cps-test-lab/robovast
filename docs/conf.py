# Copyright (C) 2025 Frederik Pasch
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

# pylint: disable=all

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import datetime
# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information
import os
import sys
import types

# ---------------------------------------------------------------------------
# Stub out PySide6 and the Qt matplotlib backend for headless CI.
#
# Several variation modules (obstacle_variation, path_variation, …) pull in
# PySide6 transitively through the GUI layer.  On GitHub Actions libEGL is
# absent so the real PySide6 import fails.  We inject lightweight stub modules
# with real classes (not MagicMock) so that multiple inheritance still works.
# ``setdefault`` ensures stubs are only used when the real package is missing.
# ---------------------------------------------------------------------------
def _create_pyside6_stubs():
    """Create minimal PySide6 stub modules if the real package is unavailable."""
    try:
        from PySide6 import QtCore  # noqa: F401 – probe that Qt libs actually load
        return  # real PySide6 is usable – nothing to do
    except (ImportError, OSError):
        pass

    # -- PySide6 top-level --------------------------------------------------
    _pyside6 = types.ModuleType('PySide6')
    _pyside6.__version__ = '0.0.0'

    # -- PySide6.QtCore -----------------------------------------------------
    _qtcore = types.ModuleType('PySide6.QtCore')
    class _QObject: pass
    class _Signal:
        def __init__(self, *a, **kw): pass
    _qtcore.QObject = _QObject
    _qtcore.Signal = _Signal
    _qtcore.QPointF = type('QPointF', (), {})

    # -- PySide6.QtGui ------------------------------------------------------
    _qtgui = types.ModuleType('PySide6.QtGui')
    for _n in ('QBrush', 'QColor', 'QPainter', 'QPen', 'QPolygonF'):
        setattr(_qtgui, _n, type(_n, (), {}))

    # -- PySide6.QtWidgets --------------------------------------------------
    _qtwidgets = types.ModuleType('PySide6.QtWidgets')
    _qtwidgets.QWidget = type('QWidget', (), {})
    _qtwidgets.QVBoxLayout = type('QVBoxLayout', (), {})

    for name, mod in [
        ('PySide6', _pyside6),
        ('PySide6.QtCore', _qtcore),
        ('PySide6.QtGui', _qtgui),
        ('PySide6.QtWidgets', _qtwidgets),
    ]:
        sys.modules[name] = mod

    # -- matplotlib Qt backend ----------------------------------------------
    _mpl_qt = types.ModuleType('matplotlib.backends.backend_qt5agg')
    _mpl_qt.FigureCanvasQTAgg = type('FigureCanvasQTAgg', (), {})
    _mpl_qt.NavigationToolbar2QT = type('NavigationToolbar2QT', (), {})
    sys.modules['matplotlib.backends.backend_qt5agg'] = _mpl_qt


_create_pyside6_stubs()

project = "RoboVAST"
copyright = f"{datetime.datetime.now().year}, Frederik Pasch"
author = "Frederik Pasch"

version = '0.1.0'
release = '0.1.0'

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = ['sphinx.ext.extlinks',
              'sphinx.ext.autodoc',
              'sphinx.ext.napoleon',
              'sphinx_click',
              'sphinxcontrib.spelling',
              'mcp_tools',
              'variation_plugins']

# Add the project root to the path so we can import the modules.
# Paths are relative to this conf.py file so they work regardless of CWD
# (e.g. local `make doc` vs GitHub Actions).
_docs_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_docs_dir, '_ext'))
sys.path.insert(0, os.path.join(_docs_dir, '..', 'src'))
sys.path.insert(0, os.path.join(_docs_dir, '..', 'src', 'robovast_nav'))

# sphinx-click configuration
# Enable proper formatting of Click docstrings
sphinx_click_format_docstrings = True

extlinks = {'repo_link': ('https://github.com/cps-test-lab/robovast/blob/main/%s', '%s')}

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

language = 'en'

linkcheck_ignore = [
    r'https://github.com/cps-test-lab/robovast/.*',
]

spelling_word_list_filename = 'dictionary.txt'
spelling_ignore_contributor_names = False

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'sphinx_rtd_theme'
html_static_path = ['.']

html_css_files = [
    'custom.css',
]

# https://docs.github.com/en/actions/learn-github-actions/contexts#github-context
github_user, github_repo = os.environ["GITHUB_REPOSITORY"].split("/", maxsplit=1)
html_context = {
    'display_github': True,
    'github_user': github_user,
    'github_repo': github_repo,
    'github_version': os.environ["GITHUB_REF_NAME"] + '/docs/',
}
