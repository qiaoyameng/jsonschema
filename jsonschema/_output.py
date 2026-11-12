"""Structured validation output for JSON Schema validators.

This module adds explicit ``basic`` and ``detailed`` output formats to the
validators in `jsonschema`.  The formats implemented here follow the
JSON Schema 2020-12 output specification
(``https://json-schema.org/draft/2020-12/output/schema``).

Only the ``basic`` and ``detailed`` formats are produced.  ``flag`` is
trivially available as `jsonschema.protocols.Validator.is_valid`, and
neither ``verbose`` annotations nor the ``list``/``hierarchical`` output
shapes from later specification revisions are synthesized.

The implementation never inspects or embeds instance values: output units
contain locations and validity only.  Annotations produced by unknown or
extension keywords are deliberately omitted rather than fabricated, and no
execution information is reported for branches which validated
successfully.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import referencing
import referencing.jsonschema

from jsonschema import validators

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError
    from jsonschema.protocols import Validator

OutputUnit = dict[str, Any]

OUTPUT_SPECIFICATION = "https://json-schema.org/draft/2020-12/output/schema"
SUPPORTED_OUTPUT_FORMATS = ("basic", "detailed")


def _escape_pointer(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _pointer(tokens) -> str:
    return "".join("/" + _escape_pointer(token) for token in tokens)


def _is_absolute_uri(uri: str) -> bool:
    return bool(urlsplit(uri).scheme)


class _Boundary:
    """A schema-resource boundary that an error's evaluation crossed."""

    __slots__ = (
        "is_ref",
        "keyword",
        "uri",
        "fragment_tokens",
        "source_length",
    )

    def __init__(
        self,
        is_ref: bool,
        keyword: str,
        uri: str,
        fragment_tokens: list[Any],
    ):
        self.is_ref = is_ref
        self.keyword = keyword
        self.uri = uri
        self.fragment_tokens = fragment_tokens
        self.source_length: int | None = None


BoundaryRecord = tuple[_Boundary, list[Any]]


class _Capture:
    __slots__ = (
        "keyword_location",
        "instance_location",
        "absolute_keyword_location",
    )

    def __init__(
        self,
        keyword_location,
        instance_location,
        absolute_keyword_location,
    ):
        self.keyword_location = keyword_location
        self.instance_location = instance_location
        self.absolute_keyword_location = absolute_keyword_location


