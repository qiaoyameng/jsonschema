"""
Tests for the structured ``basic`` and ``detailed`` validation output.
"""

from __future__ import annotations

from pathlib import Path
from unittest import TestCase
import json

from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from jsonschema import (
    Draft202012Validator,
    _output,
    exceptions,
    validators,
)


def registry_with(uri, contents):
    return Registry().with_resource(
        uri,
        Resource.from_contents(contents, default_specification=DRAFT202012),
    )


class OutputSpecificationTests(TestCase):
    def test_declared_specification_and_support(self):
        self.assertEqual(
            _output.OUTPUT_SPECIFICATION,
            "https://json-schema.org/draft/2020-12/output/schema",
        )
        self.assertEqual(
            _output.SUPPORTED_OUTPUT_FORMATS,
            ("basic", "detailed"),
        )

    def test_unsupported_format_raises(self):
        validator = Draft202012Validator({"type": "string"})
        with self.assertRaises(ValueError):
            validator.output(1, format="verbose")

    def test_output_is_json_serializable(self):
        validator = Draft202012Validator(
            {"$id": "https://example.com/schema", "type": "string"},
        )
        for format in ("basic", "detailed"):
            result = validator.output(1, format=format)
            json.dumps(result)


class ValidOutputTests(TestCase):
    def test_valid_instance_is_flag_only(self):
        validator = Draft202012Validator({"type": "string"})
        self.assertEqual(validator.basic_output("ok"), {"valid": True})
        self.assertEqual(validator.detailed_output("ok"), {"valid": True})

    def test_true_boolean_schema(self):
        self.assertEqual(
            Draft202012Validator(True).basic_output({"anything": True}),
            {"valid": True},
        )


class BooleanSchemaTests(TestCase):
    def test_false_boolean_schema_root(self):
        result = Draft202012Validator(False).basic_output({"x": 1})
        self.assertEqual(
            result,
            {
                "valid": False,
                "keywordLocation": "",
                "instanceLocation": "",
                "errors": [
                    {
                        "valid": False,
                        "keywordLocation": "",
                        "instanceLocation": "",
                    },
                ],
            },
        )

    def test_false_boolean_subschema(self):
        result = Draft202012Validator({"allOf": [False]}).basic_output(1)
        unit = result["errors"][0]
        self.assertEqual(unit["keywordLocation"], "/allOf/0")
        self.assertEqual(unit["instanceLocation"], "")


class LocationTests(TestCase):
    def test_root_keyword(self):
        validator = Draft202012Validator(
            {"$id": "https://example.com/schema", "type": "string"},
        )
        unit = validator.basic_output(1)["errors"][0]
        self.assertEqual(unit["keywordLocation"], "/type")
        self.assertEqual(unit["instanceLocation"], "")
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/schema#/type",
        )

    def test_nested_property(self):
        validator = Draft202012Validator(
            {"properties": {"p": {"type": "string"}}},
        )
        unit = validator.basic_output({"p": 1})["errors"][0]
        self.assertEqual(unit["keywordLocation"], "/properties/p/type")
        self.assertEqual(unit["instanceLocation"], "/p")
        self.assertNotIn("absoluteKeywordLocation", unit)

    def test_nested_arrays(self):
        validator = Draft202012Validator(
            {"items": {"items": {"type": "string"}}},
        )
        units = validator.basic_output([[1, 2]])["errors"]
        locations = {
            (unit["keywordLocation"], unit["instanceLocation"])
            for unit in units
        }
        self.assertEqual(
            locations,
            {
                ("/items/items/type", "/0/0"),
                ("/items/items/type", "/0/1"),
            },
        )

    def test_json_pointer_escaping(self):
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/schema",
                "properties": {"~a/b": {"type": "number"}},
            },
        )
        unit = validator.basic_output({"~a/b": "foobar"})["errors"][0]
        self.assertEqual(
            unit["keywordLocation"],
            "/properties/~0a~1b/type",
        )
        self.assertEqual(unit["instanceLocation"], "/~0a~1b")
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/schema#/properties/~0a~1b/type",
        )


class ApplicatorTreeTests(TestCase):
    def test_any_of_basic_is_flat(self):
        validator = Draft202012Validator(
            {"anyOf": [{"type": "string"}, {"minimum": 3}]},
        )
        units = validator.basic_output(1)["errors"]
        locations = [unit["keywordLocation"] for unit in units]
        self.assertEqual(
            sorted(locations),
            ["/anyOf", "/anyOf/0/type", "/anyOf/1/minimum"],
        )

    def test_any_of_detailed_keeps_tree(self):
        validator = Draft202012Validator(
            {"anyOf": [{"type": "string"}, {"minimum": 3}]},
        )
        result = validator.detailed_output(1)
        self.assertEqual(result["keywordLocation"], "")
        self.assertEqual(result["instanceLocation"], "")
        parent = result["errors"][0]
        self.assertEqual(parent["keywordLocation"], "/anyOf")
        self.assertEqual(
            [child["keywordLocation"] for child in parent["errors"]],
            ["/anyOf/0/type", "/anyOf/1/minimum"],
        )

    def test_nested_any_of_detailed_tree(self):
        validator = Draft202012Validator(
            {"anyOf": [{"anyOf": [{"type": "string"}]}]},
        )
        parent = validator.detailed_output(1)["errors"][0]
        child = parent["errors"][0]
        grandchild = child["errors"][0]
        self.assertEqual(child["keywordLocation"], "/anyOf/0/anyOf")
        self.assertEqual(
            grandchild["keywordLocation"],
            "/anyOf/0/anyOf/0/type",
        )


