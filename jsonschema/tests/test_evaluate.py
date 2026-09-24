"""
Tests for the Draft 2020-12 ``evaluate`` interface.
"""
from unittest import TestCase

from referencing import Registry
import referencing.jsonschema

from jsonschema import Draft202012Validator
from jsonschema._evaluate import Annotation

DRAFT202012 = referencing.jsonschema.DRAFT202012


def annotated(result):
    return {
        (annotation.keyword, annotation.instance_location): annotation
        for annotation in result.annotations
    }


class TestEvaluate(TestCase):
    def test_basic_annotations_with_locations(self):
        schema = {
            "$id": "https://example.com/root",
            "title": "A root schema",
            "properties": {
                "name": {
                    "title": "The name",
                    "description": "A name.",
                    "default": "anonymous",
                },
                "secret": {"writeOnly": True},
                "id": {"readOnly": True},
            },
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate(
            {"name": "Jane", "secret": "s3cr3t", "id": 12},
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.errors, ())
        self.assertEqual(
            result.annotations,
            (
                Annotation(
                    keyword="title",
                    value="A root schema",
                    instance_location=(),
                    schema_location="https://example.com/root#/title",
                ),
                Annotation(
                    keyword="title",
                    value="The name",
                    instance_location=("name",),
                    schema_location=(
                        "https://example.com/root#/properties/name/title"
                    ),
                ),
                Annotation(
                    keyword="description",
                    value="A name.",
                    instance_location=("name",),
                    schema_location=(
                        "https://example.com/root#/properties/name/description"
                    ),
                ),
                Annotation(
                    keyword="default",
                    value="anonymous",
                    instance_location=("name",),
                    schema_location=(
                        "https://example.com/root#/properties/name/default"
                    ),
                ),
                Annotation(
                    keyword="writeOnly",
                    value=True,
                    instance_location=("secret",),
                    schema_location=(
                        "https://example.com/root#/properties/secret/writeOnly"
                    ),
                ),
                Annotation(
                    keyword="readOnly",
                    value=True,
                    instance_location=("id",),
                    schema_location=(
                        "https://example.com/root#/properties/id/readOnly"
                    ),
                ),
            ),
        )

    def test_default_is_only_an_annotation(self):
        schema = {
            "properties": {"name": {"default": "anonymous"}},
        }
        validator = Draft202012Validator(schema)

        instance = {}
        result = validator.evaluate(instance)

        self.assertTrue(result.valid)
        self.assertEqual(instance, {})
        self.assertEqual(result.annotations, ())

    def test_errors_are_the_usual_validation_errors(self):
        schema = {
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }
        validator = Draft202012Validator(schema)

        instance = {"name": 37}
        result = validator.evaluate(instance)

        self.assertFalse(result.valid)
        self.assertEqual(
            [
                (error.message, list(error.path), list(error.schema_path))
                for error in result.errors
            ],
            [
                (error.message, list(error.path), list(error.schema_path))
                for error in validator.iter_errors(instance)
            ],
        )

    def test_invalid_instance_excludes_failed_subschema_annotations(self):
        schema = {
            "properties": {
                "name": {"type": "string", "title": "The name"},
            },
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate({"name": 37})

        self.assertFalse(result.valid)
        self.assertEqual(result.annotations, ())

    def test_boolean_schemas(self):
        self.assertEqual(
            Draft202012Validator(True).evaluate(37).annotations,
            (),
        )
        result = Draft202012Validator(False).evaluate(37)
        self.assertFalse(result.valid)
        self.assertEqual(result.annotations, ())
        self.assertEqual(len(result.errors), 1)

    def test_array_annotations(self):
        schema = {
            "prefixItems": [{"title": "first"}],
            "items": {"description": "the rest"},
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate([1, 2, 3])

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.instance_location)
                for a in result.annotations
            },
            {
                ("title", (0,)),
                ("description", (1,)),
                ("description", (2,)),
            },
        )