class _OutputCollector:
    """
    Runs one validation pass while recording evaluation locations.

    The collector installs thin wrappers around ``iter_errors``,
    ``descend`` and ``_validate_reference`` on a fresh validator subclass.
    The wrappers only observe what the original implementation produces;
    the semantics and ordering of errors are unchanged.
    """

    def __init__(self, validator: Validator):
        self._output_class = validators.extend(type(validator))
        self._specification = referencing.jsonschema.specification_with(
            dialect_id=(
                validator.ID_OF(validator.META_SCHEMA)
                or "urn:unknown-dialect"
            ),
            default=referencing.Specification.OPAQUE,
        )
        self._root_uri = self._base_uri(validator)
        self._depth = 0
        self._pending_ref: tuple[_Boundary] | None = None
        self._boundary_records: dict[int, list[BoundaryRecord]] = {}
        self._captures: dict[int, _Capture] = {}
        self._install()
        self._validator = self._clone(validator)

    @staticmethod
    def _base_uri(validator: Validator) -> str:
        legacy = validator._ref_resolver
        if legacy is not None:
            return legacy.base_uri
        return validator._resolver._base_uri

    def _clone(self, validator: Validator) -> Validator:
        return self._output_class(
            schema=validator.schema,
            resolver=validator._ref_resolver,
            format_checker=validator.format_checker,
            registry=validator._registry,
            _resolver=validator._resolver,
        )

    def _install(self) -> None:
        cls = self._output_class
        original_iter_errors = cls.iter_errors
        original_descend = cls.descend
        original_reference = cls._validate_reference
        collector = self

        def iter_errors(self, instance, _schema=None):
            if _schema is not None:
                yield from original_iter_errors(self, instance, _schema)
                return
            if collector._depth != 0:
                yield from original_iter_errors(self, instance)
                return
            collector._depth = 1
            try:
                for error in original_iter_errors(self, instance):
                    collector._capture(error)
                    yield error
            finally:
                collector._depth = 0

        def descend(
            self,
            instance,
            schema,
            path=None,
            schema_path=None,
            resolver=None,
        ):
            if collector._depth == 0:
                yield from original_descend(
                    self,
                    instance,
                    schema,
                    path=path,
                    schema_path=schema_path,
                    resolver=resolver,
                )
                return

            boundary = collector._begin_descend(self, schema, resolver)
            for error in original_descend(
                self,
                instance,
                schema,
                path=path,
                schema_path=schema_path,
                resolver=resolver,
            ):
                collector._record_boundary(error, boundary)
                yield error

        def _validate_reference(self, ref, instance):
            if collector._depth == 0:
                yield from original_reference(self, ref, instance)
                return

            boundary, pending = collector._begin_reference(self, ref)
            try:
                for error in original_reference(self, ref, instance):
                    collector._record_boundary(error, boundary)
                    yield error
            finally:
                if collector._pending_ref is pending:
                    collector._pending_ref = None

        cls.iter_errors = iter_errors
        cls.descend = descend
        cls._validate_reference = _validate_reference

    def _begin_reference(
        self,
        validator: Validator,
        ref: str,
    ) -> tuple[_Boundary, tuple[_Boundary]]:
        target_uri, fragment_tokens = self._resolve_reference(validator, ref)
        schema = validator.schema
        if isinstance(schema, dict) and "$recursiveRef" in schema:
            keyword = "$recursiveRef"
        elif isinstance(schema, dict) and "$dynamicRef" in schema:
            keyword = "$dynamicRef"
        else:
            keyword = "$ref"
        boundary = _Boundary(
            is_ref=True,
            keyword=keyword,
            uri=target_uri,
            fragment_tokens=fragment_tokens,
        )
        pending = (boundary,)
        self._pending_ref = pending
        return boundary, pending

    def _begin_descend(
        self,
        validator: Validator,
        schema: Any,
        resolver: Any,
    ) -> _Boundary | None:
        pending = self._pending_ref
        if pending is not None:
            self._pending_ref = None
            boundary = pending[0]
            # An embedded ``$id`` inside the referenced resource starts a
            # new resource rooted at that ``$id``, so neither the ref
            # fragment nor the outer resource identifier applies.
            if resolver is not None:
                resolved_uri = resolver._base_uri
            elif validator._ref_resolver is not None:
                resolved_uri = validator._ref_resolver.base_uri
            else:
                resolved_uri = boundary.uri
            if resolved_uri and resolved_uri != boundary.uri:
                boundary.uri = resolved_uri
                boundary.fragment_tokens = []
            return None  # the boundary was created by _begin_reference

        if isinstance(schema, bool):
            return None
        resolved = validator._resolver.in_subresource(
            self._specification.create_resource(schema),
        )
        if resolved is validator._resolver and resolver is None:
            return None

        uri = (
            resolver._base_uri
            if resolver is not None
            else resolved._base_uri
        )
        return _Boundary(
            is_ref=False,
            keyword="",
            uri=uri,
            fragment_tokens=[],
        )

    def _record_boundary(
        self,
        error: ValidationError,
        boundary: _Boundary | None,
    ) -> None:
        if boundary is None:
            return
        key = id(error)
        records = self._boundary_records.setdefault(key, [])
        if all(record[0] is not boundary for record in records):
            records.append((boundary, list(error.relative_schema_path)))

    def _resolve_reference(
        self,
        validator: Validator,
        ref: str,
    ) -> tuple[str, list[Any]]:
        legacy = validator._ref_resolver
        base = (
            legacy.base_uri
            if legacy is not None
            else validator._resolver._base_uri
        )
        uri, fragment = urldefrag(urljoin(base, ref))
        if not fragment.startswith("/"):
            return uri, []
        tokens = []
        for token in unquote(fragment[1:]).split("/"):
            tokens.append(token.replace("~1", "/").replace("~0", "~"))
        return uri, tokens

    def _capture(
        self,
        error: ValidationError,
        inherited: list[BoundaryRecord] | None = None,
    ) -> None:
        key = id(error)
        if key in self._captures:
            return

        own = self._boundary_records.get(key, [])
        records: list[BoundaryRecord] = list(own)
        if inherited:
            for record in inherited:
                if all(existing[0] is not record[0] for existing in records):
                    records.append(record)

        absolute_tokens = list(error.absolute_schema_path)
        ref_records = [
            (boundary, relative)
            for boundary, relative in records
            if boundary.is_ref
        ]
        for boundary, relative in ref_records:
            if boundary.source_length is None:
                boundary.source_length = max(
                    0,
                    len(absolute_tokens) - len(relative),
                )
        keyword_tokens = list(absolute_tokens)
        inserted_count = 0
        for boundary, _relative in sorted(
            ref_records,
            key=lambda record: record[0].source_length,
        ):
            if boundary.keyword != "$ref":
                # ``iter_errors`` already puts other reference keywords into
                # the schema path; only plain ``$ref`` is omitted.
                continue
            inserted_count += 1
            keyword_tokens.insert(
                boundary.source_length + inserted_count - 1,
                boundary.keyword,
            )

        absolute_location: str | None = None
        if records:
            innermost, relative = records[0]
            if _is_absolute_uri(innermost.uri):
                if innermost.is_ref:
                    local = innermost.fragment_tokens + absolute_tokens[
                        innermost.source_length:
                    ]
                else:
                    local = list(relative[1:])
                absolute_location = innermost.uri + "#" + _pointer(local)
        elif _is_absolute_uri(self._root_uri):
            absolute_location = self._root_uri + "#" + _pointer(
                error.absolute_schema_path,
            )

        self._captures[key] = _Capture(
            keyword_location=_pointer(keyword_tokens),
            instance_location=_pointer(error.absolute_path),
            absolute_keyword_location=absolute_location,
        )
        for child in error.context:
            self._capture(child, records)

    def collect(
        self,
        instance: Any,
    ) -> tuple[list[ValidationError], dict[int, _Capture]]:
        self._boundary_records = {}
        self._captures = {}
        errors = list(self._validator.iter_errors(instance))
        return errors, self._captures


