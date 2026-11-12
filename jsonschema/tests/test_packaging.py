"""
Tests for schema packages: packing registries for offline delivery.
"""

from __future__ import annotations

from tempfile import TemporaryDirectory
from unittest import TestCase
import copy
import json
import pathlib

from referencing import Registry
from referencing.jsonschema import (
    DRAFT4,
    DRAFT7,
    DRAFT201909,
    DRAFT202012,
)
import referencing.exceptions

from jsonschema import packaging, validators

BASE = "https://example.com/offline"

ALPHA_COMMON = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"{BASE}/alpha/common.json",
    "$dynamicAnchor": "node",
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "child": {"$dynamicRef": "#node"},
    },
    "required": ["name"],
}

BETA_COMMON = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"{BASE}/beta/common.json",
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
}

ROOT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"{BASE}/alpha/root.json",
    "type": "object",
    "properties": {
        "a": {"$ref": "common.json"},
        "b": {"$ref": "../beta/common.json"},
    },
}

GOOD_INSTANCE = {
    "a": {"name": "x", "child": {"name": "y", "child": {"name": "z"}}},
    "b": {"count": 1},
}

BAD_INSTANCE = {"a": {"name": "x", "child": {"name": 1}}, "b": {"count": "no"}}

BAD_PATHS = [["a", "child", "name"], ["b", "count"]]


def registry_for(fixture):
    registry = Registry()
    return registry.with_resources(
        (uri, DRAFT202012.create_resource(schema))
        for uri, schema in fixture
    )


def write_fixture(tmp):
    """
    Lay the schemas out as two directories with same-named files.
    """
    root = pathlib.Path(tmp)
    alpha = root / "alpha"
    beta = root / "beta"
    alpha.mkdir()
    beta.mkdir()
    (alpha / "common.json").write_text(
        json.dumps(ALPHA_COMMON), encoding="utf-8"
    )
    (beta / "common.json").write_text(
        json.dumps(BETA_COMMON), encoding="utf-8"
    )
    (alpha / "root.json").write_text(
        json.dumps(ROOT_SCHEMA), encoding="utf-8"
    )
    return alpha, beta


FIXTURE = [
    (f"{BASE}/alpha/common.json", ALPHA_COMMON),
    (f"{BASE}/beta/common.json", BETA_COMMON),
]


def pack_fixture():
    registry = registry_for(FIXTURE)
    return registry, packaging.pack(registry, ROOT_SCHEMA)


def error_paths(validator, instance):
    return [
        list(error.absolute_path)
        for error in validator.iter_errors(instance)
    ]


class TestPackRoundTrip(TestCase):
    def test_two_directories_with_same_named_files(self):
        """
        The two ``common.json`` files live in different directories, and
        the root refers to each via relative references; both survive the
        round trip with their distinct identities intact.
        """
        registry, package = pack_fixture()
        self.assertEqual(
            sorted(package["manifest"]),
            [
                f"{BASE}/alpha/common.json",
                f"{BASE}/alpha/root.json",
                f"{BASE}/beta/common.json",
            ],
        )
        restored = packaging.load(package)
        self.assertEqual(
            restored.registry.contents(f"{BASE}/alpha/common.json"),
            ALPHA_COMMON,
        )
        self.assertEqual(
            restored.registry.contents(f"{BASE}/beta/common.json"),
            BETA_COMMON,
        )

    def test_acceptance_and_error_locations_are_preserved(self):
        registry, package = pack_fixture()
        restored = packaging.load(package)

        before = validators.Draft202012Validator(
            ROOT_SCHEMA, registry=registry
        )
        after = validators.Draft202012Validator(
            restored.root, registry=restored.registry
        )

        self.assertTrue(before.is_valid(GOOD_INSTANCE))
        self.assertTrue(after.is_valid(GOOD_INSTANCE))
        self.assertFalse(before.is_valid(BAD_INSTANCE))
        self.assertFalse(after.is_valid(BAD_INSTANCE))
        self.assertEqual(
            error_paths(before, BAD_INSTANCE),
            error_paths(after, BAD_INSTANCE),
        )
        self.assertEqual(error_paths(after, BAD_INSTANCE), BAD_PATHS)

    def test_package_is_json_serializable(self):
        _, package = pack_fixture()
        serialized = json.dumps(package)
        restored = packaging.load(json.loads(serialized))
        self.assertEqual(
            sorted(restored.registry), sorted(package["manifest"])
        )

    def test_fixture_layout_matches_two_directories(self):
        with TemporaryDirectory() as tmp:
            alpha, beta = write_fixture(tmp)
            self.assertTrue((alpha / "common.json").exists())
            self.assertTrue((beta / "common.json").exists())
            self.assertNotEqual(
                json.loads((alpha / "common.json").read_text()),
                json.loads((beta / "common.json").read_text()),
            )

    def test_packaging_from_two_directories_on_disk(self):
        """
        The deployment flow: read same-named files from two directories,
        pack them, and validate offline with only the package in hand.
        """
        with TemporaryDirectory() as tmp:
            alpha, beta = write_fixture(tmp)
            registry = Registry().with_resources(
                (
                    f"{BASE}/{directory.name}/common.json",
                    DRAFT202012.create_resource(
                        json.loads(
                            (directory / "common.json").read_text(
                                encoding="utf-8"
                            ),
                        ),
                    ),
                )
                for directory in (alpha, beta)
            )
            root = json.loads(
                (alpha / "root.json").read_text(encoding="utf-8")
            )

            package = packaging.pack(registry, root)
            serialized = json.dumps(package)

        # From here on, nothing but the serialized package is available.
        restored = packaging.load(json.loads(serialized))
        validator = validators.Draft202012Validator(
            restored.root, registry=restored.registry
        )
        self.assertTrue(validator.is_valid(GOOD_INSTANCE))
        self.assertEqual(
            error_paths(validator, BAD_INSTANCE), BAD_PATHS
        )


