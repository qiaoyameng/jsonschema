"""
Tests for `Validator.evaluate` and its annotation collection.
"""
from unittest import TestCase

from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from jsonschema import (
    Annotation,
    Draft202012Validator,
    EvaluationResult,
    exceptions,
)


def resource(contents):
    return Resource.from_contents(
        contents,
        default_specification=DRAFT202012,
    )


def registry_of(**resources):
    return Registry().with_resources([
        (uri, resource(contents)) for uri, contents in resources.items()
    ])


def by_keyword(result):
    return {
        (annotation.keyword, annotation.instance_location): annotation
        for annotation in result.annotations
    }


class TestEvaluate(TestCase):
    def test_invalid_result_carries_errors_and_no_annotations(self):
        validator = Draft202012Validator({
            "title": "A number",
            "type": "integer",
        })
        result = validator.evaluate("nope")

        self.assertFalse(result.valid)
        self.assertEqual(
            [error.message for error in result.errors],
            [error.message for error in validator.iter_errors("nope")],
        )
        self.assertEqual(result.annotations, ())

    def test_valid_result_collects_annotations_with_locations(self):
        validator = Draft202012Validator({
            "title": "A thing",
            "readOnly": True,
            "properties": {
                "name": {
                    "description": "The name",
                    "default": "anonymous",
                    "type": "string",
                },
            },
        })
        result = validator.evaluate({"name": "foo"})

        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())
        self.assertEqual(
            set(result.annotations),
            {
                Annotation(
                    keyword="title",
                    value="A thing",
                    instance_location=(),
                    schema_location="#/title",
                ),
                Annotation(
                    keyword="readOnly",
                    value=True,
                    instance_location=(),
                    schema_location="#/readOnly",
                ),
                Annotation(
                    keyword="description",
                    value="The name",
                    instance_location=("name",),
                    schema_location="#/properties/name/description",
                ),
                Annotation(
                    keyword="default",
                    value="anonymous",
                    instance_location=("name",),
                    schema_location="#/properties/name/default",
                ),
            },
        )

    def test_default_is_only_an_annotation(self):
        instance = {"name": "foo"}
        validator = Draft202012Validator({
            "properties": {"other": {"default": 12}},
        })
        result = validator.evaluate(instance)

        self.assertTrue(result.valid)
        self.assertEqual(result.annotations, ())
        self.assertEqual(instance, {"name": "foo"})

    def test_evaluate_does_not_change_validation_behavior(self):
        schema = {
            "properties": {"a": {"type": "integer", "title": "A"}},
            "anyOf": [{"required": ["a"]}, {"required": ["b"]}],
        }
        validator = Draft202012Validator(schema)
        instance = {"a": "not an integer"}

        before = [error.message for error in validator.iter_errors(instance)]
        valid_before = validator.is_valid(instance)

        result = validator.evaluate(instance)

        after = [error.message for error in validator.iter_errors(instance)]
        self.assertEqual(before, after)
        self.assertEqual(valid_before, validator.is_valid(instance))
        self.assertEqual(
            [error.message for error in result.errors],
            before,
        )
        with self.assertRaises(exceptions.ValidationError):
            validator.validate(instance)

    def test_consecutive_evaluations_do_not_share_annotations(self):
        validator = Draft202012Validator({
            "anyOf": [
                {"title": "Strings", "type": "string"},
                {"description": "Integers", "type": "integer"},
            ],
        })

        first = validator.evaluate("foo")
        second = validator.evaluate(12)
        third = validator.evaluate("foo")

        self.assertEqual(
            [(a.keyword, a.value) for a in first.annotations],
            [("title", "Strings")],
        )
        self.assertEqual(
            [(a.keyword, a.value) for a in second.annotations],
            [("description", "Integers")],
        )
        self.assertEqual(first, third)

    def test_result_is_an_evaluation_result(self):
        result = Draft202012Validator(True).evaluate(37)
        self.assertEqual(
            result,
            EvaluationResult(valid=True, errors=(), annotations=()),
        )


