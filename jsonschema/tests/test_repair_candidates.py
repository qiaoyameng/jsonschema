from copy import deepcopy
from itertools import combinations
from unittest import TestCase

from jsonschema import Draft202012Validator
from jsonschema.repair_candidates import (
    CostModel,
    PatchOperation,
    SUPPORTED_KEYWORDS,
    apply_patch,
    repair_candidates,
)


def brute_force_minimum(schema, instance, value_pool, cost_model=None):
    """Enumerate every supported patch combination and validate each.

    This deliberately shares no logic with the implementation under test:
    it independently builds every non-empty subset of the atomic actions and
    asks the original validator whether the result validates.  The value
    universe is schema constants and caller-supplied values only, mirroring
    the no-invention rule.
    """
    if cost_model is None:
        cost_model = CostModel()
    validator = Draft202012Validator(schema)
    merged = _merge_pools(value_pool, _schema_value_pool(schema))
    atoms = []
    for path, values in merged.items():
        present = _get_path(instance, path) is _MISSING
        kind = "add" if present else "replace"
        for value in values:
            atoms.append(PatchOperation(kind, tuple(path), value))
    for extra in _extras(instance, schema):
        atoms.append(PatchOperation("delete", tuple(extra)))

    best = []
    minimum_cost = None
    for size in range(1, len(atoms) + 1):
        for subset in combinations(atoms, size):
            if _same_path_conflict(subset):
                continue
            try:
                patched = _apply_independently(deepcopy(instance), subset)
            except (KeyError, TypeError):
                continue
            if validator.is_valid(patched):
                cost = sum(cost_model.price(op.op) for op in subset)
                if minimum_cost is None or cost < minimum_cost:
                    minimum_cost = cost
                    best = [subset]
                elif cost == minimum_cost:
                    best.append(subset)
    return minimum_cost, best


_MISSING = object()


def _schema_value_pool(schema):
    """Collect the constant values a schema advertises, per instance path."""
    pool = {}

    def visit(node, path):
        if not isinstance(node, dict):
            return
        if "const" in node:
            pool.setdefault(path, []).append(node["const"])
        for value in node.get("enum", []):
            pool.setdefault(path, []).append(value)
        for bound in ("minimum", "maximum"):
            if isinstance(node.get(bound), (int, float)) and not isinstance(
                node.get(bound), bool,
            ):
                pool.setdefault(path, []).append(node[bound])
        for name, child in node.get("properties", {}).items():
            visit(child, path + (name,))
        for keyword in ("allOf", "anyOf"):
            for child in node.get(keyword, []):
                visit(child, path)

    visit(schema, ())
    return pool


def _merge_pools(caller, schema_pool):
    merged = {
        tuple(path): list(values) for path, values in schema_pool.items()
    }
    for path, values in caller.items():
        key = tuple(path)
        merged.setdefault(key, [])
        for value in values:
            if value not in merged[key]:
                merged[key].append(value)
    return merged


def _get_path(instance, path):
    current = instance
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _extras(instance, schema):
    from jsonschema._utils import find_additional_properties

    if not isinstance(instance, dict):
        return []
    return [[key] for key in find_additional_properties(instance, schema)]


def _same_path_conflict(operations):
    paths = [op.path for op in operations]
    return len(paths) != len(set(paths))


def _apply_independently(instance, operations):
    """A deliberately separate patch applier used only by the oracle."""
    for operation in operations:
        current = instance
        for part in operation.path[:-1]:
            current = current[part]
        key = operation.path[-1]
        if operation.op == "add":
            current[key] = deepcopy(operation.value)
        elif operation.op == "replace":
            current[key] = deepcopy(operation.value)
        elif operation.op == "delete":
            del current[key]
        else:
            raise AssertionError(f"unexpected op {operation.op!r}")
    return instance


