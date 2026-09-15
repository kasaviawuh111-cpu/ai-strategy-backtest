"""Align a model-extracted name to original text, without resolving a stock.

Only input-method horizontal whitespace is ignored. Characters, punctuation,
clause boundaries and multiple occurrences remain significant. Callers still
verify the resulting name with the server-owned security resolver.
"""

import re


def instrument_name_text(value: str) -> str:
    return re.sub(r"[^\S\r\n]", "", value)


def instrument_source_matches(text: str, name: str) -> tuple[re.Match[str], ...]:
    compact = instrument_name_text(name)
    if not compact:
        return ()
    pattern = r"[^\S\r\n]*".join(re.escape(char) for char in compact)
    return tuple(re.finditer(pattern, text))