class TestAnchorsAndDynamicReferences(TestCase):
    def test_anchor_and_dynamic_anchor_boundaries_in_manifest(self):
        root = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/root.json",
            "$dynamicAnchor": "node",
            "$defs": {"s": {"$id": "sub.json", "$anchor": "thing"}},
            "properties": {"child": {"$dynamicRef": "#node"}},
        }
        package = packaging.pack(Registry(), root)
        root_entry = package["manifest"][f"{BASE}/root.json"]
        self.assertEqual(root_entry["anchors"], [])
        self.assertEqual(root_entry["dynamicAnchors"], ["node"])
        self.assertEqual(
            root_entry["subresources"], ["sub.json"]
        )
        sub_entry = package["manifest"][f"{BASE}/sub.json"]
        self.assertEqual(sub_entry["anchors"], ["thing"])
        self.assertEqual(sub_entry["dynamicAnchors"], [])

        restored = packaging.load(package)
        resolved = restored.resolver.lookup("sub.json#thing")
        self.assertEqual(resolved.contents["$anchor"], "thing")
        self.assertEqual(
            resolved.resolver._base_uri, f"{BASE}/sub.json"
        )

    def test_recursive_dynamic_reference_resolves_identically(self):
        tree = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/tree/schema.json",
            "$dynamicAnchor": "node",
            "type": "object",
            "properties": {
                "data": True,
                "children": {
                    "type": "array",
                    "items": {"$dynamicRef": "#node"},
                },
            },
        }
        strict = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/strict/schema.json",
            "$dynamicAnchor": "node",
            "$ref": f"{BASE}/tree/schema.json",
            "properties": {"data": {"type": "string"}},
        }
        registry = Registry().with_resources([
            (f"{BASE}/tree/schema.json", DRAFT202012.create_resource(tree)),
            (
                f"{BASE}/strict/schema.json",
                DRAFT202012.create_resource(strict),
            ),
        ])
        package = packaging.pack(registry, strict)
        restored = packaging.load(package)
        instance = {
            "data": "ok",
            "children": [{"data": "ok"}, {"data": 3}],
        }
        before = validators.Draft202012Validator(strict, registry=registry)
        after = validators.Draft202012Validator(
            restored.root, registry=restored.registry
        )
        self.assertEqual(
            error_paths(before, instance),
            [["children", 1, "data"]],
        )
        self.assertEqual(
            error_paths(after, instance),
            [["children", 1, "data"]],
        )

    def test_2019_09_recursive_ref(self):
        child = {
            "$id": f"{BASE}/2019/child.json",
            "$recursiveAnchor": True,
            "type": "string",
        }
        root = {
            "$id": f"{BASE}/2019/root.json",
            "$schema": "https://json-schema.org/draft/2019-09/schema",
            "$recursiveAnchor": True,
            "type": "object",
            "properties": {
                "name": {"$recursiveRef": "#"},
                "c": {"$ref": "child.json"},
            },
        }
        registry = Registry().with_resources([
            (f"{BASE}/2019/root.json", DRAFT201909.create_resource(root)),
            (f"{BASE}/2019/child.json", DRAFT201909.create_resource(child)),
        ])
        package = packaging.pack(registry, root)
        restored = packaging.load(package)
        self.assertEqual(
            package["manifest"][f"{BASE}/2019/root.json"]["specification"],
            "draft2019-09",
        )
        validator = validators.Draft201909Validator(
            restored.root, registry=restored.registry
        )
        self.assertTrue(validator.is_valid({"c": "x"}))
        self.assertFalse(validator.is_valid({"c": 1}))