class ValueSourcingTests(TestCase):
    def test_values_come_from_allowed_table(self):
        schema = {
            "type": "object",
            "properties": {"k": {"type": "integer"}},
            "required": ["k"],
        }
        result = repair_candidates({}, schema, allowed_values={("k",): [5]})
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].patch, [
            {"op": "add", "path": ["k"], "value": 5},
        ])

    def test_enum_and_const_are_schema_constant_sources(self):
        schema = {
            "type": "object",
            "properties": {"e": {"enum": ["x"]}, "c": {"const": 7}},
            "required": ["e", "c"],
        }
        result = repair_candidates({"e": "nope", "c": 1}, schema)
        values = {
            (op["path"][0], op["value"])
            for candidate in result
            for op in candidate.patch
        }
        self.assertIn(("e", "x"), values)
        self.assertIn(("c", 7), values)

    def test_no_values_means_no_invented_repair(self):
        schema = {"type": "object", "required": ["k"]}
        result = repair_candidates({}, schema)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(len(result), 0)

    def test_numeric_bound_is_a_schema_constant_source(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 3}},
        }
        result = repair_candidates({"n": 1}, schema)
        values = {op["value"] for op in result[0].patch}
        self.assertIn(3, values)

    def test_missing_required_numeric_uses_schema_bound(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 0}},
            "required": ["n"],
        }
        result = repair_candidates({}, schema)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result[0].patch, [
            {"op": "add", "path": ["n"], "value": 0},
        ])

    def test_exclusive_integer_bound_uses_next_value(self):
        schema = {"properties": {"n": {"exclusiveMinimum": 0}}}
        result = repair_candidates({"n": 0}, schema)
        values = {op["value"] for op in result[0].patch}
        self.assertIn(1, values)
        self.assertNotIn(0, values)

    def test_multiple_of_snaps_to_a_multiple(self):
        schema = {"properties": {"n": {"multipleOf": 5}}}
        result = repair_candidates({"n": 12}, schema)
        values = {op["value"] for op in result[0].patch}
        self.assertTrue(any(value % 5 == 0 for value in values))

    def test_distinct_values_at_one_path_are_all_explored(self):
        # Two permitted values for the same key must not collapse into one
        # candidate; each is a separate patch.
        schema = {"properties": {"n": {"minimum": 10}}}
        result = repair_candidates(
            {"n": 1}, schema, allowed_values={("n",): [11, 20]},
        )
        values = {op["value"] for c in result for op in c.patch}
        self.assertEqual(values, {10, 11, 20})


class SafetyTests(TestCase):
    def test_instance_is_never_mutated(self):
        schema = {
            "type": "object",
            "properties": {"n": {"minimum": 0}},
            "additionalProperties": False,
        }
        instance = {"n": -1, "extra": 1}
        snapshot = deepcopy(instance)
        result = repair_candidates(instance, schema)
        self.assertEqual(instance, snapshot)
        self.assertTrue(result.input_unchanged)
        for candidate in result:
            self.assertNotEqual(candidate.patch, [])
        self.assertEqual(instance, snapshot)

    def test_patch_is_not_applied_to_input(self):
        schema = {"properties": {"n": {"minimum": 0}}}
        instance = {"n": -1}
        result = repair_candidates(instance, schema)
        self.assertTrue(result[0].valid)
        self.assertEqual(instance, {"n": -1})

    def test_caller_validator_is_reused(self):
        schema = {"properties": {"n": {"minimum": 0}}}
        supplied = Draft202012Validator(schema)
        result = repair_candidates({"n": -1}, schema, validator=supplied)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result[0].validator, "Draft202012Validator")
        self.assertEqual(result[0].verified, 0)

    def test_every_delivered_candidate_revalidates(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"enum": ["a", "b"]},
                "age": {"type": "integer", "minimum": 0},
                "kind": {"const": "x"},
            },
            "required": ["name", "age", "kind"],
            "additionalProperties": False,
        }
        instance = {"age": -1, "kind": "y", "extra": 2}
        result = repair_candidates(
            instance, schema, allowed_values={("name",): ["a", "b"]},
        )
        validator = Draft202012Validator(schema)
        self.assertTrue(result)
        for candidate in result:
            self.assertEqual(candidate.verified, 0)
            self.assertTrue(candidate.valid)
            patched = apply_patch(instance, candidate.operations)
            self.assertTrue(validator.is_valid(patched))

    def test_fewer_errors_is_not_treated_as_success(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        }
        # Adding "a" removes one of two required errors, but "b" is
        # undeclared, so no instance-only patch can validate.
        result = repair_candidates(
            {}, schema, allowed_values={("a",): [1], ("b",): [2]},
        )
        self.assertEqual(result.status, "unknown")
        self.assertEqual(len(result), 0)

    def test_partial_fix_with_extra_key_is_not_delivered(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        }
        instance = {"x": 1}
        result = repair_candidates(
            instance, schema, allowed_values={("a",): [7]},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0].patch,
            [
                {"op": "add", "path": ["a"], "value": 7},
                {"op": "delete", "path": ["x"]},
            ],
        )
        validator = Draft202012Validator(schema)
        add_only = apply_patch(
            instance, [PatchOperation("add", ("a",), 7)],
        )
        delete_only = apply_patch(
            instance, [PatchOperation("delete", ("x",), None)],
        )
        self.assertGreater(len(list(validator.iter_errors(add_only))), 0)
        self.assertGreater(len(list(validator.iter_errors(delete_only))), 0)