class TestEvaluateApplicators(TestCase):
    def test_anyOf_collects_from_all_passing_branches(self):
        validator = Draft202012Validator({
            "anyOf": [
                {"title": "A", "type": "string"},
                {"description": "B", "minLength": 3},
                {"default": "C", "type": "number"},
            ],
        })
        result = validator.evaluate("abcdef")

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.value, a.schema_location)
                for a in result.annotations
            },
            {
                ("title", "A", "#/anyOf/0/title"),
                ("description", "B", "#/anyOf/1/description"),
            },
        )

    def test_oneOf_collects_only_from_the_unique_passing_branch(self):
        validator = Draft202012Validator({
            "oneOf": [
                {"title": "A", "type": "string"},
                {"description": "B", "minLength": 3},
            ],
        })
        result = validator.evaluate("ab")

        self.assertTrue(result.valid)
        self.assertEqual(
            [(a.keyword, a.value, a.schema_location)
             for a in result.annotations],
            [("title", "A", "#/oneOf/0/title")],
        )

    def test_oneOf_with_multiple_matches_is_invalid(self):
        validator = Draft202012Validator({
            "oneOf": [
                {"title": "A", "type": "string"},
                {"description": "B", "minLength": 2},
            ],
        })
        result = validator.evaluate("abc")

        self.assertFalse(result.valid)
        self.assertEqual(result.annotations, ())
        self.assertEqual(len(result.errors), 1)

    def test_if_selects_then_and_does_not_leak_annotations(self):
        validator = Draft202012Validator({
            "if": {"title": "HIDDEN", "properties": {"kind": {"const": 1}}},
            "then": {"description": "THEN"},
            "else": {"default": "ELSE"},
        })

        then_result = validator.evaluate({"kind": 1})
        self.assertEqual(
            [(a.keyword, a.value, a.schema_location)
             for a in then_result.annotations],
            [("description", "THEN", "#/then/description")],
        )

        else_result = validator.evaluate({"kind": 2})
        self.assertEqual(
            [(a.keyword, a.value, a.schema_location)
             for a in else_result.annotations],
            [("default", "ELSE", "#/else/default")],
        )

    def test_not_annotations_never_appear(self):
        validator = Draft202012Validator({
            "title": "Kept",
            "not": {"title": "Hidden", "type": "string"},
        })
        result = validator.evaluate(12)

        self.assertTrue(result.valid)
        self.assertEqual(
            [(a.keyword, a.value) for a in result.annotations],
            [("title", "Kept")],
        )

    def test_failed_branches_do_not_contribute(self):
        validator = Draft202012Validator({
            "allOf": [
                {"title": "Outer"},
                {"anyOf": [
                    {"description": "Failed", "type": "string"},
                    {"readOnly": True, "type": "integer"},
                ]},
            ],
        })
        result = validator.evaluate(3)

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.value, a.schema_location)
                for a in result.annotations
            },
            {
                ("title", "Outer", "#/allOf/0/title"),
                ("readOnly", True, "#/allOf/1/anyOf/1/readOnly"),
            },
        )

    def test_nested_combination(self):
        validator = Draft202012Validator({
            "title": "Root",
            "properties": {
                "items": {
                    "type": "array",
                    "prefixItems": [{"writeOnly": True}],
                    "items": {"title": "Extra"},
                },
            },
            "if": {"required": ["flag"]},
            "then": {
                "dependentSchemas": {
                    "flag": {"description": "Has flag"},
                },
            },
        })
        result = validator.evaluate(
            {"flag": True, "items": [1, 2, 3]},
        )

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.instance_location, a.schema_location)
                for a in result.annotations
            },
            {
                ("title", (), "#/title"),
                ("writeOnly", ("items", 0),
                 "#/properties/items/prefixItems/0/writeOnly"),
                ("title", ("items", 1), "#/properties/items/items/title"),
                ("title", ("items", 2), "#/properties/items/items/title"),
                ("description", (),
                 "#/then/dependentSchemas/flag/description"),
            },
        )


