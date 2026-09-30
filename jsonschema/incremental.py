"""
Incremental validation sessions for Draft 2020-12 documents.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from jsonschema import _utils, exceptions
from jsonschema._format import FormatChecker
from jsonschema.validators import Draft202012Validator


class JsonPatchError(ValueError):
    """A JSON Patch document is malformed or cannot be applied."""


class _MemoizedFormatChecker:
    def __init__(self, checker: FormatChecker):
        self.checkers = dict(checker.checkers)
        self._checker = checker
        self._results: dict[
            tuple[str, Any],
            tuple[bool, str | None, BaseException | None],
        ] = {}

    def check(self, instance: object, format: str) -> None:
        if format not in self.checkers:
            return

        key = _format_cache_key(instance)
        if key is None:
            self._checker.check(instance, format)
            return

        cache_key = (format, key)
        try:
            result = self._results[cache_key]
        except KeyError:
            try:
                self._checker.check(instance, format)
            except exceptions.FormatError as error:
                result = (False, error.message, error.cause)
            else:
                result = (True, None, None)
            self._results[cache_key] = result

        valid, message, cause = result
        if not valid:
            raise exceptions.FormatError(message, cause=cause)


def _format_cache_key(instance: object) -> Any | None:
    key = _utils._uniq_key(instance)
    if key is _utils._UNSUPPORTED:
        return None
    return key


class IncrementalValidationSession:
    """
    Validate a document repeatedly while applying JSON Patch operations.

    Structural keywords, including ``required``, ``dependentSchemas``,
    ``contains``, array bounds, ``uniqueItems``, references, and dynamic
    references, are re-evaluated for every resulting document. Successful
    and failing format checks are reused when both the format and instance
    value are unchanged, including after a value moves to another path.

    Only the Draft 2020-12 dialect is supported. A Draft 2020-12 validator
    with custom validation keywords can be used, but its custom keywords
    cause a complete validation without format-result reuse.
    """

    def __init__(self, validator: Draft202012Validator, instance: object):
        meta_schema_id = Draft202012Validator.META_SCHEMA["$id"]
        if validator.META_SCHEMA.get("$id") != meta_schema_id:
            raise TypeError(
                "IncrementalValidationSession only supports Draft 2020-12.",
            )

        custom_validators = (
            type(validator) is not Draft202012Validator
            or validator.VALIDATORS != Draft202012Validator.VALIDATORS
        )
        if custom_validators or validator.format_checker is None:
            self._validator = validator
        else:
            self._validator = validator.evolve(
                format_checker=_MemoizedFormatChecker(
                    validator.format_checker,
                ),
            )

        self._document = deepcopy(instance)
        self.errors = list(self._validator.iter_errors(self._document))

    @classmethod
    def for_schema(
        cls,
        schema: Mapping[str, Any] | bool,
        instance: object,
        format_checker: FormatChecker | None = None,
    ) -> IncrementalValidationSession:
        validator = Draft202012Validator(
            schema,
            format_checker=format_checker,
        )
        return cls(validator, instance)

    @property
    def document(self) -> object:
        return deepcopy(self._document)

    def apply_patch(
        self,
        patch: Sequence[Mapping[str, Any]],
    ) -> list[exceptions.ValidationError]:
        patched = apply_json_patch(self._document, patch)
        errors = list(self._validator.iter_errors(patched))
        self._document = patched
        self.errors = errors
        return list(errors)


def apply_json_patch(
    document: object,
    patch: Sequence[Mapping[str, Any]],
) -> object:
    """
    Return a deep copy of ``document`` with an RFC 6902 patch applied.
    """
    if not isinstance(patch, Sequence) or isinstance(patch, str):
        raise JsonPatchError("JSON Patch must be an array.")

    result = deepcopy(document)
    for operation in patch:
        if not isinstance(operation, Mapping):
            raise JsonPatchError(
                "Each JSON Patch operation must be an object.",
            )
        result = _apply_operation(result, operation)
    return result


def _apply_operation(
    document: object,
    operation: Mapping[str, Any],
) -> object:
    op = operation.get("op")
    path = operation.get("path")
    if not isinstance(op, str):
        raise JsonPatchError("A JSON Patch operation requires a string 'op'.")
    if not isinstance(path, str):
        raise JsonPatchError(
            "A JSON Patch operation requires a string 'path'.",
        )

    tokens = _parse_pointer(path)

    if op in {"add", "replace"}:
        if "value" not in operation:
            raise JsonPatchError(f"The {op!r} operation requires a value.")
        value = deepcopy(operation["value"])
        if op == "add":
            return _add(document, tokens, value)
        _require_pointer(document, tokens)
        return _replace(document, tokens, value)

    if op == "remove":
        _require_pointer(document, tokens)
        return _remove(document, tokens)

    if op in {"move", "copy"}:
        source = operation.get("from")
        if not isinstance(source, str):
            raise JsonPatchError(
                f"The {op!r} operation requires a string 'from'.",
            )
        source_tokens = _parse_pointer(source)
        _require_pointer(document, source_tokens)
        if op == "move" and _is_path_prefix(source_tokens, tokens):
            raise JsonPatchError(
                "A value cannot be moved into one of its children.",
            )
        value = deepcopy(_resolve(document, source_tokens))
        if op == "move" and source_tokens != tokens:
            document = _remove(document, source_tokens)
        return _add(document, tokens, value)

    if op == "test":
        if "value" not in operation:
            raise JsonPatchError("The 'test' operation requires a value.")
        actual = _resolve(document, tokens)
        if not _utils.equal(actual, operation["value"]):
            raise JsonPatchError("The JSON Patch test operation failed.")
        return document

    raise JsonPatchError(f"Unsupported JSON Patch operation {op!r}.")


def _parse_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise JsonPatchError(f"Invalid JSON Pointer {pointer!r}.")
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer.split("/")[1:]
    ]


def _is_path_prefix(prefix: Sequence[str], path: Sequence[str]) -> bool:
    return (
        len(prefix) < len(path)
        and tuple(path[: len(prefix)]) == tuple(prefix)
    )


def _parent(document: object, tokens: Sequence[str]):
    parent = document
    for token in tokens[:-1]:
        parent = _child(parent, token)
    return parent, tokens[-1]


def _require_pointer(document: object, tokens: Sequence[str]) -> None:
    _resolve(document, tokens)


def _resolve(document: object, tokens: Sequence[str]) -> object:
    current = document
    for token in tokens:
        current = _child(current, token)
    return current


def _child(container: object, token: str) -> object:
    if isinstance(container, Mapping):
        try:
            return container[token]
        except KeyError:
            raise JsonPatchError(
                f"JSON Pointer target {token!r} is missing.",
            ) from None

    if isinstance(container, Sequence) and not isinstance(container, str):
        index = _array_index(token, len(container) - 1)
        try:
            return container[index]
        except IndexError:
            raise JsonPatchError(
                f"JSON Pointer index {index} is out of range.",
            ) from None

    raise JsonPatchError("JSON Pointer target is not an object or array.")


def _array_index(token: str, maximum: int, allow_end: bool = False) -> int:
    if token == "-" or not token.isascii() or not token.isdigit():
        raise JsonPatchError(f"Invalid JSON Pointer array index {token!r}.")
    if len(token) > 1 and token.startswith("0"):
        raise JsonPatchError(f"Invalid JSON Pointer array index {token!r}.")
    index = int(token)
    limit = maximum + 1 if allow_end else maximum
    if index > limit:
        raise JsonPatchError(f"JSON Pointer index {index} is out of range.")
    return index


def _add(document: object, tokens: Sequence[str], value: object) -> object:
    if not tokens:
        return value

    container, token = _parent(document, tokens)
    if isinstance(container, Mapping):
        container[token] = value
    elif isinstance(container, Sequence) and not isinstance(container, str):
        if token == "-":
            index = len(container)
        else:
            index = _array_index(token, len(container), allow_end=True)
        container.insert(index, value)
    else:
        raise JsonPatchError("JSON Patch target is not an object or array.")
    return document


def _replace(document: object, tokens: Sequence[str], value: object) -> object:
    if not tokens:
        return value

    container, token = _parent(document, tokens)
    if isinstance(container, Mapping):
        if token not in container:
            raise JsonPatchError(f"JSON Pointer target {token!r} is missing.")
        container[token] = value
    elif isinstance(container, Sequence) and not isinstance(container, str):
        index = _array_index(token, len(container) - 1)
        container[index] = value
    else:
        raise JsonPatchError("JSON Patch target is not an object or array.")
    return document


def _remove(document: object, tokens: Sequence[str]) -> object:
    if not tokens:
        raise JsonPatchError("The document root cannot be removed.")

    container, token = _parent(document, tokens)
    if isinstance(container, Mapping):
        try:
            del container[token]
        except KeyError:
            raise JsonPatchError(
                f"JSON Pointer target {token!r} is missing.",
            ) from None
    elif isinstance(container, Sequence) and not isinstance(container, str):
        index = _array_index(token, len(container) - 1)
        try:
            container.pop(index)
        except IndexError:
            raise JsonPatchError(
                f"JSON Pointer index {index} is out of range.",
            ) from None
    else:
        raise JsonPatchError("JSON Patch target is not an object or array.")
    return document