class StatusTests(TestCase):
    def test_already_valid_is_empty_and_optimal(self):
        schema = {"type": "object"}
        result = repair_candidates({"a": 1}, schema)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.optimal)
        self.assertEqual(len(result), 0)

    def test_unsupported_keyword_returns_unknown(self):
        schema = {
            "type": "object",
            "properties": {"s": {"type": "string", "minLength": 3}},
        }
        result = repair_candidates({"s": "x"}, schema)
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.optimal)
        self.assertIn("minLength", result.unsupported_keywords)
        self.assertEqual(len(result), 0)

    def test_annotations_do_not_block_repair(self):
        schema = {
            "title": "A record",
            "description": "annotated",
            "type": "object",
            "properties": {"n": {"minimum": 0, "default": 0}},
        }
        result = repair_candidates({"n": -1}, schema)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result[0].patch, [
            {"op": "replace", "path": ["n"], "value": 0},
        ])

    def test_reference_is_out_of_scope(self):
        schema = {
            "$defs": {"num": {"minimum": 0}},
            "type": "object",
            "properties": {"n": {"$ref": "#/$defs/num"}},
        }
        result = repair_candidates({"n": -1}, schema)
        self.assertEqual(result.status, "unknown")
        self.assertIn("$ref", result.unsupported_keywords)

    def test_supported_keyword_set_is_explicit(self):
        self.assertEqual(
            SUPPORTED_KEYWORDS,
            frozenset({
                "properties", "required", "additionalProperties",
                "type", "enum", "const",
                "minimum", "maximum", "exclusiveMinimum",
                "exclusiveMaximum", "multipleOf",
                "allOf", "anyOf",
            }),
        )

    def test_budget_exhaustion_is_marked(self):
        schema = {
            "type": "object",
            "properties": {f"k{i}": {"type": "integer"} for i in range(4)},
            "required": [f"k{i}" for i in range(4)],
        }
        allowed = {(f"k{i}",): [i] for i in range(4)}
        result = repair_candidates({}, schema, allowed_values=allowed,
                                   max_states=1)
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(result.optimal)

    def test_candidate_cap_is_marked_not_proven(self):
        schema = {"properties": {"e": {"enum": ["a", "b", "c", "d"]}}}
        result = repair_candidates({"e": "z"}, schema, max_candidates=2)
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(result.optimal)
        self.assertTrue(
            all(c.optimality != "optimal" for c in result),
        )

    def test_unknown_when_no_repair_validates(self):
        schema = {"type": "object", "required": ["k"]}
        result = repair_candidates({}, schema)
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.optimal)


