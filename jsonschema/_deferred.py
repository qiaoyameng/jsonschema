"""
An explicit deferred ``format`` protocol.

Some ``format`` checks need to consult an external directory or service.
This module never performs any networking itself. Instead, a first local
validation pass collects the checks that remain to be done into a purely
data-based `FormatCheckPlan`, which the caller resolves out of process and
hands back to `DeferredFormatValidator.resolve`.

A plan binds each requested check to:

* the schema (including the ``$id`` of the resource it belongs to),
* a digest of the exact instance value,
* the format name, and
* the JSON path of the instance location.

Every one of those bindings is re-checked when results come back, so a
missing, duplicated, conflicting, or foreign result is rejected rather than
silently treated as a pass or a failure.

No callable or other arbitrary Python object is ever stored in a plan or in
a result; both are plain JSON-compatible data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from itertools import count
import json

from jsonschema import _keywords, _legacy_keywords, _types
from jsonschema.exceptions import FormatError, ValidationError

#: Validation could be completed locally without any external information.
VALID = "valid"

#: Local validation produced one or more `ValidationError` objects.
INVALID = "invalid"

#: The conclusion depends on external format checks which are not yet known.
PENDING = "pending"


def methodcaller_items(schema):
    return schema.items()


class DeferredFormatError(ValueError):
    """
    A deferred format plan or its results were malformed.

    This is raised for protocol violations (unknown plan, duplicated or
    conflicting results, a result which does not match what was asked for),
    never for an instance being invalid under a schema.
    """


class DeferredValidationError(Exception):
    """
    Raised when a fully resolved validation concludes that the instance is
    invalid.
    """

    def __init__(self, errors: Sequence[ValidationError]):
        self.errors = list(errors)
        messages = "; ".join(each.message for each in self.errors)
        super().__init__(messages)

    def __str__(self) -> str:
        return "; ".join(each.message for each in self.errors)


def _json_default(value):
    raise DeferredFormatError(
        f"Plans and results must contain only JSON-compatible data, but "
        f"found {value!r} of type {type(value).__name__}.",
    )


def _canonical(value) -> bytes:
    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        ensure_ascii=False,
    )
    return text.encode("utf-8")


def _digest(value) -> str:
    return sha256(_canonical(value)).hexdigest()


def _json_path(path: Iterable[str | int]) -> str:
    out = "$"
    for element in path:
        if isinstance(element, int):
            out += f"[{element}]"
        else:
            escaped = str(element).replace("\\", "\\\\").replace("'", r"\'")
            out += f"['{escaped}']"
    return out


@dataclass(frozen=True)
class _Outcome:
    status: str
    errors: tuple[ValidationError, ...] = ()
    pending: tuple[str, ...] = ()

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING


_VALID_OUTCOME = _Outcome(VALID)


@dataclass(frozen=True)
class _LocalFormatOutcome:
    """
    A cached result of a completed local (synchronous) format callback.

    Only plain data is retained so recovery can avoid calling the callback
    a second time.
    """

    ok: bool
    message: str = ""
    cause: Exception | None = None


def _outcome_from_errors(
    errors: Iterable[ValidationError],
    pending: Iterable[str] = (),
) -> _Outcome:
    errors = tuple(errors)
    pending = tuple(dict.fromkeys(pending))
    if errors:
        return _Outcome(INVALID, errors, pending)
    if pending:
        return _Outcome(PENDING, (), pending)
    return _VALID_OUTCOME


class DeferredFormatChecker:
    """
    Declares which ``format`` values cannot be checked locally.

    Unlike `jsonschema.FormatChecker`, this object holds no executable
    checking logic -- it only names the formats which must be deferred to
    the caller. Everything else about format validation stays synchronous
    and untouched.

    Arguments:

        formats:

            The format names which require an external check. Any other
            format encountered during validation is ignored, exactly as
            with the default behavior of the ``format`` keyword.
    """

    def __init__(self, formats: Iterable[str]):
        self._formats: frozenset[str] = frozenset(formats)

    @property
    def formats(self) -> frozenset[str]:
        """The formats whose checks must be performed externally."""
        return self._formats

    def is_deferred(self, format: str) -> bool:
        return format in self._formats

    def __repr__(self) -> str:
        return f"<DeferredFormatChecker formats={sorted(self._formats)}>"


def format(validator, format, instance, schema):
    """
    The ``format`` keyword for deferred validation.

    Deferred formats are never decided locally: an unknown external result
    is neither a pass nor a failure, so a placeholder error tagged with the
    request id is emitted. During recovery the recorded request id is simply
    looked up. Synchronous formats are also cached by location, format and
    instance digest so their callback is never re-invoked on recovery.
    """
    checker = validator.format_checker
    if isinstance(checker, DeferredFormatChecker) and checker.is_deferred(
        format,
    ):
        if validator._resolving:
            request_id = validator._lookup_request_id(format, instance)
            conforms, message = validator._external_result(request_id)
            if not conforms:
                yield ValidationError(
                    message or f"{instance!r} is not a {format!r}",
                    validator="format",
                    validator_value=format,
                )
            return
        request_id = validator._register(format, instance)
        error = ValidationError(
            f"{instance!r} requires an external {format!r} check",
        )
        error._deferred_request_id = request_id
        yield error
        return

    outcome = validator._local_format(format, instance)
    if outcome is not None and not outcome.ok:
        yield ValidationError(
            outcome.message,
            cause=outcome.cause,
            validator="format",
            validator_value=format,
        )


@dataclass(frozen=True)
class FormatCheckRequest:
    """
    A single external format check, identified within a plan.

    Everything here is plain data: the instance location, the format name,
    and a digest of the exact value which must be checked.
    """

    id: str
    path: str
    format: str
    instance_digest: str

    def to_data(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "format": self.format,
            "instance_digest": self.instance_digest,
        }

    @classmethod
    def from_data(cls, data: Mapping) -> FormatCheckRequest:
        return cls(
            id=data["id"],
            path=data["path"],
            format=data["format"],
            instance_digest=data["instance_digest"],
        )


@dataclass(frozen=True)
class FormatCheckPlan:
    """
    The checks a caller must perform before a conclusion can be reached.

    A plan is pure data. It binds the schema (including the ``$id`` of the
    resource each check was found in), a digest of the instance, and one
    `FormatCheckRequest` per deferred format occurrence.
    """

    id: str
    schema_id: str
    schema_digest: str
    instance_digest: str
    checks: tuple[FormatCheckRequest, ...] = ()

    @property
    def pending(self) -> bool:
        """Whether any external checks are still needed."""
        return bool(self.checks)

    def to_data(self) -> dict:
        return {
            "id": self.id,
            "schema_id": self.schema_id,
            "schema_digest": self.schema_digest,
            "instance_digest": self.instance_digest,
            "checks": [each.to_data() for each in self.checks],
        }

    @classmethod
    def from_data(cls, data: Mapping) -> FormatCheckPlan:
        return cls(
            id=data["id"],
            schema_id=data["schema_id"],
            schema_digest=data["schema_digest"],
            instance_digest=data["instance_digest"],
            checks=tuple(
                FormatCheckRequest.from_data(each) for each in data["checks"]
            ),
        )


@dataclass(frozen=True)
class FormatCheckResult:
    """
    The caller-provided outcome of one external format check.

    ``conforms`` must be a boolean. ``error`` is an optional, human-readable
    explanation which becomes the validation error message when the check
    fails.
    """

    id: str
    conforms: bool
    error: str | None = None

    def to_data(self) -> dict:
        data = {"id": self.id, "conforms": self.conforms}
        if self.error is not None:
            data["error"] = self.error
        return data

    @classmethod
    def from_data(cls, data: Mapping) -> FormatCheckResult:
        return cls(
            id=data["id"],
            conforms=data["conforms"],
            error=data.get("error"),
        )


@dataclass(frozen=True)
class DeferredValidationResult:
    """
    The conclusion of a deferred validation.

    ``status`` is one of ``"valid"``, ``"invalid"`` or ``"pending"``.
    ``"pending"`` is produced by the one-shot `validate` entry point when
    no results have been supplied yet; ``plan`` then describes what is
    still needed.
    """

    status: str
    errors: tuple[ValidationError, ...] = ()
    plan: FormatCheckPlan | None = None

    @property
    def is_valid(self) -> bool:
        return self.status == VALID

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING

    def raise_for_errors(self) -> None:
        if self.errors:
            raise DeferredValidationError(self.errors)


def _pending_id(error: ValidationError) -> str | None:
    return getattr(error, "_deferred_request_id", None)




def anyOf(validator, anyOf, instance, schema):
    all_errors, all_pending = [], set()
    for index, subschema in enumerate(anyOf):
        outcome = validator.outcome(instance, subschema, schema_path=index)
        if outcome.status == VALID:
            return
        all_pending.update(outcome.pending)
        all_errors.extend(outcome.errors)
    if all_pending:
        for request_id in sorted(all_pending):
            yield _placeholder_error(
                request_id, instance, schema, anyOf, "anyOf",
            )
        return
    yield ValidationError(
        f"{instance!r} is not valid under any of the given schemas",
        context=all_errors,
    )


def oneOf(validator, oneOf, instance, schema):
    valid_indexes, all_errors = [], []
    all_pending = set()
    for index, subschema in enumerate(oneOf):
        outcome = validator.outcome(instance, subschema, schema_path=index)
        if outcome.status == VALID:
            valid_indexes.append(index)
        elif outcome.status == PENDING:
            all_pending.update(outcome.pending)
        else:
            all_errors.extend(outcome.errors)

    if all_pending:
        # Whether exactly one subschema matches cannot be known yet; an
        # undecided branch is treated as neither a pass nor a failure.
        for request_id in sorted(all_pending):
            yield _placeholder_error(
                request_id, instance, schema, oneOf, "oneOf",
            )
        return

    if not valid_indexes:
        yield ValidationError(
            f"{instance!r} is not valid under any of the given schemas",
            context=all_errors,
        )
    elif len(valid_indexes) > 1:
        first, *rest = valid_indexes
        ordered = rest + [first]
        reprs = ", ".join(repr(oneOf[index]) for index in ordered)
        yield ValidationError(
            f"{instance!r} is valid under each of {reprs}",
        )


def not_(validator, not_schema, instance, schema):
    outcome = validator.outcome(instance, not_schema)
    if outcome.status == PENDING:
        for request_id in sorted(outcome.pending):
            yield _placeholder_error(
                request_id, instance, schema, not_schema, "not",
            )
        return
    if outcome.status == VALID:
        yield ValidationError(
            f"{instance!r} should not be valid under {not_schema!r}",
        )


def if_(validator, if_schema, instance, schema):
    outcome = validator.outcome(instance, if_schema)
    if outcome.status == PENDING:
        for request_id in sorted(outcome.pending):
            yield _placeholder_error(
                request_id, instance, schema, if_schema, "if",
            )
        return
    if outcome.status == VALID:
        if "then" in schema:
            yield from validator.descend(
                instance, schema["then"], schema_path="then",
            )
    elif "else" in schema:
        yield from validator.descend(
            instance, schema["else"], schema_path="else",
        )


def contains(validator, contains, instance, schema):
    if not validator.is_type(instance, "array"):
        return

    min_contains = schema.get("minContains", 1)
    max_contains = schema.get("maxContains", len(instance))
    matches, pending = 0, set()
    for index, element in enumerate(instance):
        outcome = validator.outcome(element, contains, path=index)
        if outcome.status == VALID:
            matches += 1
            if matches > max_contains:
                yield ValidationError(
                    "Too many items match the given schema "
                    f"(expected at most {max_contains})",
                    validator="maxContains",
                    validator_value=max_contains,
                )
                return
        elif outcome.status == PENDING:
            pending.update(outcome.pending)

    if pending:
        for request_id in sorted(pending):
            yield _placeholder_error(
                request_id, instance, schema, contains, "contains",
            )
    elif matches < min_contains:
        if not matches:
            yield ValidationError(
                f"{instance!r} does not contain items "
                "matching the given schema",
            )
        else:
            yield ValidationError(
                "Too few items match the given schema (expected at least "
                f"{min_contains} but only {matches} matched)",
                validator="minContains",
                validator_value=min_contains,
            )


def _placeholder_error(request_id, instance, schema, validator_value, keyword):
    error = ValidationError(
        f"external format check {request_id!r} is pending",
        validator=keyword,
        validator_value=validator_value,
        instance=instance,
        schema=schema,
    )
    error._deferred_request_id = request_id
    return error


def ref(validator, ref, instance, schema):
    yield from validator._validate_reference(ref=ref, instance=instance)


def recursiveRef(validator, recursiveRef, instance, schema):
    from referencing.jsonschema import lookup_recursive_ref

    resolved = lookup_recursive_ref(validator._ensure_resolver())
    child = validator.evolve(
        schema=resolved.contents,
        _resolver=resolved.resolver,
    )
    yield from child._validate_node(instance, resolved.contents)


def _deferred_keywords(legacy: bool) -> dict[str, Callable]:
    keywords = {
        "$ref": ref,
        "$dynamicRef": ref,
        "additionalProperties": _keywords.additionalProperties,
        "allOf": _keywords.allOf,
        "anyOf": anyOf,
        "const": _keywords.const,
        "contains": contains,
        "dependentRequired": _keywords.dependentRequired,
        "dependentSchemas": _keywords.dependentSchemas,
        "enum": _keywords.enum,
        "exclusiveMaximum": _keywords.exclusiveMaximum,
        "exclusiveMinimum": _keywords.exclusiveMinimum,
        "format": format,
        "items": _keywords.items,
        "maxItems": _keywords.maxItems,
        "maxLength": _keywords.maxLength,
        "maxProperties": _keywords.maxProperties,
        "maximum": _keywords.maximum,
        "minItems": _keywords.minItems,
        "minLength": _keywords.minLength,
        "minProperties": _keywords.minProperties,
        "minimum": _keywords.minimum,
        "multipleOf": _keywords.multipleOf,
        "not": not_,
        "oneOf": oneOf,
        "pattern": _keywords.pattern,
        "patternProperties": _keywords.patternProperties,
        "prefixItems": _keywords.prefixItems,
        "properties": _keywords.properties,
        "propertyNames": _keywords.propertyNames,
        "required": _keywords.required,
        "type": _keywords.type,
        "uniqueItems": _keywords.uniqueItems,
        "if": if_,
        "unevaluatedItems": _keywords.unevaluatedItems,
        "unevaluatedProperties": _keywords.unevaluatedProperties,
    }
    if legacy:
        keywords.update(
            {
                "$recursiveRef": recursiveRef,
                "additionalItems": _legacy_keywords.additionalItems,
                "dependencies": (
                    _legacy_keywords.dependencies_draft4_draft6_draft7
                ),
                "items": _legacy_keywords.items_draft6_draft7_draft201909,
            },
        )
    return keywords


class DeferredFormatValidator:
    """
    Runs the local pass of a validation which may contain deferred formats.

    Call `plan` to perform local validation and obtain a data-based
    `FormatCheckPlan`; then call `resolve` with the caller's external
    `FormatCheckResult` objects to reach the final
    `DeferredValidationResult`.

    The engine caches the outcome of every node it visits during the first
    pass, keyed by node location, so resolving never has to call any local
    format check a second time. Nothing callable or otherwise arbitrary is
    stored in a plan or accepted result, and no networking is performed.
    """

    def __init__(
        self,
        schema,
        *,
        type_checker: _types.TypeChecker,
        format_checker: DeferredFormatChecker,
        registry,
        applicable_validators,
        keywords: Mapping[str, Callable],
        specification,
        id_of=None,
        sync_format_checker=None,
    ):
        self.schema = schema
        self.TYPE_CHECKER = type_checker
        self.format_checker = format_checker
        self._sync_format_checker = sync_format_checker
        self._registry = registry
        self._applicable_validators = applicable_validators
        self._keywords = dict(keywords)
        self._specification = specification
        self._id_of = id_of
        self._resolver = None
        self._path: tuple = ()
        self._plan_id = ""
        self._requests: list[FormatCheckRequest] = []
        self._id_counter = count()
        # Outcome caching so recovery never repeats a completed callback.
        self._local_formats: dict[tuple, _LocalFormatOutcome] = {}
        self._resolving = False
        self._resolved: dict[str, tuple[bool, str | None]] = {}
        self._last_outcome = _VALID_OUTCOME

    def evolve(self, **changes) -> DeferredFormatValidator:
        child = object.__new__(type(self))
        child.schema = changes.get("schema", self.schema)
        child.TYPE_CHECKER = self.TYPE_CHECKER
        child.format_checker = self.format_checker
        child._sync_format_checker = self._sync_format_checker
        child._registry = self._registry
        child._applicable_validators = self._applicable_validators
        child._keywords = self._keywords
        child._specification = self._specification
        child._id_of = self._id_of
        child._resolver = changes.get("_resolver", self._resolver)
        child._path = changes.get("_path", self._path)
        child._plan_id = self._plan_id
        child._requests = self._requests
        child._id_counter = self._id_counter
        child._local_formats = self._local_formats
        child._resolving = self._resolving
        child._resolved = self._resolved
        return child

    def is_type(self, instance, type: str) -> bool:
        return self.TYPE_CHECKER.is_type(instance, type)

    def is_valid(self, instance) -> bool:
        """
        Whether a node is valid, for internal "was it evaluated" helpers.

        This must return a bool for shared keyword code. A pending outcome
        is never a failure (so an undecided node is not silently marked
        invalid); it is treated as provisionally validated so recovery can
        still reach the pending checks. The final conclusion is never taken
        from this heuristic -- `resolve` replays the plan and reports any
        failing external result explicitly.
        """
        return self.outcome(instance, self.schema).status != INVALID

    def _ensure_resolver(self):
        if self._resolver is None:
            resource = self._specification.create_resource(self.schema)
            self._resolver = self._registry.resolver_with_root(resource)
        return self._resolver

    def _resolver_for(self, schema):
        resolver = self._ensure_resolver()
        if (
            schema is True
            or schema is False
            or not isinstance(schema, Mapping)
        ):
            return resolver
        return resolver.in_subresource(
            self._specification.create_resource(schema),
        )

    def _register(self, format: str, instance) -> str:
        path = _json_path(self._path)
        digest = _digest(instance)
        for request in self._requests:
            if (
                request.path == path
                and request.format == format
                and request.instance_digest == digest
            ):
                # The same value at the same location with the same format
                # is one check, however many subschemas ask for it.
                return request.id
        # Request ids are namespaced by the plan id, so a result produced
        # for some other plan can never be mistaken for one of ours.
        request_id = f"{self._plan_id[:16]}-{next(self._id_counter)}"
        request = FormatCheckRequest(
            id=request_id,
            path=path,
            format=format,
            instance_digest=digest,
        )
        self._requests.append(request)
        return request_id

    def _deferred_register(self, format: str, instance) -> str:
        return self._register(format, instance)

    def _format_cache_key(self, format: str, instance) -> tuple:
        return (_json_path(self._path), format, _digest(instance))

    def _local_format(self, format: str, instance):
        """
        Run a synchronous format check at most once per node and value.

        Only the boolean outcome and error message are cached; the callable
        itself is never stored, and on recovery the cached outcome is reused
        instead of calling the checker again.
        """
        key = self._format_cache_key(format, instance)
        cached = self._local_formats.get(key)
        if cached is not None:
            return cached
        outcome = self._invoke_local_format(format, instance)
        self._local_formats[key] = outcome
        return outcome

    def _invoke_local_format(self, format: str, instance):
        checker = self._sync_format_checker
        if checker is None or format not in checker.checkers:
            return None
        try:
            checker.check(instance, format)
        except FormatError as error:
            return _LocalFormatOutcome(
                ok=False,
                message=error.message,
                cause=error.cause,
            )
        return _LocalFormatOutcome(ok=True)

    def _lookup_request_id(self, format: str, instance) -> str:
        digest = _digest(instance)
        for request in self._requests:
            if (
                request.path == _json_path(self._path)
                and request.format == format
                and request.instance_digest == digest
            ):
                return request.id
        raise DeferredFormatError(
            f"No planned request found for format {format!r} at "
            f"{_json_path(self._path)}.",
        )

    def _external_result(self, request_id: str) -> tuple[bool, str | None]:
        try:
            return self._resolved[request_id]
        except KeyError:
            raise DeferredFormatError(
                f"Missing external result for request {request_id!r}.",
            ) from None

    def _validate_node(self, instance, schema):
        if schema is True:
            return
        if schema is False:
            yield ValidationError(
                f"False schema does not allow {instance!r}",
                validator=None,
                validator_value=None,
                instance=instance,
                schema=schema,
            )
            return
        for keyword, value in self._applicable_validators(schema):
            fn = self._keywords.get(keyword)
            if fn is None:
                continue
            for error in fn(self, value, instance, schema) or ():
                error._set(
                    validator=keyword,
                    validator_value=value,
                    instance=instance,
                    schema=schema,
                    type_checker=self.TYPE_CHECKER,
                )
                if keyword not in {
                    "if",
                    "$ref",
                    "$dynamicRef",
                    "$recursiveRef",
                }:
                    error.schema_path.appendleft(keyword)
                yield error

    def descend(self, instance, schema, path=None, schema_path=None):
        child = self.evolve(
            schema=schema,
            _resolver=self._resolver_for(schema),
            _path=self._path if path is None else (*self._path, path),
        )
        for error in child._validate_node(instance, schema):
            if path is not None:
                error.path.appendleft(path)
            if schema_path is not None:
                error.schema_path.appendleft(schema_path)
            yield error

    def outcome(self, instance, schema, *, path=None, schema_path=None):
        child = self.evolve(
            schema=schema,
            _resolver=self._resolver_for(schema),
            _path=self._path if path is None else (*self._path, path),
        )
        errors, pending = [], []
        for error in child._validate_node(instance, schema):
            if path is not None:
                error.path.appendleft(path)
            if schema_path is not None:
                error.schema_path.appendleft(schema_path)
            request_id = _pending_id(error)
            if request_id is None:
                errors.append(error)
            else:
                pending.append(request_id)
        return _outcome_from_errors(errors, pending)

    def _validate_reference(self, ref, instance):
        resolved = self._ensure_resolver().lookup(ref)
        child = self.evolve(
            schema=resolved.contents,
            _resolver=resolved.resolver,
        )
        yield from child._validate_node(instance, resolved.contents)

    def _run(self, instance):
        self._path = ()
        self._ensure_resolver()
        self._plan_id = sha256(
            _canonical(
                {
                    "$id": self.schema_id(),
                    "schema": self.schema,
                    "instance": _digest(instance),
                },
            ),
        ).hexdigest()
        errors, pending = [], set()
        for error in self._validate_node(instance, self.schema):
            request_id = _pending_id(error)
            if request_id is None:
                errors.append(error)
            else:
                pending.add(request_id)
        outcome = _outcome_from_errors(errors, pending)
        self._last_outcome = outcome
        return outcome, tuple(self._requests)

    def schema_id(self) -> str:
        if isinstance(self.schema, bool):
            return "true" if self.schema else "false"
        if self._id_of is not None:
            value = self._id_of(self.schema)
            if value is not None:
                return value
        return getattr(self._ensure_resolver(), "_base_uri", "") or ""

    def plan(self, instance) -> FormatCheckPlan:
        """Run local validation and return the data-based plan."""
        outcome, requests = self._run(instance)
        return FormatCheckPlan(
            id=self._plan_id,
            schema_id=self.schema_id(),
            schema_digest=_digest(self.schema),
            instance_digest=_digest(instance),
            checks=tuple(requests),
        )

    def resolve(
        self,
        instance,
        plan: FormatCheckPlan,
        results: Mapping[str, tuple[bool, str | None]],
    ) -> DeferredValidationResult:
        """Finish a plan with already-validated external results."""
        self._resolving = True
        self._resolved = dict(results)
        self._plan_id = plan.id
        self._requests = list(plan.checks)
        outcome, requests = self._run(instance)
        self._resolving = False
        if tuple(requests) != plan.checks:
            raise DeferredFormatError(
                "Replaying the plan produced different requests than the "
                "recorded plan.",
            )
        return _finish_pending(outcome, plan, results)


def _finish_pending(
    outcome: _Outcome,
    plan: FormatCheckPlan,
    resolved: Mapping[str, tuple[bool, str | None]],
) -> DeferredValidationResult:
    """
    Turn a fully-resolved replay into the final conclusion.

    By the time this runs, the replay's ``format`` keyword has already
    translated each accepted external result into either nothing (a pass)
    or a concrete format error, alongside every purely local error.
    """
    del resolved  # applied during the replay itself
    if outcome.errors:
        return DeferredValidationResult(INVALID, outcome.errors, plan)
    return DeferredValidationResult(VALID, (), plan)


def _coerce_results(
    results: Iterable[FormatCheckResult | Mapping],
) -> list[FormatCheckResult]:
    coerced = []
    for result in results:
        if isinstance(result, FormatCheckResult):
            coerced.append(result)
        elif isinstance(result, Mapping):
            required = {"id", "conforms"}
            if not required <= set(result):
                raise DeferredFormatError(
                    "Each result mapping needs at least 'id' and 'conforms'.",
                )
            coerced.append(FormatCheckResult.from_data(result))
        else:
            raise DeferredFormatError(
                "Each result must be a FormatCheckResult or a mapping, but "
                f"found {result!r}.",
            )
    return coerced


def _validate_results(
    plan: FormatCheckPlan,
    results: Sequence[FormatCheckResult],
    instance,
    schema,
) -> dict[str, tuple[bool, str | None]]:
    """
    Verify every result against the plan's bindings.

    A result is accepted only if it answers a request from this exact plan,
    with no missing, duplicated, conflicting, or foreign entries. The
    instance and schema must also be the ones the plan was created for.
    """
    if _digest(instance) != plan.instance_digest:
        raise DeferredFormatError(
            "The instance does not match the digest bound to this plan.",
        )
    if _digest(schema) != plan.schema_digest:
        raise DeferredFormatError(
            "The schema does not match the digest bound to this plan.",
        )

    requests = {request.id: request for request in plan.checks}
    if len(requests) != len(plan.checks):
        raise DeferredFormatError("The plan contains duplicated request ids.")

    by_id: dict[str, tuple[bool, str | None]] = {}
    for result in results:
        if type(result.conforms) is not bool:
            raise DeferredFormatError(
                f"Result {result.id!r} has a non-boolean 'conforms' value: "
                f"{result.conforms!r}.",
            )
        if result.error is not None and not isinstance(result.error, str):
            raise DeferredFormatError(
                f"Result {result.id!r} has a non-string 'error' value: "
                f"{result.error!r}.",
            )
        if result.id not in requests:
            raise DeferredFormatError(
                f"Result {result.id!r} does not belong to this plan "
                f"({plan.id!r}).",
            )
        if result.id in by_id:
            raise DeferredFormatError(
                f"Result {result.id!r} was provided more than once.",
            )
        by_id[result.id] = (result.conforms, result.error)

    missing = sorted(set(requests) - set(by_id))
    if missing:
        raise DeferredFormatError(
            "Missing external results for: " + ", ".join(missing),
        )
    return by_id


def _build_engine(
    schema,
    *,
    format_checker,
    type_checker,
    registry,
    applicable_validators,
    keywords,
    specification,
    id_of,
    sync_format_checker,
    legacy,
) -> DeferredFormatValidator:
    return DeferredFormatValidator(
        schema,
        type_checker=type_checker,
        format_checker=format_checker,
        registry=registry,
        applicable_validators=applicable_validators,
        keywords=keywords if keywords is not None else _deferred_keywords(
            legacy,
        ),
        specification=specification,
        id_of=id_of,
        sync_format_checker=sync_format_checker,
    )


def prepare(
    instance,
    schema,
    *,
    format_checker: DeferredFormatChecker | Iterable[str],
    type_checker: _types.TypeChecker = _types.draft202012_type_checker,
    registry=None,
    applicable_validators=methodcaller_items,
    keywords: Mapping[str, Callable] | None = None,
    specification=None,
    id_of=None,
    sync_format_checker=None,
    legacy: bool = False,
) -> tuple[DeferredFormatValidator, FormatCheckPlan]:
    """
    Run the local validation pass and return the engine plus its plan.

    The returned `FormatCheckPlan` contains only JSON-compatible data and
    can be serialized and given to whichever system performs the external
    checks. No network access happens here.
    """
    if specification is None:
        from referencing.jsonschema import DRAFT202012

        specification = DRAFT202012
    if registry is None:
        from referencing import Registry

        registry = Registry()
    if not isinstance(format_checker, DeferredFormatChecker):
        format_checker = DeferredFormatChecker(format_checker)

    engine = _build_engine(
        schema,
        format_checker=format_checker,
        type_checker=type_checker,
        registry=registry,
        applicable_validators=applicable_validators,
        keywords=keywords,
        specification=specification,
        id_of=id_of,
        sync_format_checker=sync_format_checker,
        legacy=legacy,
    )
    plan = engine.plan(instance)
    return engine, plan


def plan(
    instance,
    schema,
    *,
    format_checker: DeferredFormatChecker | Iterable[str],
    **kwargs,
) -> FormatCheckPlan:
    """Return the external-check plan for a deferred validation."""
    _, planned = prepare(
        instance,
        schema,
        format_checker=format_checker,
        **kwargs,
    )
    return planned


def resolve(
    instance,
    schema,
    plan: FormatCheckPlan | Mapping,
    results: Iterable[FormatCheckResult | Mapping],
    *,
    format_checker: DeferredFormatChecker | Iterable[str],
    type_checker: _types.TypeChecker = _types.draft202012_type_checker,
    registry=None,
    applicable_validators=methodcaller_items,
    keywords: Mapping[str, Callable] | None = None,
    specification=None,
    id_of=None,
    sync_format_checker=None,
    legacy: bool = False,
) -> DeferredValidationResult:
    """
    Finish a deferred validation using externally produced results.

    Missing, duplicated, conflicting, or foreign results raise
    `DeferredFormatError`. Accepted results are replayed into the recorded
    local pass without re-running any completed local format callback.
    """
    if specification is None:
        from referencing.jsonschema import DRAFT202012

        specification = DRAFT202012
    if registry is None:
        from referencing import Registry

        registry = Registry()
    if isinstance(plan, Mapping):
        plan = FormatCheckPlan.from_data(plan)
    if not isinstance(format_checker, DeferredFormatChecker):
        format_checker = DeferredFormatChecker(format_checker)

    engine = _build_engine(
        schema,
        format_checker=format_checker,
        type_checker=type_checker,
        registry=registry,
        applicable_validators=applicable_validators,
        keywords=keywords,
        specification=specification,
        id_of=id_of,
        sync_format_checker=sync_format_checker,
        legacy=legacy,
    )
    coerced = _coerce_results(results)
    resolved = _validate_results(plan, coerced, instance, schema)
    return engine.resolve(instance, plan, resolved)


def validate(
    instance,
    schema,
    *,
    format_checker: DeferredFormatChecker | Iterable[str],
    results: Iterable[FormatCheckResult | Mapping] = (),
    **kwargs,
) -> DeferredValidationResult:
    """Plan and (if results are supplied) resolve in one call."""
    engine, planned = prepare(
        instance,
        schema,
        format_checker=format_checker,
        **kwargs,
    )
    if not results and planned.checks:
        # Local errors already decide the outcome; only a genuinely
        # undecided conclusion is reported as pending.
        if engine._last_outcome.errors:
            return DeferredValidationResult(
                INVALID,
                engine._last_outcome.errors,
                planned,
            )
        return DeferredValidationResult(PENDING, (), planned)
    resolved = _validate_results(
        planned,
        _coerce_results(results),
        instance,
        schema,
    )
    return engine.resolve(instance, planned, resolved)


