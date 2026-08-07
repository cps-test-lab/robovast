"""Undo an MCP client's HTML escaping of free text.

Some clients HTML-escape prompt text on the way to the model, so a description dictated
as ``SIM_SUITE_->ROBOSITO_`` reaches a tool -- and is then stored -- as
``SIM_SUITE_-&gt;ROBOSITO_``, which shows up verbatim in ``list_campaigns``, the web UI
and every export. Nothing downstream can repair that: by then the entity is simply part
of the string.

This undoes it at the boundary where the escaped text arrives. It is deliberately *not*
:func:`html.unescape`, which implements HTML5's error recovery and mangles ordinary
prose:

- No-semicolon references are recognised, so a URL's query string is destroyed --
  ``?a=1&param=2`` becomes ``?a=1\xb6m=2``.
- A named reference matches by longest prefix, so ``&notit;`` becomes ``\xactit;``.

Both forms are far more likely to be someone's literal text than an escape, so a
reference is honoured only when it is well-formed: a known HTML5 name, or a numeric
codepoint, terminated by a semicolon. Anything else is left exactly as it arrived.

Decoding is single-level -- one pass, and the replacement text is never rescanned -- so
``&amp;gt;`` yields the literal ``&gt;`` rather than ``>``. A description that is
*about* an entity survives being written down.

Applies to prose only. Never run it over file content or configuration text: a ``.vast``
or an ``.osc`` legitimately contains ``&`` and ``<``, and decoding them would corrupt
the file.
"""

import re
from html.entities import html5

#: A well-formed character reference: named, decimal, or hexadecimal, semicolon required.
#: The name is matched whole -- no longest-prefix fallback -- so an unknown name is left
#: alone rather than partially decoded.
_REFERENCE = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def _decode(match: re.Match) -> str:
    body = match.group(1)
    if not body.startswith("#"):
        # html5 keys carry the semicolon; the semicolon-less legacy keys are the ones we
        # are refusing, so ask for the terminated form only.
        return html5.get(body + ";", match.group(0))
    try:
        code = int(body[2:], 16) if body[1] in "xX" else int(body[1:])
    except ValueError:
        return match.group(0)
    # A surrogate or an out-of-range codepoint is not text a description meant; keeping
    # the reference literal is better than raising or substituting U+FFFD.
    if not 0 < code < 0x110000 or 0xD800 <= code <= 0xDFFF:
        return match.group(0)
    return chr(code)


def unescape_client_text(text: str) -> str:
    """Decode the HTML character references in *text*, single-level. See module docstring."""
    return _REFERENCE.sub(_decode, text)
