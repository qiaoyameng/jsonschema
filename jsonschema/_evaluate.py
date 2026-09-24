"""
Evaluation of instances against schemas, collecting annotations.

This module backs ``Validator.evaluate``, which pairs the usual
validation result with the annotations (e.g. :kw:`title`,
:kw:`description`, :kw:`default`, :kw:`readOnly`, :kw:`writeOnly`)
produced by the branches of the schema which the instance actually
validated successfully against.

Only annotations from successful applications are collected:

* every passing ``anyOf`` branch contributes its annotations,
* a ``oneOf`` contributes annotations only when exactly one branch
  passes,
* ``if`` merely selects between ``then`` and ``else`` -- its own
  annotations never leak into the result,
* failed branches and the contents of ``not`` never contribute.

Evaluation never mutates the instance, the schema, or the validator,
and in particular :kw:`default` is reported purely as an annotation --
no defaults are ever filled into the instance.
"""
from __future__ import annotations

from urllib.parse import urldefrag
import re

from attrs import define, field
import referencing.exceptions

from jsonschema import exceptions

#: Keywords whose values are collected as annotations.
ANNOTATION_KEYWORDS = frozenset({
    "title",
    "description",
    "default",
    "readOnly",
    "writeOnly",
})


@define(frozen=True)
class Annotation:
    """
    A single annotation collected while evaluating an instance.

    Attributes:

        keyword (str):

            the annotation keyword (e.g. ``"title"``)

        value:

            the value of the annotation keyword in the schema

        instance_location (tuple):

            the path to the annotated value within the instance,
            as a tuple of object keys and array indices

        schema_location (str):

            an absolute URI (with a JSON pointer fragment) identifying
            the annotation keyword within the schema resource it was
            found in, after resolving any intervening references
    """

    keyword: str
    value: object
    instance_location: tuple
    schema_location: str


@define(frozen=True)
class EvaluationResult:
    """
    The result of evaluating an instance against a schema.

    Attributes:

        valid (bool):

            whether the instance is valid under the schema

        errors (tuple):

            the `jsonschema.ValidationError`\\ s the instance produced,
            exactly as `jsonschema.protocols.Validator.iter_errors`
            reports them

        annotations (tuple):

            the `Annotation`\\ s collected from the branches of the
            schema which the instance successfully validated against.
            Empty whenever the instance is invalid.
    """

    valid: bool
    errors: tuple = field(factory=tuple)
    annotations: tuple = field(factory=tuple)


def evaluate(validator, instance):
    """
    Evaluate the instance under the validator's schema.

    Returns an `EvaluationResult`. Neither the instance, the schema,
    nor the validator are modified, and no state is shared between
    calls.
    """
    errors = tuple(validator.iter_errors(instance))
    if errors:
        return EvaluationResult(valid=False, errors=errors)

    annotations = _walk(
        validator,
        instance,
        validator.schema,
        resolver=validator._resolver,
        instance_path=(),
        schema_location=validator._resolver._base_uri,
    )
    return EvaluationResult(
        valid=True,
        errors=(),
        annotations=tuple(annotations if annotations is not None else ()),
    )


def _escape(segment):
    return str(segment).replace("~", "~0").replace("/", "~1")


def _join_location(location, *segments):
    pointer = "".join("/" + _escape(each) for each in segments)
    uri, fragment = urldefrag(location)
    return f"{uri}#{fragment}{pointer}" if fragment else f"{uri}#{pointer}"


def _reference_location(ref, resolved):
    """
    The location of the schema a reference resolved to.

    Uses the base URI of the resolver of the *resolved* resource, so
    that locations reflect the resource actually jumped to (including
    for ``$dynamicRef``), not the referring document.
    """
    fragment = ref[1:] if ref.startswith("#") else urldefrag(ref).fragment
    uri = resolved.resolver._base_uri
    return f"{uri}#{fragment}" if fragment else uri


def _is_valid(validator, schema, resolver, instance):
    if schema is True:
        return True
    if schema is False:
        return False
    return validator.evolve(schema=schema, _resolver=resolver).is_valid(
        instance,
    )