class ReferenceTests(TestCase):
    def test_relative_ref_points_to_target_resource(self):
        registry = registry_with(
            "https://example.com/other.json",
            {"$defs": {"bar": {"type": "string"}}},
        )
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/main.json",
                "properties": {
                    "foo": {"$ref": "other.json#/$defs/bar"},
                },
            },
            registry=registry,
        )
        unit = validator.basic_output({"foo": 1})["errors"][0]
        self.assertEqual(
            unit["keywordLocation"],
            "/properties/foo/$ref/type",
        )
        self.assertEqual(unit["instanceLocation"], "/foo")
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/other.json#/$defs/bar/type",
        )

    def test_ref_does_not_point_back_to_source_file(self):
        registry = registry_with(
            "https://example.com/other.json",
            {"type": "string"},
        )
        validator = Draft202012Validator(
            {"$id": "https://example.com/main.json", "$ref": "other.json"},
            registry=registry,
        )
        unit = validator.basic_output(1)["errors"][0]
        self.assertTrue(
            unit["absoluteKeywordLocation"].startswith(
                "https://example.com/other.json#",
            ),
        )
        self.assertNotIn("main.json", unit["absoluteKeywordLocation"])

    def test_chained_refs(self):
        registry = registry_with(
            "https://example.com/outer.json",
            {"items": {"$ref": "inner.json"}},
        ).with_resource(
            "https://example.com/inner.json",
            Resource.from_contents(
                {"type": "string"},
                default_specification=DRAFT202012,
            ),
        )
        validator = Draft202012Validator(
            {"$id": "https://example.com/main.json", "$ref": "outer.json"},
            registry=registry,
        )
        unit = validator.basic_output([1])["errors"][0]
        self.assertEqual(
            unit["keywordLocation"],
            "/$ref/items/$ref/type",
        )
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/inner.json#/type",
        )

    def test_nested_embedded_id(self):
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/root.json",
                "properties": {
                    "p": {"$id": "sub.json", "type": "string"},
                },
            },
        )
        unit = validator.basic_output({"p": 1})["errors"][0]
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/sub.json#/type",
        )
        self.assertEqual(
            unit["keywordLocation"],
            "/properties/p/type",
        )


class ContentPolicyTests(TestCase):
    def test_instance_values_are_never_embedded(self):
        validator = Draft202012Validator({"type": "string"})
        result = validator.detailed_output(987654321)
        serialized = json.dumps(result)
        self.assertNotIn("987654321", serialized)

    def test_no_unknown_annotation_keys(self):
        validator = Draft202012Validator(
            {"type": "string", "readOnly": True, "title": "ignored"},
        )
        result = validator.basic_output(1)
        for unit in result.get("errors", []):
            self.assertNotIn("annotations", unit)
            self.assertNotIn("annotation", unit)

    def test_no_success_branch_information(self):
        # anyOf with one passing branch reports only the failing branch
        validator = Draft202012Validator(
            {"anyOf": [{"type": "string"}, {"minimum": 3}]},
        )
        result = validator.detailed_output(1)
        parent = result["errors"][0]
        self.assertEqual(len(parent["errors"]), 2)
        for child in parent["errors"]:
            self.assertFalse(child["valid"])
            self.assertNotIn("annotations", child)


class CallbackTests(TestCase):
    def _validator_with_callback(self):
        calls = []

        def callback(validator, value, instance, schema):
            calls.append(instance)
            if instance != value:
                yield exceptions.ValidationError("callback failed")

        custom = validators.extend(
            Draft202012Validator,
            {"customKeyword": callback},
        )
        return custom({"customKeyword": 1}), calls

    def test_callback_runs_once_per_output(self):
        validator, calls = self._validator_with_callback()
        validator.basic_output(2)
        self.assertEqual(calls, [2])

    def test_callback_runs_once_for_each_format(self):
        validator, calls = self._validator_with_callback()
        validator.basic_output(2)
        validator.detailed_output(2)
        self.assertEqual(calls, [2, 2])


