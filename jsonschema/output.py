"""
JSON Schema validation output.

This module implements the ``flag``, ``basic`` and ``detailed`` output
formats from the JSON Schema 2020-12 output specification.  The same output
shape is used for instances validated by any of the validator classes
provided by this package, whose outputs are all reported with JSON Pointers
and (when a base URI is available) absolute keyword URIs.

``verbose`` is deliberately not implemented: producing it correctly requires
reporting the result of keywords which succeeded, including annotations from
unknown vocabularies, which this implementation does not collect.

The machine-readable fields intentionally contain locations, not the business
values being validated.  Pass ``include_error=True`` to include the
human-readable error message, which may contain such values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jsonschema import _utils

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from jsonschema.exceptions import ValidationError


OUTPUT_SPECIFICATION_VERSION = "2020-12"
"""The JSON Schema output specification version whose format is emitted."""

OUTPUT_SPECIFICATION_URI = (
    "https://json-schema.org/draft/2020-12/output/schema"
)
"""The URI of the output meta-schema which this module follows."""

OUTPUT_SPEC_VERSION = OUTPUT_SPECIFICATION_VERSION
OUTPUT_SPEC_URI = OUTPUT_SPECIFICATION_URI
OUTPUT_SPECIFICATION = OUTPUT_SPECIFICATION_VERSION

SUPPORTED_OUTPUT_SPECIFICATION_VERSIONS = ("2019-09", "2020-12")
"""Output specification versions whose formats are compatible with this API."""

SUPPORTED_OUTPUT_SPEC_VERSIONS = SUPPORTED_OUTPUT_SPECIFICATION_VERSIONS
SUPPORTED_OUTPUT_SPECIFICATION_RANGE = (
    SUPPORTED_OUTPUT_SPECIFICATION_VERSIONS
)

SUPPORTED_OUTPUT_FORMATS = ("flag", "basic", "detailed")
"""The output formats which can be requested."""

UNSUPPORTED_OUTPUT_FORMATS = ("verbose",)
"""Output formats which this implementation deliberately does not emit."""

_UNSET = _utils.Unset()


def _errors(target, instance):
    if instance is _UNSET:
        return list(target)
    return list(target.iter_errors(instance))


def _root(valid):
    return {
        "valid": valid,
        "keywordLocation": "",
        "instanceLocation": "",
    }


def _default_message(error):
    if error.validator is None:
        return "schema validation failed"
    return f"validation failed for {error.validator!r}"


def _output_unit(error, *, include_error):
    unit = {
        "valid": False,
        "keywordLocation": error.keyword_location,
        "instanceLocation": error.instance_location,
    }
    absolute = error.absolute_keyword_location
    if absolute is not None:
        unit["absoluteKeywordLocation"] = absolute
    if include_error:
        unit["error"] = error.message
    else:
        # The output meta-schema requires an "error" or "errors" member for
        # invalid units.  Keep the default free of any instance values.
        unit["error"] = _default_message(error)
    return unit


def _walk(errors: Iterable[ValidationError]) -> Iterator[ValidationError]:
    for error in errors:
        yield error
        yield from _walk(error.context)


def _basic_errors(errors, *, include_error):
    return [
        _output_unit(error, include_error=include_error)
        for error in _walk(errors)
    ]


def _detailed_errors(errors, *, include_error):
    units = []
    for error in errors:
        unit = _output_unit(error, include_error=include_error)
        if error.context:
            unit["errors"] = _detailed_errors(
                error.context,
                include_error=include_error,
            )
        units.append(unit)
    return units


def flag(target, instance=_UNSET):
    """
    Produce flag output for ``instance``.

    ``target`` may be a validator or an iterable of validation errors.
    """
    if instance is _UNSET:
        errors = list(target)
    else:
        errors = list(target.iter_errors(instance))
    return {"valid": not errors}


def basic(
    target,
    instance=_UNSET,
    *,
    include_error=False,
    **kwargs: Any,
):
    """
    Produce basic output for ``instance``.

    The root unit contains a flat list of every output unit produced while
    validating.  Child errors continue to have keyword locations prefixed by
    their applicator (for example ``/anyOf/0/type``).

    Arguments:

        target:

            a validator, or an iterable of `ValidationError`\\s

        instance:

            the instance to validate, if ``target`` is a validator

        include_error:

            whether to include the human-readable error message.  It is
            omitted by default since messages can embed instance values.
    """
    if "include_error_message" in kwargs:
        include_error = kwargs.pop("include_error_message")
    errors = _errors(target, instance)
    result = _root(valid=not errors)
    if errors:
        result["errors"] = _basic_errors(errors, include_error=include_error)
    return result


def detailed(
    target,
    instance=_UNSET,
    *,
    include_error=False,
    **kwargs: Any,
):
    """
    Produce detailed output for ``instance``.

    Errors caused by applicators retain their parent/child relationship.
    """
    if "include_error_message" in kwargs:
        include_error = kwargs.pop("include_error_message")
    errors = _errors(target, instance)
    result = _root(valid=not errors)
    if errors:
        result["errors"] = _detailed_errors(
            errors,
            include_error=include_error,
        )
    return result


def output(
    target,
    instance=_UNSET,
    *,
    format="basic",
    output_format=None,
    **kwargs: Any,
):
    """
    Produce output in an explicitly requested format.

    Supported formats are ``flag``, ``basic`` and ``detailed``.  Passing
    ``verbose`` raises `ValueError`, because its successful-branch execution
    information is not collected by this implementation.
    """
    if output_format is not None:
        format = output_format
    if format == "flag":
        return flag(target, instance)
    if format == "basic":
        return basic(target, instance, **kwargs)
    if format == "detailed":
        return detailed(target, instance, **kwargs)
    raise ValueError(
        f"unsupported output format {format!r}; "
        f"supported formats are {', '.join(SUPPORTED_OUTPUT_FORMATS)}",
    )


def iter_output(
    target,
    instance=_UNSET,
    *,
    format="basic",
    **kwargs: Any,
):
    """
    Iterate over the output units of a validation.
    """
    result = output(target, instance, format=format, **kwargs)
    if format == "flag":
        yield result
    else:
        yield from result.get("errors", ())


basic_output = basic
detailed_output = detailed
flag_output = flag
to_basic_output = basic
to_detailed_output = detailed
as_basic_output = basic
as_detailed_output = detailed


__all__ = [
    "OUTPUT_SPECIFICATION_URI",
    "OUTPUT_SPECIFICATION_VERSION",
    "OUTPUT_SPECIFICATION",
    "OUTPUT_SPEC_URI",
    "OUTPUT_SPEC_VERSION",
    "SUPPORTED_OUTPUT_FORMATS",
    "SUPPORTED_OUTPUT_SPECIFICATION_RANGE",
    "SUPPORTED_OUTPUT_SPEC_VERSIONS",
    "SUPPORTED_OUTPUT_SPECIFICATION_VERSIONS",
    "UNSUPPORTED_OUTPUT_FORMATS",
    "as_basic_output",
    "as_detailed_output",
    "basic",
    "basic_output",
    "detailed",
    "detailed_output",
    "flag",
    "flag_output",
    "iter_output",
    "output",
    "to_basic_output",
    "to_detailed_output",
]
