# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What a client needs to talk to a robovast-service, and nothing else.

These modules were under :mod:`robovast.common`, which cannot host them once the client
ships as its own distribution: ``common/__init__.py`` carries the lazy re-export map, so
the package is a regular one and its contents cannot be split across two distributions.
``robovast.client`` is a package of its own for exactly that reason.

The rule for what belongs here is the install it has to survive in: ``pydantic``,
``click`` and ``requests``, with no simulator, no Kubernetes client and no array library.
Anything reaching further belongs in the core.

Deliberately kept empty of re-exports. The parent it came from cost 528 modules to touch
because its ``__init__`` imported eagerly; repeating that here would undo the point.
"""