class TestConflicts(TestCase):
    def test_same_uri_different_contents_is_rejected(self):
        root = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/root.json",
            "allOf": [
                {"$id": "shared.json", "type": "string"},
                {"$id": "shared.json", "type": "integer"},
            ],
        }
        with self.assertRaises(packaging.ConflictingResource) as cm:
            packaging.pack(Registry(), root)
        self.assertEqual(cm.exception.uri, f"{BASE}/shared.json")

    def test_same_uri_same_contents_is_fine(self):
        root = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/root.json",
            "allOf": [
                {"$id": "shared.json", "type": "string"},
                {"$id": "shared.json", "type": "string"},
            ],
        }
        package = packaging.pack(Registry(), root)
        self.assertEqual(
            sorted(package["manifest"]),
            [f"{BASE}/root.json", f"{BASE}/shared.json"],
        )

    def test_root_conflicting_with_registry_is_rejected(self):
        root = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/root.json",
            "type": "integer",
        }
        registry = Registry().with_resource(
            f"{BASE}/root.json",
            DRAFT202012.create_resource({"type": "string"}),
        )
        with self.assertRaises(packaging.ConflictingResource) as cm:
            packaging.pack(registry, root)
        self.assertEqual(cm.exception.uri, f"{BASE}/root.json")


class TestMissingResources(TestCase):
    def test_unregistered_reference_is_rejected(self):
        root = {
            "$id": f"{BASE}/root.json",
            "$ref": "https://remote.example/missing.json",
        }
        with self.assertRaises(packaging.MissingResource) as cm:
            packaging.pack(Registry(), root)
        self.assertEqual(
            cm.exception.uri, "https://remote.example/missing.json"
        )
        self.assertEqual(
            cm.exception.ref, "https://remote.example/missing.json"
        )
        self.assertEqual(cm.exception.pointer, "#/$ref")

    def test_missing_reference_reports_location(self):
        root = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/root.json",
            "properties": {"p": {"$ref": "missing.json"}},
        }
        with self.assertRaises(packaging.MissingResource) as cm:
            packaging.pack(Registry(), root)
        self.assertEqual(cm.exception.uri, f"{BASE}/missing.json")
        self.assertEqual(cm.exception.pointer, "#/properties/p/$ref")

    def test_unregistered_remote_is_never_retrieved(self):
        calls = []

        def retrieve(uri):
            calls.append(uri)
            raise referencing.exceptions.NoSuchResource(ref=uri)

        root = {
            "$id": f"{BASE}/root.json",
            "$ref": "https://remote.example/missing.json",
        }
        registry = Registry(retrieve=retrieve)
        with self.assertRaises(packaging.MissingResource):
            packaging.pack(registry, root)
        self.assertEqual(calls, [])

    def test_restored_registry_does_not_retrieve(self):
        restored = packaging.load(
            packaging.pack(
                Registry(),
                {"$id": f"{BASE}/root.json", "type": "integer"},
            ),
        )
        with self.assertRaises(referencing.exceptions.NoSuchResource):
            restored.registry.get_or_retrieve(
                "https://remote.example/missing.json"
            )