class TestEvaluateApplicators(TestCase):
    def test_all_passing_any_of_branches_contribute(self):
        schema = {
            "anyOf": [
                {"title": "always"},
                {"description": "also always"},
            ],
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate(37)

        self.assertTrue(result.valid)
        self.assertEqual(
            {a.keyword for a in result.annotations},
            {"title", "description"},
        )

    def test_failed_any_of_branch_does_not_contribute(self):
        schema = {
            "anyOf": [
                {"type": "integer", "title": "passing"},
                {"type": "string", "title": "failing"},
            ],
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate(37)

        self.assertTrue(result.valid)
        self.assertEqual(
            [(a.keyword, a.value) for a in result.annotations],
            [("title", "passing")],
        )

    def test_one_of_contributes_when_exactly_one_branch_passes(self):
        schema = {
            "oneOf": [
                {"type": "integer", "title": "the one"},
                {"type": "string", "title": "not this one"},
            ],
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate(37)

        self.assertTrue(result.valid)
        self.assertEqual(
            [(a.keyword, a.value) for a in result.annotations],
            [("title", "the one")],
        )

    def test_one_of_contributes_nothing_when_multiple_branches_pass(self):
        schema = {
            "oneOf": [
                {"type": "integer", "title": "one"},
                {"minimum": 3, "title": "two"},
            ],
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate(37)

        self.assertFalse(result.valid)
        self.assertEqual(result.annotations, ())

    def test_if_selects_then_without_leaking_its_annotations(self):
        schema = {
            "if": {"type": "integer", "title": "hidden"},
            "then": {"title": "then branch"},
            "else": {"title": "else branch"},
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate(37)

        self.assertTrue(result.valid)
        self.assertEqual(
            [(a.keyword, a.value) for a in result.annotations],
            [("title", "then branch")],
        )

    def test_if_selects_else_without_leaking_its_annotations(self):
        schema = {
            "if": {"type": "integer", "title": "hidden"},
            "then": {"title": "then branch"},
            "else": {"title": "else branch"},
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate("foo")

        self.assertTrue(result.valid)
        self.assertEqual(
            [(a.keyword, a.value) for a in result.annotations],
            [("title", "else branch")],
        )

    def test_not_never_contributes_annotations(self):
        schema = {
            "not": {"type": "string", "title": "hidden"},
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate(37)

        self.assertTrue(result.valid)
        self.assertEqual(result.annotations, ())

    def test_nested_combination(self):
        schema = {
            "properties": {
                "thing": {
                    "if": {"type": "object"},
                    "then": {
                        "anyOf": [
                            {"required": ["a"], "title": "has a"},
                            {"required": ["b"], "title": "has b"},
                        ],
                    },
                    "else": {"title": "not an object"},
                },
            },
        }
        validator = Draft202012Validator(schema)

        result = validator.evaluate({"thing": {"a": 1, "b": 2}})

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.value, a.instance_location)
                for a in result.annotations
            },
            {
                ("title", "has a", ("thing",)),
                ("title", "has b", ("thing",)),
            },
        )

        result = validator.evaluate({"thing": 12})

        self.assertTrue(result.valid)
        self.assertEqual(
            [
                (a.keyword, a.value, a.instance_location)
                for a in result.annotations
            ],
            [("title", "not an object", ("thing",))],
        )


class TestEvaluateReferences(TestCase):
    def test_ref_locations_point_at_the_referenced_resource(self):
        referenced = {
            "$id": "https://example.com/referenced",
            "title": "Referenced",
            "properties": {"foo": {"description": "A foo."}},
        }
        registry = Registry().with_resource(
            "https://example.com/referenced",
            DRAFT202012.create_resource(referenced),
        )
        schema = {
            "$id": "https://example.com/root",
            "$ref": "referenced",
        }
        validator = Draft202012Validator(schema, registry=registry)

        result = validator.evaluate({"foo": "bar"})

        self.assertTrue(result.valid)
        self.assertEqual(
            {
                (a.keyword, a.instance_location, a.schema_location)
                for a in result.annotations
            },
            {
                (
                    "title",
                    (),
                    "https://example.com/referenced#/title",
                ),
                (
                    "description",
                    ("foo",),
                    "https://example.com/referenced#/properties/foo/description",
                ),
            },
        )

    def test_dynamic_ref_locations_point_at_the_dynamic_target(self):
        tree = {
            "$id": "https://example.com/tree",
            "$dynamicAnchor": "node",
            "title": "A tree node",
            "properties": {
                "children": {
                    "type": "array",
                    "items": {"$dynamicRef": "#node"},
                },
            },
        }
        strict_tree = {
            "$id": "https://example.com/strict-tree",
            "$dynamicAnchor": "node",
            "$ref": "tree",
            "description": "A strict tree node",
        }
        registry = Registry().with_resources([
            ("https://example.com/tree", DRAFT202012.create_resource(tree)),
            (
                "https://example.com/strict-tree",
                DRAFT202012.create_resource(strict_tree),
            ),
        ])
        validator = Draft202012Validator(strict_tree, registry=registry)

        result = validator.evaluate({"children": [{}]})

        self.assertTrue(result.valid)
        locations = annotated(result)
        # The root is the strict tree itself.
        self.assertEqual(
            locations["description", ()].schema_location,
            "https://example.com/strict-tree#/description",
        )
        # Following $ref lands in the tree resource.
        self.assertEqual(
            locations["title", ()].schema_location,
            "https://example.com/tree#/title",
        )
        # The $dynamicRef to #node dynamically resolves back to the
        # strict tree, so the child's annotations are located there --
        # not in the root document of the schema being evaluated.
        self.assertEqual(
            locations["description", ("children", 0)].schema_location,
            "https://example.com/strict-tree#/description",
        )
        self.assertEqual(
            locations["title", ("children", 0)].schema_location,
            "https://example.com/tree#/title",
        )

    def test_ref_within_combinators(self):
        positive = {
            "$id": "https://example.com/positive",
            "minimum": 0,
            "title": "Positive",
        }
        registry = Registry().with_resource(
            "https://example.com/positive",
            DRAFT202012.create_resource(positive),
        )
        schema = {
            "anyOf": [
                {"$ref": "https://example.com/positive"},
                {"type": "string", "title": "A string"},
            ],
        }
        validator = Draft202012Validator(schema, registry=registry)

        result = validator.evaluate(12)

        self.assertTrue(result.valid)
        self.assertEqual(
            [
                (a.keyword, a.value, a.schema_location)
                for a in result.annotations
            ],
            [("title", "Positive", "https://example.com/positive#/title")],
        )


class TestEvaluateRepeatedUse(TestCase):
    def test_consecutive_evaluations_do_not_mix_annotations(self):
        schema = {
            "properties": {
                "name": {"title": "The name"},
                "age": {"readOnly": True},
            },
        }
        validator = Draft202012Validator(schema)

        first = validator.evaluate({"name": "Jane"})
        second = validator.evaluate({"age": 37})
        third = validator.evaluate({"name": "Jane"})

        self.assertEqual(
            [a.instance_location for a in first.annotations],
            [("name",)],
        )
        self.assertEqual(
            [a.instance_location for a in second.annotations],
            [("age",)],
        )
        self.assertEqual(third, first)

    def test_evaluate_does_not_change_existing_behavior(self):
        schema = {
            "properties": {"name": {"type": "string", "default": "x"}},
            "required": ["name"],
        }
        validator = Draft202012Validator(schema)

        instance = {"name": 37}
        validator.evaluate(instance)

        self.assertFalse(validator.is_valid(instance))
        with self.assertRaises(Exception) as cm:
            validator.validate(instance)
        self.assertIn("is not of type", str(cm.exception))
        self.assertEqual(instance, {"name": 37})
