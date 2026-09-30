from collections import deque
from copy import deepcopy
from unittest import TestCase

from jsonschema import (
    Draft202012Validator,
    FormatChecker,
)
from jsonschema import validators
from jsonschema._incremental import (
    IncrementalValidationSession,
    JSONPatchError,
    apply_json_patch,
)


def error_data(error):
    return (
        list(error.absolute_path),
        list(error.absolute_schema_path),
        error.validator,
        error.message,
        sorted(
            (
                list(child.absolute_path),
                list(child.absolute_schema_path),
                child.validator,
                child.message,
            )
            for child in error.context
        ),
    )


def errors_for(validator, instance):
    return sorted(
        (error_data(error) for error in validator.iter_errors(instance)),
        key=repr,
    )


def incremental_errors(errors):
    return sorted((error_data(error) for error in errors), key=repr)


class CountingFormatChecker(FormatChecker):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.checks("x-custom")(self.check_custom)

    def check_custom(self, value):
        self.calls += 1
        return isinstance(value, str) and value == "ok"


class IncrementalValidationTest(TestCase):
    def assert_matches_full_validation(self, session, validator):
        self.assertEqual(
            incremental_errors(session.iter_errors()),
            errors_for(validator, session.document),
        )

    def test_initial_document_is_not_mutated_and_returns_a_copy(self):
        document = {"value": [1, 2]}
        session = IncrementalValidationSession(
            Draft202012Validator({}),
            document,
        )
        document["value"].append(3)

        self.assertEqual(session.document, {"value": [1, 2]})
        session.document["value"].append(4)
        self.assertEqual(session.document, {"value": [1, 2]})

    def test_add_remove_replace_move_and_copy_match_full_validation(self):
        schema = {
            "type": "object",
            "required": ["target"],
            "additionalProperties": False,
            "properties": {
                "target": {"type": "integer", "minimum": 3},
                "moved": {"type": "integer", "minimum": 3},
                "copied": {"type": "integer", "minimum": 3},
            },
        }
        validator = Draft202012Validator(schema)
        document = {"target": 4, "moved": 5}
        session = IncrementalValidationSession(validator, document)
        rounds = [
            [{"op": "replace", "path": "/target", "value": 1}],
            [{"op": "remove", "path": "/target"}],
            [{"op": "add", "path": "/target", "value": 2}],
            [{"op": "replace", "path": "/target", "value": 6}],
            [{"op": "move", "from": "/moved", "path": "/target"}],
            [{"op": "add", "path": "/target", "value": 7}],
        ]
        expected = deepcopy(document)

        self.assert_matches_full_validation(session, validator)
        for patch in rounds:
            expected = apply_json_patch(expected, patch)
            errors = session.apply(patch)
            self.assertEqual(session.document, expected)
            self.assertEqual(
                incremental_errors(errors),
                errors_for(validator, expected),
            )
            self.assert_matches_full_validation(session, validator)

        errors = session.apply([
            {"op": "copy", "from": "/target", "path": "/copied"},
        ])
        self.assertEqual(session.document["copied"], 7)
        self.assertEqual(
            incremental_errors(errors),
            errors_for(validator, session.document),
        )

    def test_parent_and_sibling_constraints_match_full_validation(self):
        schema = {
            "type": "object",
            "required": ["required"],
            "dependentRequired": {"trigger": ["sibling"]},
            "dependentSchemas": {
                "schemaTrigger": {
                    "required": ["schemaSibling"],
                },
            },
            "properties": {
                "trigger": {"type": "boolean"},
                "schemaTrigger": {"type": "boolean"},
                "required": {"type": "string"},
                "sibling": {"type": "string"},
                "schemaSibling": {"type": "string"},
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "uniqueItems": True,
                },
            },
        }
        validator = Draft202012Validator(schema)
        session = IncrementalValidationSession(validator, {
            "items": ["a", "a"],
        })
        rounds = [
            [{"op": "add", "path": "/trigger", "value": True}],
            [{"op": "add", "path": "/schemaTrigger", "value": True}],
            [{"op": "remove", "path": "/items/0"}],
            [
                {"op": "add", "path": "/sibling", "value": "here"},
                {"op": "add", "path": "/schemaSibling", "value": "here"},
            ],
            [{"op": "add", "path": "/required", "value": "here"}],
            [{"op": "replace", "path": "/items/0", "value": "new"}],
        ]

        expected = session.document
        for patch in rounds:
            expected = apply_json_patch(expected, patch)
            self.assertEqual(
                incremental_errors(session.apply(patch)),
                errors_for(validator, expected),
            )

    def test_independent_subtree_change_reuses_format_results(self):
        schema = {
            "type": "object",
            "required": ["left", "right"],
            "properties": {
                "left": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "string", "format": "x-custom"},
                        "b": {"type": "string", "format": "x-custom"},
                    },
                },
                "right": {
                    "type": "object",
                    "properties": {
                        "c": {"type": "string", "format": "x-custom"},
                        "d": {"type": "string", "format": "x-custom"},
                    },
                },
            },
        }
        format_checker = CountingFormatChecker()
        validator = Draft202012Validator(
            schema,
            format_checker=format_checker,
        )
        document = {
            "left": {"a": "ok", "b": "ok"},
            "right": {"c": "ok", "d": "ok"},
        }
        session = IncrementalValidationSession(validator, document)
        initial_calls = format_checker.calls
        self.assertEqual(initial_calls, 4)

        format_checker.calls = 0
        errors = session.apply([
            {"op": "replace", "path": "/left/a", "value": "bad"},
        ])
        self.assertEqual(len(errors), 1)
        self.assertEqual(list(errors[0].absolute_path), ["left", "a"])
        self.assertEqual(format_checker.calls, 1)
        self.assertEqual(
            incremental_errors(errors),
            errors_for(validator, session.document),
        )

        format_checker.calls = 0
        errors = session.apply([
            {"op": "replace", "path": "/right/c", "value": "bad"},
        ])
        self.assertEqual(format_checker.calls, 1)
        self.assertEqual(len(errors), 2)
        self.assertEqual(
            [list(error.absolute_path) for error in errors],
            [["left", "a"], ["right", "c"]],
        )
        self.assertEqual(
            incremental_errors(errors),
            errors_for(validator, session.document),
        )

    def test_static_reference_result_is_reused(self):
        schema = {
            "type": "object",
            "properties": {
                "left": {"$ref": "#/$defs/item"},
                "right": {"$ref": "#/$defs/item"},
            },
            "$defs": {
                "item": {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "format": "x-custom",
                        },
                    },
                },
            },
        }
        format_checker = CountingFormatChecker()
        validator = Draft202012Validator(
            schema,
            format_checker=format_checker,
        )
        document = {
            "left": {"value": "ok"},
            "right": {"value": "ok"},
        }
        session = IncrementalValidationSession(validator, document)
        format_checker.calls = 0

        session.apply([
            {"op": "replace", "path": "/right/value", "value": "still-ok"},
        ])
        self.assertEqual(format_checker.calls, 1)

    def test_dynamic_reference_matches_full_validation(self):
        schema = {
            "$id": "https://example.test/root",
            "$ref": "list",
            "$defs": {
                "item": {
                    "$dynamicAnchor": "item",
                    "type": "string",
                },
                "list": {
                    "$id": "list",
                    "type": "array",
                    "items": {"$dynamicRef": "#item"},
                    "$defs": {
                        "item": {
                            "$dynamicAnchor": "item",
                        },
                    },
                },
            },
        }
        validator = Draft202012Validator(schema)
        session = IncrementalValidationSession(validator, ["ok"])
        expected = ["ok"]

        for patch in (
            [{"op": "add", "path": "/-", "value": 2}],
            [{"op": "replace", "path": "/0", "value": 3}],
            [{"op": "remove", "path": "/0"}],
        ):
            expected = apply_json_patch(expected, patch)
            self.assertEqual(
                incremental_errors(session.apply(patch)),
                errors_for(validator, expected),
            )

    def test_invalid_patch_leaves_session_state_unchanged(self):
        validator = Draft202012Validator({"type": "object"})
        session = IncrementalValidationSession(validator, {"a": 1})
        before = deepcopy(session.document)

        with self.assertRaises(JSONPatchError):
            session.apply([{"op": "remove", "path": "/missing"}])

        self.assertEqual(session.document, before)
        self.assert_matches_full_validation(session, validator)

    def test_unknown_keyword_falls_back_to_full_validation(self):
        schema = {"type": "object", "x-custom": True}
        validator = Draft202012Validator(schema)
        session = IncrementalValidationSession(validator, {})
        self.assertTrue(session._engine._fallback)

        errors = session.apply([{"op": "add", "path": "/a", "value": 1}])
        self.assertEqual(errors, [])

    def test_only_draft_2020_12_is_supported(self):
        other = validators.create(
            meta_schema={"$id": "urn:other-draft"},
            validators=Draft202012Validator.VALIDATORS,
        )
        with self.assertRaises(TypeError):
            IncrementalValidationSession(other({}), {})

    def test_returned_errors_have_expected_paths_and_deques(self):
        schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
        }
        session = IncrementalValidationSession(
            Draft202012Validator(schema),
            {"value": "bad"},
        )
        error, = session.iter_errors()
        self.assertEqual(error.path, deque(["value"]))
        self.assertEqual(
            error.schema_path,
            deque(["properties", "value", "type"]),
        )

    def test_custom_keyword_in_unevaluated_branch_falls_back(self):
        schema = {
            "allOf": [
                {"const": "unreachable"},
                {"x-custom": True},
            ],
        }
        validator = Draft202012Validator(schema)
        session = IncrementalValidationSession(validator, "reachable")
        self.assertTrue(session._engine._fallback)
        self.assertEqual(
            incremental_errors(session.iter_errors()),
            errors_for(validator, session.document),
        )

    def test_invalid_patch_also_leaves_errors_unchanged(self):
        validator = Draft202012Validator({"required": ["a"]})
        session = IncrementalValidationSession(validator, {})
        before = incremental_errors(session.iter_errors())

        with self.assertRaises(JSONPatchError):
            session.apply([{"op": "add", "path": "not/a/pointer", "value": 1}])

        self.assertEqual(
            incremental_errors(session.iter_errors()),
            before,
        )

    def test_patch_array_index_and_pointer_escaping_rules(self):
        validator = Draft202012Validator({"type": "array"})
        session = IncrementalValidationSession(validator, [1, 2])

        self.assertEqual(
            session.apply([{"op": "add", "path": "/-", "value": 3}]),
            [],
        )
        self.assertEqual(session.document, [1, 2, 3])

        with self.assertRaises(JSONPatchError):
            session.apply([{"op": "remove", "path": "/01"}])
        with self.assertRaises(JSONPatchError):
            session.apply([{"op": "test", "path": "/3", "value": 1}])

        session.apply([
            {"op": "add", "path": "/3", "value": {"a/b": 1}},
        ])
        self.assertEqual(session.document, [1, 2, 3, {"a/b": 1}])

    def test_move_to_child_path_is_invalid(self):
        validator = Draft202012Validator({})
        session = IncrementalValidationSession(
            validator,
            {"a": {"b": 1}},
        )

        session.apply([{"op": "move", "from": "/a", "path": "/a"}])
        self.assertEqual(session.document, {"a": {"b": 1}})
        with self.assertRaises(JSONPatchError):
            session.apply([
                {"op": "move", "from": "/a", "path": "/a/b"},
            ])
        self.assertEqual(session.document, {"a": {"b": 1}})

    def test_patch_shares_unrelated_subtrees(self):
        validator = Draft202012Validator({})
        document = {
            "changed": {"value": 1},
            "untouched": {"value": 2},
            "items": [{"value": 3}, {"value": 4}],
        }
        session = IncrementalValidationSession(validator, document)
        untouched = session._document["untouched"]
        first_item = session._document["items"][0]

        session.apply([
            {"op": "replace", "path": "/changed/value", "value": 5},
        ])

        self.assertIs(session._document["untouched"], untouched)
        self.assertIs(session._document["items"][0], first_item)

    def test_boolean_and_integer_cache_keys_do_not_collide(self):
        schema = {
            "type": "object",
            "additionalProperties": {"type": "boolean"},
        }
        validator = Draft202012Validator(schema)
        session = IncrementalValidationSession(validator, {
            "boolean": True,
            "integer": 0,
        })
        self.assertEqual(
            incremental_errors(session.iter_errors()),
            errors_for(validator, session.document),
        )

    def test_array_insertion_keeps_existing_item_results(self):
        format_checker = CountingFormatChecker()
        schema = {
            "type": "array",
            "items": {"type": "string", "format": "x-custom"},
        }
        validator = Draft202012Validator(
            schema,
            format_checker=format_checker,
        )
        session = IncrementalValidationSession(validator, ["ok", "ok"])
        format_checker.calls = 0

        session.apply([
            {"op": "add", "path": "/0", "value": "ok"},
        ])

        self.assertEqual(format_checker.calls, 0)
        self.assertEqual(
            incremental_errors(session.iter_errors()),
            errors_for(validator, session.document),
        )

    def test_root_replace_and_move_are_applied_without_mutation(self):
        validator = Draft202012Validator({})
        original = {"a": 1}
        session = IncrementalValidationSession(validator, original)

        session.apply([{"op": "replace", "path": "", "value": [1, 2]}])
        self.assertEqual(session.document, [1, 2])
        self.assertEqual(original, {"a": 1})

        session.apply([
            {"op": "move", "from": "/0", "path": "/1"},
        ])
        self.assertEqual(session.document, [2, 1])

    def test_nested_array_append_uses_end_index(self):
        validator = Draft202012Validator({})
        session = IncrementalValidationSession(validator, [[1], [2]])

        session.apply([{"op": "add", "path": "/1/-", "value": 3}])

        self.assertEqual(session.document, [[1], [2, 3]])
