from __future__ import annotations

from copy import deepcopy
import unittest

from jsonschema import (
    Draft202012Validator,
    FormatChecker,
    exceptions,
    validators,
)
from jsonschema.incremental import (
    IncrementalValidationSession,
    apply_json_patch,
    JsonPatchError,
)


class CountingFormatChecker(FormatChecker):
    def __init__(self):
        super().__init__(formats=[])
        self.calls = []

        @self.checks("marker")
        def marker(value):
            self.calls.append(value)
            return not str(value).startswith("bad")


SCHEMA = {
    "$schema": Draft202012Validator.META_SCHEMA["$id"],
    "$defs": {
        "active": {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["email"],
                    "properties": {"email": {"format": "marker"}},
                    "additionalProperties": False,
                },
                {"const": "ok"},
            ],
        },
        "node": {
            "$dynamicAnchor": "node",
            "type": ["object", "string"],
            "properties": {
                "value": {"format": "marker"},
                "child": {"$dynamicRef": "#node"},
            },
            "additionalProperties": False,
        },
    },
    "type": "object",
    "required": ["stable", "active", "items", "kind"],
    "properties": {
        "stable": {"format": "marker"},
        "active": {"$ref": "#/$defs/active"},
        "tree": {"$ref": "#/$defs/node"},
        "items": {
            "type": "array",
            "minItems": 2,
            "uniqueItems": True,
            "items": {"format": "marker"},
        },
        "kind": {"enum": ["simple", "dependent"]},
        "dep": {"format": "marker"},
    },
    "dependentSchemas": {
        "needsDep": {
            "required": ["dep"],
            "properties": {"dep": {"const": "ok"}},
        },
    },
}


INITIAL_DOCUMENT = {
    "stable": "stable-value",
    "active": "ok",
    "tree": {
        "value": "tree-value",
        "child": {"value": "leaf-value"},
    },
    "items": ["stable-item", "x"],
    "kind": "simple",
}


def describe_errors(errors):
    described = []
    for error in sorted(errors, key=lambda each: list(each.absolute_path)):
        described.append(
            (
                error.validator,
                list(error.absolute_path),
                list(error.absolute_schema_path),
                error.message,
                repr(error.instance),
                repr(error.cause),
                describe_errors(error.context),
            ),
        )
    return described