def _unit(capture: _Capture) -> dict[str, Any]:
    unit: dict[str, Any] = {
        "valid": False,
        "keywordLocation": capture.keyword_location,
        "instanceLocation": capture.instance_location,
    }
    if capture.absolute_keyword_location is not None:
        unit["absoluteKeywordLocation"] = capture.absolute_keyword_location
    return unit


def _detailed_unit(
    error: ValidationError,
    captures: dict[int, _Capture],
) -> OutputUnit:
    unit = _unit(captures[id(error)])
    children = [
        _detailed_unit(child, captures)
        for child in error.context
        if id(child) in captures
    ]
    if children:
        unit["errors"] = children
    return unit


def _flatten(
    error: ValidationError,
    captures: dict[int, _Capture],
    out: list[OutputUnit],
) -> None:
    if id(error) not in captures:
        return
    out.append(_unit(captures[id(error)]))
    for child in error.context:
        _flatten(child, captures, out)


def detailed(validator: Validator, instance: Any) -> dict[str, Any]:
    """
    Return the ``detailed`` output produced by validating ``instance``.
    """
    errors, captures = _OutputCollector(validator).collect(instance)
    if not errors:
        return {"valid": True}
    return {
        "valid": False,
        "keywordLocation": "",
        "instanceLocation": "",
        "errors": [_detailed_unit(error, captures) for error in errors],
    }


def basic(validator: Validator, instance: Any) -> dict[str, Any]:
    """
    Return the ``basic`` output produced by validating ``instance``.
    """
    errors, captures = _OutputCollector(validator).collect(instance)
    if not errors:
        return {"valid": True}
    units: list[dict[str, Any]] = []
    for error in errors:
        _flatten(error, captures, units)
    return {
        "valid": False,
        "keywordLocation": "",
        "instanceLocation": "",
        "errors": units,
    }


def output(
    validator: Validator,
    instance: Any,
    format: str = "basic",
) -> dict[str, Any]:
    """
    Return structured validation output in the requested ``format``.
    """
    if format == "basic":
        return basic(validator, instance)
    if format == "detailed":
        return detailed(validator, instance)
    raise ValueError(
        f"Unsupported output format {format!r}; "
        f"supported formats are {SUPPORTED_OUTPUT_FORMATS}",
    )
