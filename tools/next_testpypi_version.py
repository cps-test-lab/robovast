#!/usr/bin/env python3
"""Pick the version a TestPyPI rehearsal upload should carry.

TestPyPI refuses to re-upload a filename it already holds, so iterating on the
packaging claim (`make publish-client-test` -> `make publish-client-test-venv`)
would otherwise cost a version bump per attempt -- and the version that reaches
real PyPI must be the one the tree was released at, not one inflated by
rehearsals. So the rehearsal carries a POST-release of the tree's version:
2.1.0, then 2.1.0.post1, .post2, ... one past the highest TestPyPI already
holds.

A post-release rather than `.devN` because pip skips pre-releases unless asked:
`pip install robovast-client` in publish-client-test-venv would resolve a .devN
rehearsal to the stale final release and pass on a wheel nobody just built.
"""

import json
import sys
import urllib.error
import urllib.request

INDEX = "https://test.pypi.org/pypi/{name}/json"


def released_versions(name: str) -> set[str]:
    try:
        with urllib.request.urlopen(INDEX.format(name=name), timeout=30) as response:
            return set(json.load(response)["releases"])
    except urllib.error.HTTPError as error:
        if error.code == 404:  # never uploaded under this name
            return set()
        raise


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <distribution> <base-version>", file=sys.stderr)
        return 2
    name, base = sys.argv[1], sys.argv[2]

    taken = released_versions(name)
    if base not in taken:
        print(base)
        return 0

    post = 1
    while f"{base}.post{post}" in taken:
        post += 1
    print(f"{base}.post{post}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