class MinimumCostTests(TestCase):
    """Independent enumeration checks the search's claims."""

    def assertMatchesOracle(self, schema, instance, pool, cost_model=None):
        result = repair_candidates(
            instance,
            schema,
            allowed_values=pool,
            cost_model=cost_model,
        )
        minimum, expected = brute_force_minimum(
            schema, instance, pool, cost_model,
        )
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.optimal)
        self.assertIsNotNone(minimum)

        # Every reported candidate costs at least the true minimum, and at
        # least one candidate attains it.
        self.assertEqual(
            min(result.candidates, key=lambda candidate: candidate.cost).cost,
            minimum,
        )
        self.assertTrue(
            all(c.cost >= minimum for c in result.candidates),
        )
        # The exhaustive oracle and the search agree on the optimal patch set.
        reported = {
            _ops_identity(c.operations) for c in result.candidates
            if c.cost == minimum
        }
        expected_set = {_ops_identity(subset) for subset in expected}
        self.assertEqual(reported, expected_set)
        # Candidates are ordered by cost and then canonically.
        keys = [
            (candidate.cost, _ops_identity(candidate.operations))
            for candidate in result.candidates
        ]
        self.assertEqual(keys, sorted(keys))

    def test_enum_tie_order(self):
        schema = {"properties": {"e": {"enum": ["z", "a", "m"]}}}
        self.assertMatchesOracle(schema, {"e": 9}, {})

    def test_type_and_const(self):
        schema = {
            "type": "object",
            "properties": {"flag": {"type": "boolean"}, "kind": {"const": 2}},
            "required": ["flag", "kind"],
        }
        instance = {"flag": "yes", "kind": 1}
        self.assertMatchesOracle(
            schema, instance, {("flag",): [True]},
        )

    def test_numeric_bounds(self):
        schema = {
            "properties": {"n": {"minimum": 0, "maximum": 100}},
        }
        self.assertMatchesOracle(schema, {"n": -3}, {})

    def test_numeric_with_caller_values(self):
        schema = {"properties": {"n": {"minimum": 10}}}
        self.assertMatchesOracle(schema, {"n": 1}, {("n",): [11, 50]})

    def test_additional_properties_delete(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "additionalProperties": False,
        }
        self.assertMatchesOracle(schema, {"a": 1, "x": 2, "y": 3}, {})

    def test_required_that_triggers_additional_properties(self):
        # ``a`` is missing and declared; ``x`` is an unexpected extra.  A
        # repair must both add ``a`` and delete ``x``; each alone is invalid.
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        }
        self.assertMatchesOracle(
            schema, {"x": 1}, {("a",): [7]},
        )

    def test_cost_model_changes_ranking(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a"],
            "additionalProperties": False,
        }
        cost_model = CostModel(add=5.0, delete=1.0, replace=1.0)
        result = repair_candidates(
            {"x": 1}, schema, allowed_values={("a",): [9]},
            cost_model=cost_model,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result[0].cost, 6.0)

    def test_all_of_combination(self):
        schema = {
            "allOf": [
                {"required": ["a"]},
                {"properties": {"a": {"minimum": 0}}},
            ],
        }
        self.assertMatchesOracle(schema, {}, {("a",): [0, 5]})


def _ops_identity(operations):
    return tuple(
        (op.op, tuple(op.path), _value_identity(op.value))
        for op in sorted(operations, key=lambda o: (o.path, o.op))
    )


def _value_identity(value):
    if isinstance(value, dict):
        return ("dict", tuple(
            sorted((k, _value_identity(v)) for k, v in value.items()),
        ))
    if isinstance(value, list):
        return ("list", tuple(_value_identity(v) for v in value))
    return (type(value).__name__, value)


class ApplyPatchTests(TestCase):
    def test_returns_new_value_and_leaves_input_alone(self):
        instance = {"a": {"b": 1}, "x": 2}
        snapshot = deepcopy(instance)
        result = apply_patch(instance, [
            PatchOperation("replace", ("a", "b"), 9),
            PatchOperation("add", ("a", "c"), 3),
            PatchOperation("delete", ("x",), None),
        ])
        self.assertEqual(result, {"a": {"b": 9, "c": 3}})
        self.assertEqual(instance, snapshot)

    def test_added_values_are_copied_not_aliased(self):
        instance = {}
        value = {"nested": [1]}
        result = apply_patch(instance, [
            PatchOperation("add", ("k",), value),
        ])
        value["nested"].append(2)
        self.assertEqual(result["k"], {"nested": [1]})

    def test_root_level_operation_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_patch({"a": 1}, [PatchOperation("replace", (), 1)])

    def test_missing_intermediate_path_is_rejected(self):
        with self.assertRaises(KeyError):
            apply_patch(
                {"a": {}},
                [PatchOperation("replace", ("a", "z", "q"), 1)],
            )
