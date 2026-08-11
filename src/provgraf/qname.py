"""qname validation: prefix:local.path (NC-1).

Examples: acme:riverside.rent, acme:riverside.deposit@card, prov:regulation.subsidies.
The local part allows letters/digits/._-@ (where @ = an alternateOf variant on conflict).
"""
import re

_QNAME_RE = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9_][a-z0-9_.@-]*$")


class QnameError(ValueError):
    pass


def validate(qname: str) -> str:
    if not _QNAME_RE.match(qname):
        raise QnameError(
            f"Invalid qname '{qname}'. Pattern: prefix:local.path "
            f"(e.g. acme:riverside.rent, acme:riverside.deposit@card)."
        )
    return qname


def prefix(qname: str) -> str:
    return qname.split(":", 1)[0]