def _walk(validator, instance, schema, resolver, instance_path,
          schema_location):
    """
    Collect annotations from applying ``schema`` to ``instance``.

    Returns a list of `Annotation`\\ s if the instance is valid under
    the schema, or `None` if it is not.
    """
    if schema is True:
        return []
    if schema is False or not _is_valid(
        validator, schema, resolver, instance,
    ):
        return None

    annotations = []

    def descend(instance, subschema, segments, resolver=resolver,
                path=instance_path, location=schema_location):
        # Descents into applicators of an already-valid schema cannot
        # fail; a None here would indicate a bug, so propagate it.
        found = _walk(
            validator, instance, subschema, resolver,
            path, _join_location(location, *segments),
        )
        if found is None:
            message = "unreachable: invalid subschema"
            raise RuntimeError(message)
        annotations.extend(found)

    for keyword, value in schema.items():
        if keyword in ANNOTATION_KEYWORDS:
            annotations.append(Annotation(
                keyword=keyword,
                value=value,
                instance_location=instance_path,
                schema_location=_join_location(schema_location, keyword),
            ))
        elif keyword == "properties" and validator.is_type(
            instance, "object",
        ):
            for property, subschema in value.items():
                if property in instance:
                    descend(
                        instance[property], subschema,
                        segments=("properties", property),
                        path=instance_path + (property,),
                    )
        elif keyword == "patternProperties" and validator.is_type(
            instance, "object",
        ):
            for pattern, subschema in value.items():
                for property in instance:
                    if re.search(pattern, property):
                        descend(
                            instance[property], subschema,
                            segments=("patternProperties", pattern),
                            path=instance_path + (property,),
                        )
        elif keyword == "additionalProperties" and validator.is_type(
            instance, "object",
        ):
            properties = schema.get("properties", {})
            patterns = schema.get("patternProperties", {})
            extras = (
                property for property in instance
                if property not in properties
                and not any(re.search(pattern, property)
                            for pattern in patterns)
            )
            for property in extras:
                descend(
                    instance[property], value,
                    segments=("additionalProperties",),
                    path=instance_path + (property,),
                )
        elif keyword == "prefixItems" and validator.is_type(
            instance, "array",
        ):
            for index, subschema in enumerate(value):
                if index < len(instance):
                    descend(
                        instance[index], subschema,
                        segments=("prefixItems", index),
                        path=instance_path + (index,),
                    )
        elif keyword == "items" and validator.is_type(instance, "array"):
            start = len(schema.get("prefixItems", []))
            for index in range(start, len(instance)):
                descend(
                    instance[index], value,
                    segments=("items",),
                    path=instance_path + (index,),
                )
        elif keyword == "dependentSchemas" and validator.is_type(
            instance, "object",
        ):
            for property, subschema in value.items():
                if property in instance:
                    descend(
                        instance, subschema,
                        segments=("dependentSchemas", property),
                    )
        elif keyword == "allOf":
            for index, subschema in enumerate(value):
                descend(instance, subschema, segments=("allOf", index))
        elif keyword == "anyOf":
            matched = False
            for index, subschema in enumerate(value):
                found = _walk(
                    validator, instance, subschema, resolver,
                    instance_path,
                    _join_location(schema_location, "anyOf", index),
                )
                if found is not None:
                    annotations.extend(found)
                    matched = True
            if not matched:
                return None
        elif keyword == "oneOf":
            passing = []
            for index, subschema in enumerate(value):
                found = _walk(
                    validator, instance, subschema, resolver,
                    instance_path,
                    _join_location(schema_location, "oneOf", index),
                )
                if found is not None:
                    passing.append(found)
            if len(passing) != 1:
                return None
            annotations.extend(passing[0])
        elif keyword == "not":
            # The instance is valid, so it is invalid under the ``not``
            # subschema, whose annotations therefore never apply.
            continue
        elif keyword == "if":
            if _is_valid(validator, value, resolver, instance):
                branch, segment = schema.get("then"), "then"
            else:
                branch, segment = schema.get("else"), "else"
            if branch is not None:
                descend(instance, branch, segments=(segment,))
        elif keyword in ("$ref", "$dynamicRef"):
            try:
                resolved = resolver.lookup(value)
            except referencing.exceptions.Unresolvable as err:
                raise exceptions._WrappedReferencingError(err) from err
            descend(
                instance, resolved.contents,
                segments=(),
                resolver=resolved.resolver,
                location=_reference_location(value, resolved),
            )

    return annotations