class IncrementalValidationSessionTests(unittest.TestCase):
    def test_json_patch_operations_do_not_mutate_inputs(self):
        document = {"a~1b": [1, 2, 3]}
        patch = [
            {"op": "test", "path": "/a~01b/2", "value": 3},
            {"op": "move", "from": "/a~01b/0", "path": "/a~01b/1"},
            {"op": "copy", "from": "/a~01b/0", "path": "/a~01b/-"},
        ]

        result = apply_json_patch(document, patch)

        self.assertEqual(result, {"a~1b": [2, 1, 3, 2]})
        self.assertEqual(document, {"a~1b": [1, 2, 3]})

    def test_json_patch_can_add_to_empty_array_end(self):
        result = apply_json_patch(
            {"items": []},
            [{"op": "add", "path": "/items/-", "value": 1}],
        )
        self.assertEqual(result, {"items": [1]})

    def test_json_patch_move_to_same_location_is_a_noop(self):
        document = {"root": {"value": 1}}
        result = apply_json_patch(
            document,
            [{"op": "move", "from": "", "path": ""}],
        )
        self.assertEqual(result, document)
        self.assertIsNot(result, document)

    def test_json_patches_match_full_iter_errors_and_reuse_formats(self):
        checker = CountingFormatChecker()
        original = deepcopy(INITIAL_DOCUMENT)
        session = IncrementalValidationSession(
            Draft202012Validator(SCHEMA, format_checker=checker),
            original,
        )

        rounds = [
            (
                [{"op": "replace", "path": "/tree/child/value",
                  "value": "bad-leaf"}],
                ["bad-leaf"],
            ),
            (
                [{"op": "replace", "path": "/tree/child/value",
                  "value": "leaf-value"}],
                [],
            ),
            (
                [{"op": "remove", "path": "/items/1"}],
                [],
            ),
            (
                [{"op": "add", "path": "/items/-", "value": "x"}],
                [],
            ),
            (
                [{"op": "add", "path": "/needsDep", "value": True}],
                [],
            ),
            (
                [{"op": "add", "path": "/dep", "value": "bad-dep"}],
                ["bad-dep"],
            ),
            (
                [{"op": "replace", "path": "/dep", "value": "ok"}],
                ["ok"],
            ),
            (
                [
                    {"op": "remove", "path": "/dep"},
                    {"op": "remove", "path": "/needsDep"},
                ],
                [],
            ),
            (
                [{"op": "replace", "path": "/items/1",
                  "value": "stable-item"}],
                [],
            ),
            (
                [{"op": "replace", "path": "/items/1", "value": "x"}],
                [],
            ),
            (
                [{"op": "move", "from": "/stable", "path": "/dep"}],
                [],
            ),
            (
                [{"op": "move", "from": "/dep", "path": "/stable"}],
                [],
            ),
        ]

        self.assertEqual(original, INITIAL_DOCUMENT)
        initial_call_count = len(checker.calls)

        for patch, expected_format_calls in rounds:
            patch_before = deepcopy(patch)
            calls_before = len(checker.calls)
            errors = session.apply_patch(patch)

            baseline = Draft202012Validator(
                SCHEMA,
                format_checker=CountingFormatChecker(),
            ).iter_errors(session.document)
            self.assertEqual(
                describe_errors(errors),
                describe_errors(baseline),
            )
            self.assertEqual(
                checker.calls[calls_before:],
                expected_format_calls,
            )
            self.assertEqual(patch, patch_before)

        self.assertNotIn("stable-value", checker.calls[initial_call_count:])
        self.assertNotIn("tree-value", checker.calls[initial_call_count:])
        self.assertNotIn("stable-item", checker.calls[initial_call_count:])

    def test_documents_are_not_mutated(self):
        caller_document = deepcopy(INITIAL_DOCUMENT)
        session = IncrementalValidationSession.for_schema(
            SCHEMA,
            caller_document,
            format_checker=CountingFormatChecker(),
        )

        caller_document["stable"] = "mutated-by-caller"
        self.assertEqual(session.document, INITIAL_DOCUMENT)

        returned = session.document
        returned["stable"] = "mutated-return-value"
        self.assertEqual(session.document, INITIAL_DOCUMENT)

    def test_invalid_patch_is_atomic_and_preserves_state(self):
        checker = CountingFormatChecker()
        session = IncrementalValidationSession(
            Draft202012Validator(SCHEMA, format_checker=checker),
            INITIAL_DOCUMENT,
        )
        before = session.document
        errors_before = describe_errors(session.errors)
        calls_before = len(checker.calls)
        patch = [
            {"op": "add", "path": "/temporary", "value": "x"},
            {"op": "remove", "path": "/missing"},
        ]

        with self.assertRaises(JsonPatchError):
            session.apply_patch(patch)

        self.assertEqual(session.document, before)
        self.assertEqual(describe_errors(session.errors), errors_before)
        self.assertEqual(len(checker.calls), calls_before)

    def test_custom_keyword_validator_falls_back_to_full_validation(self):
        def custom_keyword(validator, value, instance, schema):
            if value and isinstance(instance, dict) and instance.get(
                "active",
            ) != "custom":
                yield exceptions.ValidationError("custom keyword failed")

        CustomValidator = validators.extend(
            Draft202012Validator,
            validators={"x-custom": custom_keyword},
        )
        checker = CountingFormatChecker()
        schema = dict(SCHEMA)
        schema["x-custom"] = True
        session = IncrementalValidationSession(
            CustomValidator(schema, format_checker=checker),
            INITIAL_DOCUMENT,
        )

        calls_before = len(checker.calls)
        errors = session.apply_patch(
            [{"op": "replace", "path": "/tree/value", "value": "changed"}],
        )

        self.assertEqual(checker.calls[calls_before:], [
            "stable-value",
            "changed",
            "leaf-value",
            "stable-item",
            "x",
        ])
        baseline = CustomValidator(
            schema,
            format_checker=CountingFormatChecker(),
        ).iter_errors(session.document)
        self.assertEqual(describe_errors(errors), describe_errors(baseline))

    def test_only_draft_2020_12_is_supported(self):
        with self.assertRaises(TypeError):
            IncrementalValidationSession(
                validators.Draft7Validator({}),
                INITIAL_DOCUMENT,
            )

    def test_cached_format_error_preserves_cause(self):
        class RaisingFormatChecker(FormatChecker):
            def __init__(self):
                super().__init__(formats=[])
                self.calls = 0

                @self.checks("boom", raises=ValueError)
                def boom(value):
                    self.calls += 1
                    raise ValueError("detonation")

        schema = {
            "$schema": Draft202012Validator.META_SCHEMA["$id"],
            "properties": {
                "boom": {"format": "boom"},
                "pow": {"format": "boom"},
            },
        }
        checker = RaisingFormatChecker()
        session = IncrementalValidationSession(
            Draft202012Validator(schema, format_checker=checker),
            {"boom": "x"},
        )

        first = session.apply_patch(
            [{"op": "move", "from": "/boom", "path": "/pow"}],
        )
        second = session.apply_patch(
            [{"op": "move", "from": "/pow", "path": "/boom"}],
        )

        self.assertEqual(checker.calls, 1)
        self.assertEqual(
            [list(each.absolute_path) for each in first],
            [["pow"]],
        )
        self.assertEqual(
            [list(each.absolute_path) for each in second],
            [["boom"]],
        )
        self.assertEqual(repr(first[0].cause), repr(second[0].cause))


if __name__ == "__main__":
    unittest.main()
