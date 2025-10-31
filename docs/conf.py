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

project = "RoboVAST"
copyright = f"{datetime.datetime.now().year}, Frederik Pasch"
author = "Frederik Pasch"

version = '0.1.0'
release = '0.1.0'

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = ['sphinx.ext.extlinks',
              'sphinxcontrib.spelling']

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
