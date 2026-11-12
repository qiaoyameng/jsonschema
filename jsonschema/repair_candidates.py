"""
Bounded, review-only repair suggestions for instances which fail validation.

The entry point is `repair_candidates`.  It never mutates the instance or
schema it is given and it never applies a patch; it only reports candidates
which callers (e.g. a review UI) can present to a human.

Two rules shape the search:

* Business values are never invented.  New property values (and, where it is
  safe, replacement values) must come from the caller's ``allowed_values``
  table or from constants appearing in the schema itself (``enum``, ``const``
  and numeric bounds).
* Validity is decided by the original validator.  Every candidate which is
  reported has been re-run through a validator built from the caller's
  schema, and ``RepairCandidate.verified`` is the number of errors that
  validator reported for the patched instance.

The first version supports a small, explicit keyword set: ``properties`` /
``required`` / ``additionalProperties``, ``type`` / ``enum`` / ``const``,
numeric bounds (``minimum``, ``maximum``, ``exclusiveMinimum``,
``exclusiveMaximum``, ``multipleOf``) and ``allOf`` / ``anyOf``.  Anything
else is treated as outside the supported range: the result's ``status`` is
marked ``"unknown"`` instead of pretending to have found an optimum.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from jsonschema import _utils
from jsonschema.validators import validator_for

#: Keywords this first version knows how to reason about.
SUPPORTED_KEYWORDS = frozenset({
    "properties",
    "required",
    "additionalProperties",
    "type",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "allOf",
    "anyOf",
})

_UNKNOWN_STATUS = "unknown"
_INCOMPLETE_STATUS = "incomplete"
_OK_STATUS = "ok"

__all__ = [
    "CostModel",
    "PatchOperation",
    "RepairCandidate",
    "RepairResult",
    "SUPPORTED_KEYWORDS",
    "apply_patch",
    "repair_candidates",
]


@dataclass(frozen=True)
class CostModel:
    """Prices for the three patch operations.

    Adding a property costs ``add``, deleting one costs ``delete`` and
    replacing an existing value costs ``replace``.  Callers can supply their
    own model, or just tune the defaults.
    """

    add: float = 1.0
    delete: float = 1.0
    replace: float = 1.0

    def price(self, kind):
        if kind == "add":
            return self.add
        if kind == "delete":
            return self.delete
        if kind == "replace":
            return self.replace
        raise ValueError(f"unknown patch operation {kind!r}")


@dataclass(frozen=True)
class PatchOperation:
    """One immutable operation in a candidate patch."""

    op: str
    path: tuple
    value: Any = dataclass_field(default=None, compare=False, hash=False)

    def to_dict(self):
        result = {"op": self.op, "path": list(self.path)}
        if self.op in {"add", "replace"}:
            result["value"] = self.value
        return result

    def __hash__(self):
        return hash((self.op, self.path))

    def __eq__(self, other):
        if not isinstance(other, PatchOperation):
            return NotImplemented
        return self.op == other.op and self.path == other.path and (
            self.op == "delete" or _utils.equal(self.value, other.value)
        )


@dataclass(frozen=True)
class RepairCandidate:
    """A reviewable suggestion.  Nothing here has been applied."""

    operations: tuple
    cost: float
    errors_before: int
    verified: int
    validator: str
    optimality: str

    @property
    def patch(self):
        return [operation.to_dict() for operation in self.operations]

    @property
    def valid(self):
        return self.verified == 0

    def to_dict(self):
        return {
            "patch": self.patch,
            "cost": self.cost,
            "errors_before": self.errors_before,
            "verified": self.verified,
            "validator": self.validator,
            "optimality": self.optimality,
            "valid": self.valid,
        }


@dataclass(frozen=True)
class RepairResult:
    """The outcome of a bounded search.

    ``status`` is ``"ok"`` when the search provably enumerated every
    supported candidate, ``"incomplete"`` when a budget cut it short and
    ``"unknown"`` when the schema (or the request) is outside the supported
    range.  ``candidates`` is always ordered by cost and then by patch.
    """

    candidates: tuple
    status: str
    optimal: bool
    explored: int
    reason: str = ""
    unsupported_keywords: tuple = ()
    input_unchanged: bool = True

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self):
        return len(self.candidates)

    def __getitem__(self, index):
        return self.candidates[index]

    def to_dict(self):
        return {
            "candidates": [
                candidate.to_dict() for candidate in self.candidates
            ],
            "status": self.status,
            "optimal": self.optimal,
            "explored": self.explored,
            "reason": self.reason,
            "unsupported_keywords": list(self.unsupported_keywords),
            "input_unchanged": self.input_unchanged,
        }


def repair_candidates(
    instance,
    schema,
    allowed_values=None,
    *,
    cost_model=None,
    max_states=128,
    max_candidates=5,
    cls=None,
    validator=None,
):
    """Return review-only repair candidates for a failing instance.

    Arguments:

        instance: the JSON value that failed validation.  It is never
            mutated.
        schema: the JSON Schema it failed against.
        allowed_values: mapping of instance path (a tuple of keys / indices)
            to a sequence of values the caller permits there.  These are the
            only *new* values that may be suggested, alongside constants
            already present in the schema.
        cost_model: a `CostModel` describing operation prices.
        max_states: candidate budget for the breadth-first search.  Once the
            budget is exhausted the result is marked non-optimal rather than
            guessing.
        max_candidates: maximum number of candidates to return.
        cls: an explicit validator class.  When omitted it is chosen with
            `jsonschema.validators.validator_for`.
        validator: an already-built validator to reuse.  When given it takes
            precedence over ``cls``, which is how a caller guarantees the
            "original validator" performs the re-check.

    The original input is always verified unchanged first, and every
    candidate is re-validated with a validator built from the same schema.
    """
    if cost_model is None:
        cost_model = CostModel()
    if allowed_values is None:
        allowed_values = {}
    allowed = _as_allowed(allowed_values)
    snapshot = _json_clone(instance)

    if validator is None:
        if cls is None:
            cls = validator_for(schema)
        validator = cls(schema)
    validator_name = type(validator).__name__

    baseline_errors = list(validator.iter_errors(instance))
    errors_before = len(baseline_errors)
    if errors_before == 0:
        return RepairResult(
            candidates=(),
            status=_OK_STATUS,
            optimal=True,
            explored=0,
            reason="instance already valid",
            input_unchanged=_utils.equal(instance, snapshot),
        )

    unsupported = _unsupported_keywords(schema)
    if unsupported:
        return RepairResult(
            candidates=(),
            status=_UNKNOWN_STATUS,
            optimal=False,
            explored=0,
            reason=(
                "schema uses keywords outside the supported range; no repair "
                "can be proven complete"
            ),
            unsupported_keywords=tuple(sorted(unsupported)),
            input_unchanged=_utils.equal(instance, snapshot),
        )

    # Anything the schema fixes outright is a legitimate value source without
    # the caller having to repeat it.
    schema_constants = _collect_schema_constants(schema)

    roots = _roots_from_errors(baseline_errors)
    actions = _build_actions(
        roots=roots,
        allowed=allowed,
        constants=schema_constants,
    )

    candidates, complete, budget_exhausted, explored = _search(
        instance=instance,
        actions=actions,
        validator=validator,
        cost_model=cost_model,
        max_states=max_states,
        max_candidates=max_candidates,
        validator_name=validator_name,
        errors_before=errors_before,
    )

    if candidates:
        status = _OK_STATUS if complete else _INCOMPLETE_STATUS
        reason = "" if complete else (
            "search budget reached before optimality could be proven"
        )
    elif budget_exhausted:
        status = _INCOMPLETE_STATUS
        reason = "search budget reached before a valid repair was found"
    elif not actions:
        # The schema is in range, but no permitted value / deletion can
        # address the errors.  We deliberately do not invent business values.
        status = _UNKNOWN_STATUS
        reason = "no permitted repair values or deletions address the errors"
    else:
        # Every combination was tried and none validates.  More expensive
        # permutations cannot help either, so there is no supported repair.
        status = _UNKNOWN_STATUS
        reason = "no supported repair validates under the given inputs"

    return RepairResult(
        candidates=tuple(candidates),
        status=status,
        optimal=complete and bool(candidates),
        explored=explored,
        reason=reason,
        input_unchanged=_utils.equal(instance, snapshot),
    )


def _as_allowed(allowed_values):
    normalized = {}
    for path, values in allowed_values.items():
        key = (path,) if isinstance(path, str) else tuple(path)
        normalized[key] = list(values)
    return normalized


# --------------------------------------------------------------------------- #
# Schema inspection
# --------------------------------------------------------------------------- #
def _walk_subschemas(schema):
    queue = deque([schema])
    while queue:
        current = queue.popleft()
        if isinstance(current, bool):
            continue
        if not isinstance(current, Mapping):
            continue
        yield current
        for keyword in ("properties", "$defs", "definitions",
                        "dependentSchemas"):
            container = current.get(keyword)
            if isinstance(container, Mapping):
                queue.extend(container.values())
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            container = current.get(keyword)
            if isinstance(container, Sequence) and not isinstance(
                container, str,
            ):
                queue.extend(container)
        if isinstance(current.get("items"), Mapping):
            queue.append(current["items"])
        if isinstance(current.get("additionalProperties"), Mapping):
            queue.append(current["additionalProperties"])
        if isinstance(current.get("propertyNames"), Mapping):
            queue.append(current["propertyNames"])


#: Keywords which cannot make an instance invalid, so ignoring them does not
#: weaken the completeness claim.
_IGNORED_KEYWORDS = frozenset({
    "$schema", "$id", "$anchor", "$comment", "$vocabulary", "$defs",
    "definitions", "$dynamicAnchor", "$recursiveAnchor",
    "title", "description", "default", "examples", "deprecated",
    "readOnly", "writeOnly", "contentEncoding", "contentMediaType",
    "contentSchema",
})

#: Reference keywords whose targets we do not follow in this version.
_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})


def _unsupported_keywords(schema):
    found = set()
    for subschema in _walk_subschemas(schema):
        for keyword in subschema:
            if keyword in _IGNORED_KEYWORDS:
                continue
            if keyword in _REFERENCE_KEYWORDS:
                # We cannot prove we enumerated every repair when a reference
                # could pull in arbitrary keywords.
                found.add(keyword)
            elif keyword not in SUPPORTED_KEYWORDS:
                found.add(keyword)
    return found


def _collect_schema_constants(schema):
    """Map instance paths to constant values advertised by the schema."""
    constants = {}

    def visit(subschema, path):
        if isinstance(subschema, bool):
            return
        if "const" in subschema:
            constants.setdefault(path, []).append(subschema["const"])
        if "enum" in subschema:
            for value in subschema["enum"]:
                constants.setdefault(path, []).append(value)
        # Inclusive bounds are themselves valid values, which also lets a
        # missing required numeric property be added without inventing data.
        for keyword in ("minimum", "maximum"):
            bound = subschema.get(keyword)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool):
                constants.setdefault(path, []).append(bound)
        properties = subschema.get("properties")
        if isinstance(properties, Mapping):
            for name, child in properties.items():
                visit(child, path + (name,))
        for keyword in ("allOf", "anyOf"):
            for child in subschema.get(keyword, ()):
                visit(child, path)

    visit(schema, ())
    # De-duplicate while preserving order.
    return {
        path: list(_unique(values))
        for path, values in constants.items()
    }


def _unique(values):
    seen = []
    for value in values:
        if not any(_utils.equal(value, prior) for prior in seen):
            seen.append(value)
            yield value


# --------------------------------------------------------------------------- #
# Error -> action roots
# --------------------------------------------------------------------------- #
def _roots_from_errors(errors):
    roots = []
    seen = set()
    for error in errors:
        if error.validator not in SUPPORTED_KEYWORDS:
            continue
        path = tuple(error.absolute_path)
        # ``message`` distinguishes e.g. "'a' is required" from "'b' is
        # required"; without it distinct required failures at the same path
        # would collapse into a single repair root.
        key = (path, error.validator, error.message)
        if key in seen:
            continue
        seen.add(key)
        roots.append(error)
    return roots


def _build_actions(roots, allowed, constants):
    actions = []
    for error in roots:
        actions.extend(_actions_for_error(error, allowed, constants))
    # Drop duplicates that different error roots can produce (the same
    # missing key may be reported by a top-level required and an allOf).
    return list(_unique_actions(actions))


def _unique_actions(actions):
    seen = set()
    for action in actions:
        if action not in seen:
            seen.add(action)
            yield action


def _actions_for_error(error, allowed, constants):
    path = tuple(error.absolute_path)
    keyword = error.validator

    if keyword == "required":
        missing = _missing_property(error)
        if missing is None:
            return []
        target = path + (missing,)
        return _value_actions("add", target, allowed, constants)

    if keyword == "additionalProperties":
        # The error's path locates the object; each unexpected key is an
        # independent delete, so reviewers can drop them one at a time.
        if error.validator_value is not False:
            return []
        extras = _utils.find_additional_properties(
            error.instance, error.schema,
        )
        return [PatchOperation("delete", path + (extra,)) for extra in extras]

    if keyword in {"type", "enum", "const"}:
        return _value_actions("replace", path, allowed, constants)

    if keyword in {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    }:
        return _numeric_actions(error, path, allowed, constants)

    if keyword in {"allOf", "anyOf"}:
        # The nested error already carries the concrete failing keyword; its
        # absolute path points at the offending value, so regenerate from the
        # deepest supported child instead of inventing an applicator action.
        return _actions_from_children(error, allowed, constants)
    return []


def _missing_property(error):
    # The message is fixed by the required() implementation:
    #   "'name' is a required property"
    message = error.message
    marker = "'"
    if not message.startswith(marker):
        return None
    end = message.find(marker, 1)
    if end == -1:
        return None
    return message[1:end]


def _actions_from_children(error, allowed, constants):
    actions = []
    for child in _deep_errors(error):
        if child.validator in SUPPORTED_KEYWORDS:
            actions.extend(_actions_for_error(child, allowed, constants))
    return list(_unique_actions(actions))


def _deep_errors(error):
    if not error.context:
        return [error]
    leaves = []
    for child in error.context:
        leaves.extend(_deep_errors(child))
    return leaves


# --------------------------------------------------------------------------- #
# Value sourcing (no invented business values)
# --------------------------------------------------------------------------- #
def _value_actions(kind, path, allowed, constants):
    values = _candidate_values(path, allowed, constants)
    if not values:
        return []
    return [PatchOperation(kind, path, value) for value in values]


def _candidate_values(path, allowed, constants):
    values = []
    for source in (allowed.get(path, ()), constants.get(path, ())):
        for value in source:
            if not any(_utils.equal(value, prior) for prior in values):
                values.append(value)
    return values


def _numeric_actions(error, path, allowed, constants):
    actions = []
    current = error.instance
    local_schema = error.schema if isinstance(error.schema, Mapping) else {}

    boundary_values = _boundary_values(local_schema, current)
    sourced = _candidate_values(path, allowed, constants)
    values = []
    for value in boundary_values + sourced:
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            if not any(_utils.equal(value, prior) for prior in values):
                values.append(value)

    for value in values:
        actions.append(PatchOperation("replace", path, value))
    return actions


def _boundary_values(local_schema, current):
    """Values taken from numeric bounds already written in the schema."""
    values = []
    for keyword in ("minimum", "maximum", "exclusiveMinimum",
                    "exclusiveMaximum"):
        bound = local_schema.get(keyword)
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            continue
        if keyword in {"minimum", "maximum"}:
            values.append(bound)
        else:
            step = _numeric_step(current, bound)
            if step == 0:
                # For non-integer values there is no safe "next representable"
                # review value inside an exclusive bound, so we do not invent
                # one; the caller's allowed_values can still provide it.
                continue
            values.append(bound + step if keyword == "exclusiveMinimum"
                          else bound - step)
    multiple = local_schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and not isinstance(multiple, bool):
        snapped = _snap_to_multiple(current, multiple)
        if snapped is not None:
            values.append(snapped)
    return values


def _numeric_step(current, bound):
    # The smallest representable nudge; for integer-only values this stays 1.
    if isinstance(current, int) and isinstance(bound, int) and not isinstance(
        current, bool,
    ) and not isinstance(bound, bool):
        return 1
    return 0  # floats: the bound itself is the nearest safe review value


def _snap_to_multiple(current, multiple):
    if not isinstance(current, (int, float)) or isinstance(current, bool):
        return None
    factor = current // multiple
    for candidate in (factor * multiple, (factor + 1) * multiple):
        if _utils.equal(candidate % multiple, 0):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Immutable patch application
# --------------------------------------------------------------------------- #
def apply_patch(instance, operations):
    """Return a deep-copied instance with ``operations`` applied.

    The original is never touched.  Only object paths are supported, which
    matches the first-version keyword set.
    """
    result = _json_clone(instance)
    for operation in operations:
        _apply_one(result, operation)
    return result


def _json_clone(value):
    if isinstance(value, Mapping):
        return {key: _json_clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_clone(item) for item in value]
    return value


def _apply_one(root, operation):
    if not operation.path:
        raise ValueError("root-level operations are not supported")
    container = _navigate(root, operation.path[:-1])
    key = operation.path[-1]
    if operation.op == "add":
        container[key] = _json_clone(operation.value)
    elif operation.op == "replace":
        container[key] = _json_clone(operation.value)
    elif operation.op == "delete":
        container.pop(key, None)
    else:
        raise ValueError(f"unsupported op {operation.op!r}")


def _navigate(root, path):
    current = root
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            raise KeyError(key)
        current = current[key]
    return current


# --------------------------------------------------------------------------- #
# Bounded best-first search
# --------------------------------------------------------------------------- #
def _search(instance, actions, validator, cost_model, max_states,
            max_candidates, validator_name, errors_before):
    # Each state is (operations, cost).  We expand cheapest-first so the
    # first fully-valid states we reach are the cheapest ones.  The cost of
    # a candidate is the sum of the operations it contains; tie-breaking is
    # done by a stable order over the operations themselves.
    start = ((), 0.0)
    frontier = [start]
    # The canonical tuple, not a set, is the identity: two operations on the
    # same path with different values are different candidate patches.
    visited = {()}
    solutions = []
    explored = 0
    budget_exhausted = False

    while frontier and len(solutions) < max_candidates:
        frontier.sort(key=lambda state: (state[1], _ops_key(state[0])))
        operations, cost = frontier.pop(0)

        try:
            patched = apply_patch(instance, operations)
        except (KeyError, TypeError, ValueError):
            # A patch we cannot apply is not a candidate; do not expand it.
            continue
        # Delivering a candidate means the original validator clears it.
        remaining = sum(1 for _ in validator.iter_errors(patched))
        explored += 1

        if remaining == 0:
            # Operations are independent (they only touch distinct object
            # keys), so canonicalizing their order makes ties reproducible.
            canonical = tuple(sorted(operations, key=_op_sort_key))
            if all(existing.operations != canonical for existing in solutions):
                solutions.append(
                    RepairCandidate(
                        operations=canonical,
                        cost=cost,
                        errors_before=errors_before,
                        verified=0,
                        validator=validator_name,
                        optimality="optimal",
                    ),
                )
            continue

        if explored >= max_states:
            budget_exhausted = True
            break

        for action in actions:
            if _conflicts(operations, action):
                continue
            new_ops = operations + (action,)
            state_key = tuple(sorted(new_ops, key=_op_sort_key))
            if state_key in visited:
                continue
            visited.add(state_key)
            frontier.append((new_ops, cost + cost_model.price(action.op)))

    solutions.sort(key=lambda candidate: (
        candidate.cost, _ops_key(candidate.operations),
    ))

    # If we stopped only because ``max_candidates`` was reached while the
    # frontier still held cheaper-or-equal work, the reported set is not
    # provably the global optimum.
    truncated = bool(frontier)
    complete = not budget_exhausted and not truncated
    if not complete:
        solutions = [
            _replace_optimality(candidate, "not proven optimal")
            for candidate in solutions
        ]
    return solutions, complete, budget_exhausted, explored


def _replace_optimality(candidate, optimality):
    return RepairCandidate(
        operations=candidate.operations,
        cost=candidate.cost,
        errors_before=candidate.errors_before,
        verified=candidate.verified,
        validator=candidate.validator,
        optimality=optimality,
    )


def _conflicts(operations, action):
    # Two operations on the same path cannot coexist (except that an add and
    # a delete of the same key would be contradictory, which the same-path
    # rule already covers).
    for existing in operations:
        if existing.path == action.path:
            return True
    return False


_OP_RANK = {"add": 0, "replace": 1, "delete": 2}


def _ops_key(operations):
    return tuple(_op_sort_key(operation) for operation in operations)


def _op_sort_key(operation):
    # A total, JSON-shaped key so equal-cost patches have a stable order even
    # when values are not naturally comparable.
    return (
        _OP_RANK.get(operation.op, len(_OP_RANK)),
        _path_key(operation.path),
        _value_key(operation.value),
    )


def _path_key(path):
    return tuple((type(part).__name__, str(part)) for part in path)


def _value_key(value):
    return (type(value).__name__, repr(value))