class TestEvaluateReferences(TestCase):
    def test_ref_locations_use_the_resolved_resource_uri(self):
        registry = registry_of(**{
            "https://example.com/address.json": {
                "$id": "https://example.com/address.json",
                "title": "Address",
                "properties": {
                    "zip": {"default": "00000", "type": "string"},
                },
            },
        })
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/person.json",
                "properties": {
                    "name": {"title": "Name"},
                    "address": {"$ref": "address.json"},
                },
            },
            registry=registry,
        )
        result = validator.evaluate(
            {"name": "foo", "address": {"zip": "12345"}},
        )

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.instance_location, a.schema_location)
                for a in result.annotations
            },
            {
                ("title", ("name",),
                 "https://example.com/person.json#/properties/name/title"),
                ("title", ("address",),
                 "https://example.com/address.json#/title"),
                ("default", ("address", "zip"),
                 "https://example.com/address.json#/properties/zip/default"),
            },
        )

    def test_ref_with_pointer_fragment(self):
        registry = registry_of(**{
            "https://example.com/defs.json": {
                "$id": "https://example.com/defs.json",
                "$defs": {
                    "named": {"title": "Named", "type": "string"},
                },
            },
        })
        validator = Draft202012Validator(
            {"$ref": "https://example.com/defs.json#/$defs/named"},
            registry=registry,
        )
        result = validator.evaluate("foo")

        self.assertTrue(result.valid)
        self.assertEqual(
            [(a.keyword, a.schema_location) for a in result.annotations],
            [("title",
              "https://example.com/defs.json#/$defs/named/title")],
        )

    def test_dynamicRef_locations_use_the_resolved_resource_uri(self):
        registry = registry_of(**{
            "https://example.com/tree.json": {
                "$id": "https://example.com/tree.json",
                "$dynamicAnchor": "node",
                "type": "object",
                "properties": {
                    "value": {"title": "Value"},
                    "children": {
                        "type": "array",
                        "items": {"$dynamicRef": "#node"},
                    },
                },
            },
        })
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/root.json",
                "$ref": "tree.json",
            },
            registry=registry,
        )
        result = validator.evaluate(
            {"value": 1, "children": [{"value": 2}]},
        )

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.instance_location, a.schema_location)
                for a in result.annotations
            },
            {
                (("value",),
                 "https://example.com/tree.json#/properties/value/title"),
                (("children", 0, "value"),
                 "https://example.com/tree.json#node/properties/value/title"),
            },
        )

    def test_nested_references_and_combinations_across_resources(self):
        registry = registry_of(**{
            "https://example.com/common.json": {
                "$id": "https://example.com/common.json",
                "$defs": {
                    "labeled": {
                        "description": "Labeled",
                        "properties": {
                            "label": {"title": "Label"},
                        },
                    },
                },
            },
        })
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/root.json",
                "allOf": [
                    {"$ref": "common.json#/$defs/labeled"},
                    {
                        "anyOf": [
                            {"title": "With label", "required": ["label"]},
                            {"title": "Without label", "required": ["other"]},
                        ],
                    },
                ],
            },
            registry=registry,
        )
        result = validator.evaluate({"label": "x"})

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.instance_location, a.schema_location)
                for a in result.annotations
            },
            {
                ("description", (),
                 "https://example.com/common.json#/$defs/labeled"
                 "/description"),
                ("title", ("label",),
                 "https://example.com/common.json"
                 "#/$defs/labeled/properties/label/title"),
                ("title", (),
                 "https://example.com/root.json#/allOf/1/anyOf/0/title"),
            },
        )


class TestEvaluateAnnotationsOnly(TestCase):
    def test_instance_and_schema_are_not_modified(self):
        schema = {
            "properties": {
                "a": {"default": 1, "title": "A"},
            },
        }
        instance = {"a": 5}
        validator = Draft202012Validator(schema)

        result = validator.evaluate(instance)

        self.assertTrue(result.valid)
        self.assertEqual(instance, {"a": 5})
        self.assertEqual(
            schema,
            {"properties": {"a": {"default": 1, "title": "A"}}},
        )
        self.assertEqual(
            by_keyword(result)[("default", ("a",))].value,
            1,
        )