class TestBudgets(TestCase):
    def test_resource_budget(self):
        root = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/root.json",
            "$defs": {
                "a": {"$id": "a.json", "type": "integer"},
                "b": {"$id": "b.json", "type": "string"},
            },
        }
        with self.assertRaises(packaging.BudgetExceeded):
            packaging.pack(
                Registry(), root, budget=packaging.Budget(max_resources=2)
            )

    def test_byte_budget(self):
        root = {
            "$id": f"{BASE}/root.json",
            "description": "x" * 1000,
        }
        with self.assertRaises(packaging.BudgetExceeded):
            packaging.pack(
                Registry(), root, budget=packaging.Budget(max_bytes=100)
            )

    def test_depth_budget(self):
        node = root = {"$id": f"{BASE}/root.json", "properties": {}}
        for _ in range(10):
            node["properties"]["nested"] = {"type": "object", "properties": {}}
            node = node["properties"]["nested"]
        with self.assertRaises(packaging.BudgetExceeded):
            packaging.pack(
                Registry(), root, budget=packaging.Budget(max_depth=3)
            )

    def test_load_budget(self):
        package = packaging.pack(
            Registry(), {"$id": f"{BASE}/root.json", "type": "integer"}
        )
        with self.assertRaises(packaging.BudgetExceeded):
            packaging.load(
                package, budget=packaging.Budget(max_resources=0)
            )


class TestManifestVerification(TestCase):
    def test_each_resource_has_a_verifiable_digest(self):
        _, package = pack_fixture()
        for entry in package["resources"]:
            described = package["manifest"][entry["uri"]]
            self.assertEqual(
                described["digest"],
                packaging._digest(entry["contents"]),
            )

    def test_modified_contents_are_rejected(self):
        _, package = pack_fixture()
        package["resources"][0]["contents"]["type"] = "string"
        with self.assertRaises(packaging.InvalidPackage) as cm:
            packaging.load(package)
        self.assertIn("digest", str(cm.exception))

    def test_modified_anchors_are_rejected(self):
        _, package = pack_fixture()
        entry = package["manifest"][f"{BASE}/alpha/common.json"]
        entry["dynamicAnchors"] = ["somethingelse"]
        with self.assertRaises(packaging.InvalidPackage) as cm:
            packaging.load(package)
        self.assertIn("dynamicAnchors", str(cm.exception))

    def test_modified_specification_is_rejected(self):
        _, package = pack_fixture()
        package["manifest"][f"{BASE}/alpha/common.json"][
            "specification"
        ] = "not-a-real-draft"
        with self.assertRaises(packaging.InvalidPackage):
            packaging.load(package)

    def test_missing_manifest_entry_is_rejected(self):
        _, package = pack_fixture()
        del package["manifest"][f"{BASE}/beta/common.json"]
        with self.assertRaises(packaging.InvalidPackage):
            packaging.load(package)

    def test_extra_manifest_entry_is_rejected(self):
        _, package = pack_fixture()
        package["manifest"][f"{BASE}/ghost.json"] = {
            "id": None,
            "specification": "draft2020-12",
            "anchors": [],
            "dynamicAnchors": [],
            "subresources": [],
            "digest": "x",
            "size": 1,
        }
        with self.assertRaises(packaging.InvalidPackage):
            packaging.load(package)

    def test_wrong_format_or_version_is_rejected(self):
        _, package = pack_fixture()
        for key, value in [
            ("format", "something-else"),
            ("version", 2),
            ("digestAlgorithm", "md5"),
        ]:
            broken = copy.deepcopy(package)
            broken[key] = value
            with self.assertRaises(packaging.InvalidPackage):
                packaging.load(broken)

    def test_root_must_be_packaged(self):
        _, package = pack_fixture()
        package["root"] = f"{BASE}/not-there.json"
        with self.assertRaises(packaging.InvalidPackage):
            packaging.load(package)

    def test_duplicate_uris_are_rejected(self):
        _, package = pack_fixture()
        duplicate = copy.deepcopy(package["resources"][0])
        package["resources"].append(duplicate)
        with self.assertRaises(packaging.InvalidPackage):
            packaging.load(package)

    def test_load_only_accepts_plain_data(self):
        _, package = pack_fixture()
        for value in [
            "root.json",
            pathlib.Path("root.json"),
            DRAFT202012.create_resource(ROOT_SCHEMA),
            None,
        ]:
            with self.assertRaises(packaging.InvalidPackage):
                packaging.load(value)


class TestIsolation(TestCase):
    def test_registry_and_root_are_not_modified(self):
        registry, package = pack_fixture()
        registry_before = dict(registry.items())
        root_before = copy.deepcopy(ROOT_SCHEMA)
        packaging.pack(registry, ROOT_SCHEMA)
        self.assertEqual(dict(registry.items()), registry_before)
        self.assertEqual(ROOT_SCHEMA, root_before)

    def test_load_does_not_mutate_the_package(self):
        _, package = pack_fixture()
        snapshot = copy.deepcopy(package)
        packaging.load(package)
        self.assertEqual(package, snapshot)

    def test_restored_root_is_independent_of_the_input(self):
        restored = packaging.load(pack_fixture()[1])
        self.assertIsNot(restored.root, ROOT_SCHEMA)
        restored.root["type"] = "string"
        self.assertEqual(ROOT_SCHEMA["type"], "object")


