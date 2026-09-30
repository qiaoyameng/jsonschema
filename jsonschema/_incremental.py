"""Incremental validation for Draft 2020-12 instances."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

import referencing.exceptions
import referencing.jsonschema

from jsonschema import _utils
from jsonschema.exceptions import (
    UndefinedTypeCheck,
    UnknownType,
    ValidationError,
)
from jsonschema.validators import Draft202012Validator

_DRAFT202012 = referencing.jsonschema.DRAFT202012
_VALIDATION_KEYWORDS = frozenset(Draft202012Validator.VALIDATORS)
_STANDARD_KEYWORDS = _VALIDATION_KEYWORDS | frozenset({
    "$defs",
    "$id",
    "$schema",
    "$vocabulary",
    "$anchor",
    "$dynamicAnchor",
    "$comment",
    "default",
    "deprecated",
    "description",
    "examples",
    "readOnly",
    "writeOnly",
    "title",
    "contentEncoding",
    "contentMediaType",
    "contentSchema",
    "then",
    "else",
    "minContains",
    "maxContains",
})

_SCHEMA_CONTAINERS = (
    "items",
    "contains",
    "additionalProperties",
    "propertyNames",
    "if",
    "then",
    "else",
    "not",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SCHEMA_OBJECT_CONTAINERS = (
    "$defs",
    "properties",
    "patternProperties",
    "dependentSchemas",
)
_SCHEMA_ARRAY_CONTAINERS = ("allOf", "anyOf", "oneOf")
_PARENT_SCOPE_KEYWORDS = frozenset({
    "additionalProperties",
    "unevaluatedProperties",
})


class JSONPatchError(ValueError):
    """A JSON Patch document is malformed or cannot be applied."""


class _Identity:
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value

    def __hash__(self):
        return id(self.value)

    def __eq__(self, other):
        return isinstance(other, _Identity) and other.value is self.value


def _instance_key(instance: Any):
    if isinstance(instance, bool):
        return ("boolean", instance)
    if instance is None:
        return ("null", None)
    if isinstance(instance, (int, float)):
        return ("number", instance)
    if isinstance(instance, str):
        return ("string", instance)
    return _Identity(instance)


def _resolver_key(resolver: Any):
    return (
        resolver._base_uri,
        tuple(resolver._previous),
        _Identity(resolver._registry),
    )


class _IncrementalValidator:
    def __init__(
        self,
        engine: _IncrementalEngine,
        schema: Any,
        resolver: Any,
        parent_instance: Any,
        descend_mode: str = "same",
    ):
        self._engine = engine
        self.schema = schema
        self._resolver = resolver
        self._parent_instance = parent_instance
        self._descend_mode = descend_mode
        source = engine.validator
        self._ref_resolver = source._ref_resolver
        self.format_checker = source.format_checker
        self.TYPE_CHECKER = source.TYPE_CHECKER
        self.VALIDATORS = source.VALIDATORS

    def evolve(self, **changes):
        return self._engine.wrap(
            changes.get("schema", self.schema),
            changes.get("_resolver", self._resolver),
            self._parent_instance,
            self._descend_mode,
        )

    def iter_errors(self, instance: Any):
        parent_instance = (
            instance if self._descend_mode == "child"
            else self._parent_instance
        )
        yield from self._engine.validate(
            instance,
            self.schema,
            self._resolver,
            parent_instance=parent_instance,
        )

    def descend(
        self,
        instance: Any,
        schema: Any,
        path: str | int | None = None,
        schema_path: str | int | None = None,
        resolver: Any = None,
    ):
        if resolver is None:
            resource = _DRAFT202012.create_resource(schema)
            resolver = self._resolver.in_subresource(resource)

        parent_instance = (
            instance if self._descend_mode == "child"
            else self._parent_instance
        )
        yield from self._engine.validate(
            instance,
            schema,
            resolver,
            path=path,
            schema_path=schema_path,
            parent_instance=parent_instance,
        )

    def validate(self, *args, **kwargs):
        for error in self.iter_errors(*args, **kwargs):
            raise error

    def is_type(self, instance: Any, type: str):
        try:
            return self.TYPE_CHECKER.is_type(instance, type)
        except UndefinedTypeCheck:
            raise UnknownType(type, instance, self.schema) from None

    def _validate_reference(self, ref: str, instance: Any):
        resolved = self._resolver.lookup(ref)
        return self.descend(
            instance,
            resolved.contents,
            resolver=resolved.resolver,
        )

    def is_valid(self, instance: Any):
        return next(self.iter_errors(instance), None) is None


class _IncrementalEngine:
    def __init__(self, validator: Any):
        if validator.__class__ is not Draft202012Validator:
            raise TypeError("Incremental validation supports Draft 2020-12.")

        self.validator = validator
        self._cache: dict[
            tuple[Any, ...],
            list[ValidationError],
        ] = {}
        standard = Draft202012Validator.VALIDATORS
        self._fallback = any(
            validator.VALIDATORS.get(keyword) is not standard[keyword]
            for keyword in standard
        )
        if not self._fallback:
            self._fallback = self._uses_unsupported_keywords(
                validator.schema,
                validator._resolver,
                set(),
            )

    def _uses_unsupported_keywords(self, schema, resolver, seen):
        if schema is True or schema is False:
            return False
        if not isinstance(schema, Mapping):
            return True

        identity = _Identity(schema)
        if identity in seen:
            return False
        seen.add(identity)

        if set(schema) - _STANDARD_KEYWORDS:
            return True

        for ref in (schema.get("$ref"), schema.get("$dynamicRef")):
            if ref is not None:
                try:
                    resolved = resolver.lookup(ref)
                except referencing.exceptions.Unresolvable:
                    continue
                if self._uses_unsupported_keywords(
                    resolved.contents,
                    resolved.resolver,
                    seen,
                ):
                    return True

        for keyword in _SCHEMA_CONTAINERS:
            if keyword in schema:
                resource = _DRAFT202012.create_resource(schema[keyword])
                child_resolver = resolver.in_subresource(resource)
                if self._uses_unsupported_keywords(
                    schema[keyword],
                    child_resolver,
                    seen,
                ):
                    return True

        for keyword in _SCHEMA_OBJECT_CONTAINERS:
            if keyword in schema:
                for subschema in schema[keyword].values():
                    resource = _DRAFT202012.create_resource(subschema)
                    child_resolver = resolver.in_subresource(resource)
                    if self._uses_unsupported_keywords(
                        subschema,
                        child_resolver,
                        seen,
                    ):
                        return True

        for keyword in (*_SCHEMA_ARRAY_CONTAINERS, "prefixItems"):
            if keyword in schema:
                for subschema in schema[keyword]:
                    resource = _DRAFT202012.create_resource(subschema)
                    child_resolver = resolver.in_subresource(resource)
                    if self._uses_unsupported_keywords(
                        subschema,
                        child_resolver,
                        seen,
                    ):
                        return True

        return False

    def wrap(
        self,
        schema: Any,
        resolver: Any,
        parent_instance: Any,
        descend_mode: str = "same",
    ):
        return _IncrementalValidator(
            self,
            schema,
            resolver,
            parent_instance,
            descend_mode,
        )

    def root(self, instance: Any):
        yield from self.validate(
            instance,
            self.validator.schema,
            self.validator._resolver,
            parent_instance=instance,
        )

    def _real_validator(self, schema: Any, resolver: Any):
        return self.validator.evolve(schema=schema, _resolver=resolver)

    def validate(
        self,
        instance: Any,
        schema: Any,
        resolver: Any,
        path: str | int | None = None,
        schema_path: str | int | None = None,
        parent_instance: Any = None,
    ):
        if parent_instance is None:
            parent_instance = instance
        if self._fallback:
            errors = self._real_validator(schema, resolver).iter_errors(
                instance,
            )
            for error in errors:
                _prefix_error(error, path, schema_path)
                yield error
            return

        if schema is True:
            return
        if schema is False:
            error = ValidationError(
                f"False schema does not allow {instance!r}",
                validator=None,
                validator_value=None,
                instance=instance,
                schema=schema,
            )
            _prefix_error(error, path, schema_path)
            yield error
            return

        key = (
            _instance_key(instance),
            _instance_key(parent_instance),
            _Identity(schema),
            _resolver_key(resolver),
        )
        cached = self._cache.get(key)
        if cached is not None:
            for error in deepcopy(cached):
                _prefix_error(error, path, schema_path)
                yield error
            return

        errors = []
        for keyword, value in schema.items():
            descend_mode = (
                "same"
                if keyword in _PARENT_SCOPE_KEYWORDS
                else "child"
            )
            wrapper = self.wrap(
                schema,
                resolver,
                parent_instance,
                descend_mode,
            )
            keyword_validator = wrapper.VALIDATORS.get(keyword)
            if keyword_validator is None:
                continue

            keyword_errors = keyword_validator(
                wrapper,
                value,
                instance,
                schema,
            ) or ()
            for error in keyword_errors:
                error._set(
                    validator=keyword,
                    validator_value=value,
                    instance=instance,
                    schema=schema,
                    type_checker=wrapper.TYPE_CHECKER,
                )
                if keyword not in {"if", "$ref"}:
                    error.schema_path.appendleft(keyword)
                errors.append(error)

        self._cache[key] = deepcopy(errors)
        for error in errors:
            _prefix_error(error, path, schema_path)
            yield error

    def prune(self, root: Any):
        reachable = set()
        stack = [root]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                identity = id(current)
                if identity in reachable:
                    continue
                reachable.add(identity)
                stack.extend(current.values())
            elif isinstance(current, list):
                identity = id(current)
                if identity in reachable:
                    continue
                reachable.add(identity)
                stack.extend(current)

        retained = {}
        for key, errors in self._cache.items():
            instance, parent, schema, resolver = key
            if isinstance(instance, _Identity) and (
                id(instance.value) not in reachable
            ):
                continue
            if isinstance(parent, _Identity) and (
                id(parent.value) not in reachable
            ):
                continue
            retained[key] = errors
        self._cache = retained


def _prefix_error(error: ValidationError, path: Any, schema_path: Any):
    if path is not None:
        error.path.appendleft(path)
    if schema_path is not None:
        error.schema_path.appendleft(schema_path)


def _unescape_token(token: str):
    if "~" in token:
        result = []
        index = 0
        while index < len(token):
            char = token[index]
            if char == "~":
                index += 1
                if index >= len(token) or token[index] not in "01":
                    raise JSONPatchError("Invalid JSON Pointer escape.")
                result.append("~" if token[index] == "0" else "/")
            else:
                result.append(char)
            index += 1
        return "".join(result)
    return token


def _parse_pointer(pointer: str, *, allow_root: bool = False):
    if not isinstance(pointer, str):
        raise JSONPatchError("JSON Pointer paths must be strings.")
    if pointer == "":
        if allow_root:
            return []
        raise JSONPatchError("The root document cannot be targeted.")
    if not pointer.startswith("/"):
        raise JSONPatchError("Invalid JSON Pointer.")
    return [_unescape_token(part) for part in pointer.split("/")[1:]]


def _array_index(token: str, length: int, *, allow_end: bool = False):
    if token == "-":
        if allow_end:
            return length
        raise JSONPatchError("The '-' index cannot be read.")
    if not token or token != token.lstrip("-"):
        raise JSONPatchError("Invalid array index.")
    if len(token) > 1 and not token.isdigit():
        raise JSONPatchError("Invalid array index.")
    if not token.isdigit():
        raise JSONPatchError("Invalid array index.")
    if len(token) > 1 and token[0] == "0":
        raise JSONPatchError("Array indexes must not have leading zeroes.")

    index = int(token)
    maximum = length if allow_end else length - 1
    if index > maximum:
        raise JSONPatchError("Array index is out of bounds.")
    return index


def _navigate(document: Any, tokens: Sequence[str]):
    current = document
    for token in tokens:
        if isinstance(current, dict):
            if token not in current:
                raise JSONPatchError("Path target does not exist.")
            current = current[token]
        elif isinstance(current, list):
            current = current[_array_index(token, len(current))]
        else:
            raise JSONPatchError("Path does not point through a container.")
    return current


def _get(document: Any, pointer: str, *, allow_root: bool = False):
    tokens = _parse_pointer(pointer, allow_root=allow_root)
    return _navigate(document, tokens)


def _add_at(document: Any, tokens: Sequence[str], value: Any):
    if not tokens:
        return deepcopy(value)

    token, *rest = tokens
    if isinstance(document, dict):
        updated = dict(document)
        if rest:
            if token not in document:
                raise JSONPatchError("Path target does not exist.")
            updated[token] = _add_at(document[token], rest, value)
        else:
            updated[token] = deepcopy(value)
        return updated

    if isinstance(document, list):
        index = _array_index(token, len(document), allow_end=not rest)
        updated = list(document)
        if rest:
            updated[index] = _add_at(document[index], rest, value)
        else:
            updated.insert(index, deepcopy(value))
        return updated

    raise JSONPatchError("Path does not point through a container.")


def _remove_at(document: Any, tokens: Sequence[str]):
    if not tokens:
        raise JSONPatchError("The root document cannot be removed.")

    token, *rest = tokens
    if isinstance(document, dict):
        if rest:
            if token not in document:
                raise JSONPatchError("Path target does not exist.")
            updated = dict(document)
            updated[token] = _remove_at(document[token], rest)
            return updated
        if token not in document:
            raise JSONPatchError("Remove target does not exist.")
        updated = dict(document)
        del updated[token]
        return updated

    if isinstance(document, list):
        index = _array_index(token, len(document))
        updated = list(document)
        if rest:
            updated[index] = _remove_at(document[index], rest)
        else:
            updated.pop(index)
        return updated

    raise JSONPatchError("Path does not point through a container.")


def _replace_at(document: Any, tokens: Sequence[str], value: Any):
    if not tokens:
        return deepcopy(value)

    token, *rest = tokens
    if isinstance(document, dict):
        if token not in document:
            raise JSONPatchError("Replace target does not exist.")
        updated = dict(document)
        if rest:
            updated[token] = _replace_at(document[token], rest, value)
        else:
            updated[token] = deepcopy(value)
        return updated

    if isinstance(document, list):
        index = _array_index(token, len(document))
        updated = list(document)
        if rest:
            updated[index] = _replace_at(document[index], rest, value)
        else:
            updated[index] = deepcopy(value)
        return updated

    raise JSONPatchError("Path does not point through a container.")


def _is_prefix(prefix: str, pointer: str):
    return pointer.startswith(f"{prefix}/")


def _move(document: Any, from_pointer: str, pointer: str):
    if from_pointer == pointer:
        return document
    if _is_prefix(from_pointer, pointer):
        raise JSONPatchError("Cannot move a value into itself.")

    value = _get(document, from_pointer, allow_root=True)
    removed = _remove_at(
        document,
        _parse_pointer(from_pointer, allow_root=True),
    )
    return _add_at(removed, _parse_pointer(pointer, allow_root=True), value)


def _copy(document: Any, from_pointer: str, pointer: str):
    if _is_prefix(from_pointer, pointer):
        raise JSONPatchError("Cannot copy a value into itself.")
    value = _get(document, from_pointer, allow_root=True)
    return _add_at(document, _parse_pointer(pointer, allow_root=True), value)


def _test(document: Any, pointer: str, value: Any):
    target = _get(document, pointer, allow_root=True)
    if not _utils.equal(target, value):
        raise JSONPatchError("Test operation failed.")
    return document


def apply_json_patch(document: Any, patch: Any):
    """Apply a JSON Patch without mutating ``document``."""
    if not isinstance(patch, list):
        raise JSONPatchError("A JSON Patch document must be an array.")

    result = document
    for operation in patch:
        if not isinstance(operation, Mapping):
            raise JSONPatchError("Each patch operation must be an object.")
        op = operation.get("op")
        path = operation.get("path")
        tokens = (
            _parse_pointer(path, allow_root=True)
            if isinstance(path, str)
            else None
        )

        if tokens is None:
            raise JSONPatchError(
                "Each patch operation requires a string path.",
            )
        if op in {"add", "replace", "test"}:
            if "value" not in operation:
                raise JSONPatchError(f"{op!r} requires a value.")
            if op == "add":
                result = _add_at(result, tokens, operation["value"])
            elif op == "replace":
                result = _replace_at(result, tokens, operation["value"])
            else:
                result = _test(result, path, operation["value"])
        elif op in {"move", "copy"}:
            from_pointer = operation.get("from")
            if not isinstance(from_pointer, str):
                raise JSONPatchError(f"{op!r} requires a 'from' path.")
            if op == "move":
                result = _move(result, from_pointer, path)
            else:
                result = _copy(result, from_pointer, path)
        elif op == "remove":
            result = _remove_at(result, tokens)
        else:
            raise JSONPatchError(f"Unsupported patch operation: {op!r}.")

    return result


class IncrementalValidationSession:
    """
    Reuse Draft 2020-12 validation results between JSON Patch updates.

    All standard assertion, applicator, format, reference, dynamic
    reference, and unevaluated-keyword behavior is supported. A schema
    which uses a non-standard keyword is validated from scratch on every
    update.
    """

    def __init__(self, validator: Any, instance: Any):
        self._engine = _IncrementalEngine(validator)
        self._document = deepcopy(instance)
        self._errors = self._validate()

    @property
    def document(self):
        return deepcopy(self._document)

    def iter_errors(self):
        for error in deepcopy(self._errors):
            yield error

    def apply(self, patch: Any):
        candidate = apply_json_patch(self._document, patch)
        errors = list(self._engine.root(candidate))
        self._document = candidate
        self._errors = errors
        self._engine.prune(self._document)
        return list(self.iter_errors())

    def _validate(self):
        return list(self._engine.root(self._document))
