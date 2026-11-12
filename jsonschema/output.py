"""
Structured validation output.

This module offers the ``basic`` and ``detailed`` output formats defined by
the JSON Schema 2020-12 output specification
(``https://json-schema.org/draft/2020-12/output/schema``).

These formats describe *where* validation failed using JSON Pointer
locations, instead of only providing a human-readable error string.  They
are therefore suitable for handing to another system which needs to
locate the offending fields.

Only ``basic`` and ``detailed`` are provided.  Instance and schema values
are never embedded in the output, and no information is reported for
branches which validated successfully.
"""

from __future__ import annotations

from jsonschema._output import (
    OUTPUT_SPECIFICATION,
    SUPPORTED_OUTPUT_FORMATS,
    basic,
    detailed,
    output,
)

__all__ = [
    "OUTPUT_SPECIFICATION",
    "SUPPORTED_OUTPUT_FORMATS",
    "basic",
    "detailed",
    "output",
]
