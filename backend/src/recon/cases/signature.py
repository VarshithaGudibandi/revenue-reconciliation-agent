import hashlib
import json
from collections.abc import Iterable

from recon.domain.money import Money


def exception_signature(
    kind: str, variance: Money | None, records: Iterable[tuple[str, str, str]]
) -> str:
    """Fingerprint of an exception's kind, variance and the content of its records.

    ``records`` are ``(role, record id, content hash)``. Two exceptions with the same
    signature describe the same data, so an accepted case whose signature changes has
    seen its data change.
    """
    content = {
        "kind": kind,
        "variance": None if variance is None else [variance.minor, variance.currency],
        "records": sorted(records),
    }
    return hashlib.sha256(json.dumps(content, separators=(",", ":")).encode()).hexdigest()