class TestDialectsAndBoundaries(TestCase):
    def test_draft7_legacy_id(self):
        root = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "$id": f"{BASE}/d7/root.json",
            "definitions": {"s": {"$id": "sub7.json", "type": "integer"}},
            "allOf": [{"$ref": "sub7.json"}],
        }
        package = packaging.pack(Registry(), root)
        self.assertEqual(
            package["manifest"][f"{BASE}/d7/root.json"]["specification"],
            "draft-07",
        )
        restored = packaging.load(package)
        validator = validators.Draft7Validator(
            restored.root, registry=restored.registry
        )
        self.assertTrue(validator.is_valid(7))
        self.assertFalse(validator.is_valid("no"))

    def test_draft4_legacy_id(self):
        root = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "id": f"{BASE}/d4/root.json",
            "definitions": {"s": {"id": "sub4.json", "type": "integer"}},
            "allOf": [{"$ref": "sub4.json"}],
        }
        package = packaging.pack(Registry(), root)
        self.assertEqual(
            package["manifest"][f"{BASE}/d4/root.json"]["specification"],
            "draft-04",
        )
        restored = packaging.load(package)
        validator = validators.Draft4Validator(
            restored.root, registry=restored.registry
        )
        self.assertTrue(validator.is_valid(7))
        self.assertFalse(validator.is_valid("no"))

    def test_boolean_schemas(self):
        root = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{BASE}/bool/root.json",
            "properties": {"a": True, "b": False},
        }
        restored = packaging.load(packaging.pack(Registry(), root))
        validator = validators.Draft202012Validator(
            restored.root, registry=restored.registry
        )
        self.assertTrue(validator.is_valid({"a": "anything"}))
        self.assertFalse(validator.is_valid({"b": 1}))

    def test_explicit_root_uri_when_schema_has_no_id(self):
        root = {"type": "object"}
        package = packaging.pack(
            Registry(), root, root_uri="urn:example:no-id-root"
        )
        self.assertEqual(package["root"], "urn:example:no-id-root")
        restored = packaging.load(package)
        self.assertEqual(
            restored.root_uri, "urn:example:no-id-root"
        )
        validator = validators.Draft202012Validator(
            restored.root, registry=restored.registry
        )
        self.assertTrue(validator.is_valid({}))
        self.assertFalse(validator.is_valid(1))

    def test_root_without_id_and_without_uri_is_rejected(self):
        with self.assertRaises(packaging.PackageError):
            packaging.pack(Registry(), {"type": "object"})


class TestResolutionParity(TestCase):
    def test_restored_resolver_matches_original_base_uri(self):
        registry, package = pack_fixture()
        restored = packaging.load(package)
        before = validators.Draft202012Validator(
            ROOT_SCHEMA, registry=registry
        )
        after = validators.Draft202012Validator(
            restored.root, registry=restored.registry
        )
        self.assertEqual(
            before._resolver._base_uri,
            after._resolver._base_uri,
        )
        self.assertEqual(after._resolver._base_uri, restored.root_uri)

    def test_reference_identity_is_preserved(self):
        """
        References resolve to the same resource contents before and after
        packaging, by URI.
        """
        registry, package = pack_fixture()
        restored = packaging.load(package)
        original = registry.crawl()
        for uri in package["manifest"]:
            if original.get(uri) is None:
                continue
            self.assertEqual(
                original.contents(uri),
                restored.registry.contents(uri),
            )

    def test_pathological_relative_id_fails_at_pack_time(self):
        """
        A root whose internal id makes its references unresolvable fails
        when packaging, exactly as it would fail when validated.
        """
        root = {
            "$id": "from-id",
            "type": "object",
            "properties": {"x": {"$ref": "leaf.json"}},
        }
        registry = Registry().with_resource(
            f"{BASE}/leaf.json",
            DRAFT202012.create_resource({"type": "integer"}),
        )
        with self.assertRaises(packaging.MissingResource):
            packaging.pack(registry, root, root_uri=f"{BASE}/explicit")