class CompatibilityTests(TestCase):
    def test_iter_errors_is_unchanged(self):
        validator = Draft202012Validator({"type": "string"})
        validator.basic_output(1)
        errors = list(validator.iter_errors(1))
        self.assertEqual(len(errors), 1)
        self.assertEqual(list(errors[0].absolute_schema_path), ["type"])
        self.assertEqual(errors[0].message, "1 is not of type 'string'")

    def test_original_validator_class_is_not_mutated(self):
        validator = Draft202012Validator({"type": "string"})
        original_class = type(validator)
        validator.basic_output(1)
        self.assertIs(type(validator), original_class)

    def test_output_matches_output_schema(self):
        path = (
            Path(__file__).parents[2]
            / "json"
            / "output-tests"
            / "draft2020-12"
            / "output-schema.json"
        )
        output_schema = json.loads(path.read_text())
        registry = registry_with(
            "https://json-schema.org/draft/2020-12/output/schema",
            output_schema,
        )
        checker = Draft202012Validator(output_schema, registry=registry)
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/schema",
                "anyOf": [{"type": "string"}, {"minimum": 3}],
            },
        )
        for format in ("basic", "detailed"):
            result = validator.output(1, format=format)
            self.assertFalse(list(checker.iter_errors(result)))


class PublicInterfaceTests(TestCase):
    def test_package_level_functions(self):
        import jsonschema

        validator = Draft202012Validator({"type": "string"})
        self.assertEqual(
            jsonschema.basic_output(validator, 1),
            validator.basic_output(1),
        )
        self.assertEqual(
            jsonschema.detailed_output(validator, 1),
            validator.detailed_output(1),
        )
        self.assertEqual(
            jsonschema.OUTPUT_SPECIFICATION,
            _output.OUTPUT_SPECIFICATION,
        )
        self.assertEqual(
            jsonschema.SUPPORTED_OUTPUT_FORMATS,
            _output.SUPPORTED_OUTPUT_FORMATS,
        )

    def test_output_module(self):
        from jsonschema import output

        validator = Draft202012Validator({"type": "string"})
        self.assertEqual(
            output.output(validator, 1, format="detailed"),
            validator.detailed_output(1),
        )
        self.assertEqual(
            output.basic(validator, 1),
            validator.basic_output(1),
        )

    def test_validators_module_functions(self):
        from jsonschema import validators as validators_module

        validator = Draft202012Validator({"type": "string"})
        self.assertEqual(
            validators_module.output(validator, 1),
            validator.basic_output(1),
        )
        self.assertEqual(
            validators_module.basic_output(validator, 1),
            validator.basic_output(1),
        )
        self.assertEqual(
            validators_module.detailed_output(validator, 1),
            validator.detailed_output(1),
        )

    def test_validate_output_function(self):
        from jsonschema.validators import validate_output

        result = validate_output(1, {"type": "string"})
        self.assertFalse(result["valid"])
        result = validate_output("ok", {"type": "string"})
        self.assertEqual(result, {"valid": True})

    def test_protocol_declares_output_methods(self):
        from jsonschema.protocols import Validator

        for method in ("output", "basic_output", "detailed_output"):
            self.assertTrue(hasattr(Validator, method), method)


class DynamicReferenceTests(TestCase):
    def test_dynamic_ref_uses_its_own_keyword_name(self):
        validator = Draft202012Validator(
            {
                "$defs": {
                    "node": {
                        "$dynamicAnchor": "node",
                        "type": "string",
                    },
                },
                "properties": {"n": {"$dynamicRef": "#node"}},
            },
        )
        unit = validator.basic_output({"n": 1})["errors"][0]
        self.assertEqual(
            unit["keywordLocation"],
            "/properties/n/$dynamicRef/type",
        )

    def test_dynamic_ref_external_resource(self):
        registry = registry_with(
            "https://example.com/ext.json",
            {"$dynamicAnchor": "node", "type": "string"},
        )
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/root.json",
                "$dynamicRef": "ext.json#node",
            },
            registry=registry,
        )
        unit = validator.basic_output(1)["errors"][0]
        self.assertEqual(unit["keywordLocation"], "/$dynamicRef/type")
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/ext.json#/type",
        )


class ReferenceFragmentEscapingTests(TestCase):
    def test_ref_fragment_with_escaped_pointer(self):
        registry = registry_with(
            "https://example.com/other.json",
            {"$defs": {"~a/b": {"type": "string"}}},
        )
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/main.json",
                "$ref": "other.json#/$defs/~0a~1b",
            },
            registry=registry,
        )
        unit = validator.basic_output(1)["errors"][0]
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/other.json#/$defs/~0a~1b/type",
        )


class EmbeddedIdReferenceTests(TestCase):
    def test_embedded_id_in_referenced_resource_resets_location(self):
        registry = registry_with(
            "https://example.com/other.json",
            {
                "$defs": {
                    "bar": {"$id": "inner.json", "type": "string"},
                },
            },
        )
        validator = Draft202012Validator(
            {
                "$id": "https://example.com/main.json",
                "$ref": "other.json#/$defs/bar",
            },
            registry=registry,
        )
        unit = validator.basic_output(1)["errors"][0]
        self.assertEqual(
            unit["absoluteKeywordLocation"],
            "https://example.com/inner.json#/type",
        )
        self.assertEqual(unit["keywordLocation"], "/$ref/type")
