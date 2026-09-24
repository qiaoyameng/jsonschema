"""
Evaluation of instances against schemas, collecting annotations.

This module implements a non-mutating ``evaluate`` interface for the
Draft 2020-12 validator. Besides validity and the usual validation
errors, it collects the annotations (``title``, ``description``,
``default``, ``readOnly`` and ``writeOnly``) produced by the
subschemas which *successfully* applied to each part of the instance.

Only annotations from branches which actually passed for the given
instance are reported: every passing ``anyOf`` branch contributes,
``oneOf`` contributes only when exactly one branch passes, ``if``
merely selects between ``then`` and ``else`` without leaking its own
annotations, and annotations inside failed branches or ``not`` never
make it into the result. ``default`` is reported as an annotation
only -- the instance is never modified.
"""
from __future__ import annotations

import re

from attrs import frozen

from jsonschema._utils import find_additional_properties
from jsonschema.exceptions import ValidationError

#: The keywords whose values are collected as annotations.
ANNOTATION_KEYWORDS = (
    "title",
    "description",
    "default",
    "readOnly",
    "writeOnly",
)


@frozen
class Annotation:
    """
    A single annotation collected while evaluating an instance.
    """

    keyword: str
    value: object
    instance_location: tuple[int | str, ...]
    schema_location: str


@frozen
class EvaluationResult:
    """
    The outcome of evaluating an instance against a schema.
    """

    valid: bool
    errors: tuple[ValidationError, ...]
    annotations: tuple[Annotation, ...]


def evaluate(validator, instance) -> EvaluationResult:
    """
    Evaluate the instance against the validator's schema.

    The instance is not modified. The returned errors are exactly the
    ones ``validator.iter_errors(instance)`` produces, and annotations
    are collected only from subschemas which successfully applied.
    """
    errors = tuple(validator.iter_errors(instance))
    annotations = tuple(
        _collect(
            validator,
            instance,
            validator.schema,
            validator._resolver,
            instance_path=(),
            schema_path=(),
        ),
    )
    return EvaluationResult(
        valid=not errors,
        errors=errors,
        annotations=annotations,
    )


def _base_uri(resolver) -> str:
    return getattr(resolver, "_base_uri", "")


def _location(base_uri: str, schema_path: tuple[int | str, ...]) -> str:
    pointer = "".join(
        "/" + str(segment).replace("~", "~0").replace("/", "~1")
        for segment in schema_path
    )
    return f"{base_uri}#{pointer}"


def _subresource_resolver(validator, resolver, subschema):
    if subschema is True or subschema is False:
        return resolver
    return resolver.in_subresource(
        validator._specification.create_resource(subschema),
    )


def _applies(validator, subschema, instance, resolver) -> bool:
    evolved = validator.evolve(schema=subschema, _resolver=resolver)
    return evolved.is_valid(instance)


def _collect(
    validator,
    instance,
    schema,
    resolver,
    instance_path,
    schema_path,
):
    """
    Yield annotations from ``schema`` wherever it applies to ``instance``.

    Callers are responsible for only calling this with (sub)schemas
    which successfully validated the corresponding (sub)instance.
    """
    if schema is True or schema is False:
        return

    base_uri = _base_uri(resolver)

    for keyword in ANNOTATION_KEYWORDS:
        if keyword in schema:
            yield Annotation(
                keyword=keyword,
                value=schema[keyword],
                instance_location=instance_path,
                schema_location=_location(
                    base_uri, (*schema_path, keyword),
                ),
            )

    def descend(instance, subschema, instance_path, schema_path):
        subresolver = _subresource_resolver(validator, resolver, subschema)
        if _applies(validator, subschema, instance, subresolver):
            yield from _collect(
                validator,
                instance,
                subschema,
                subresolver,
                instance_path=instance_path,
                schema_path=schema_path,
            )

    # References contribute the annotations of whatever they resolve to.
    # The resolved resolver carries the URI of the resource the reference
    # (dynamically) resolved to, so locations point at the real resource.
    for keyword in ("$ref", "$dynamicRef"):
        ref = schema.get(keyword)
        if ref is None:
            continue
        resolved = resolver.lookup(ref)
        if _applies(validator, resolved.contents, instance, resolved.resolver):
            yield from _collect(
                validator,
                instance,
                resolved.contents,
                resolved.resolver,
                instance_path=instance_path,
                schema_path=(),
            )

    for index, subschema in enumerate(schema.get("allOf", [])):
        yield from descend(
            instance, subschema, instance_path, (*schema_path, "allOf", index),
        )

    for index, subschema in enumerate(schema.get("anyOf", [])):
        yield from descend(
            instance, subschema, instance_path, (*schema_path, "anyOf", index),
        )

    oneOf = schema.get("oneOf")
    if oneOf is not None:
        passing = [
            (index, subschema)
            for index, subschema in enumerate(oneOf)
            if _applies(
                validator, subschema, instance,
                _subresource_resolver(validator, resolver, subschema),
            )
        ]
        if len(passing) == 1:
            index, subschema = passing[0]
            yield from descend(
                instance, subschema, instance_path,
                (*schema_path, "oneOf", index),
            )

    # "if" only selects between "then" and "else"; its own annotations
    # (and "not"'s, which is never descended into) do not contribute.
    if "if" in schema:
        if_schema = schema["if"]
        if _applies(
            validator, if_schema, instance,
            _subresource_resolver(validator, resolver, if_schema),
        ):
            branch, keyword = schema.get("then"), "then"
        else:
            branch, keyword = schema.get("else"), "else"
        if branch is not None:
            yield from descend(
                instance, branch, instance_path, (*schema_path, keyword),
            )

    if validator.TYPE_CHECKER.is_type(instance, "object"):
        for property, subschema in schema.get("properties", {}).items():
            if property in instance:
                yield from descend(
                    instance[property], subschema,
                    (*instance_path, property),
                    (*schema_path, "properties", property),
                )

        for pattern, subschema in schema.get("patternProperties", {}).items():
            for property, value in instance.items():
                if re.search(pattern, property):
                    yield from descend(
                        value, subschema,
                        (*instance_path, property),
                        (*schema_path, "patternProperties", pattern),
                    )

        additional = schema.get("additionalProperties")
        if additional is not None:
            for property in find_additional_properties(instance, schema):
                yield from descend(
                    instance[property], additional,
                    (*instance_path, property),
                    (*schema_path, "additionalProperties"),
                )

        for property, subschema in schema.get("dependentSchemas", {}).items():
            if property in instance:
                yield from descend(
                    instance, subschema, instance_path,
                    (*schema_path, "dependentSchemas", property),
                )

    if validator.TYPE_CHECKER.is_type(instance, "array"):
        prefixItems = schema.get("prefixItems", [])
        for index, subschema in enumerate(prefixItems[:len(instance)]):
            yield from descend(
                instance[index], subschema,
                (*instance_path, index),
                (*schema_path, "prefixItems", index),
            )

        items = schema.get("items")
        if items is not None:
            for index in range(len(prefixItems), len(instance)):
                yield from descend(
                    instance[index], items,
                    (*instance_path, index),
                    (*schema_path, "items"),
                )

        contains = schema.get("contains")
        if contains is not None:
            subresolver = _subresource_resolver(validator, resolver, contains)
            for index, each in enumerate(instance):
                if _applies(validator, contains, each, subresolver):
                    yield from _collect(
                        validator,
                        each,
                        contains,
                        subresolver,
                        instance_path=(*instance_path, index),
                        schema_path=(*schema_path, "contains"),
                    )
