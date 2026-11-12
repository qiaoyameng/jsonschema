=========================
Offline Schema Packaging
=========================

.. currentmodule:: jsonschema.packaging

Sometimes a set of schemas has to be validated somewhere with no access to
the network or to the original files -- for example on an air-gapped
verification host.

The `jsonschema.packaging` module turns an explicit `referencing.Registry`
and a root schema into a single JSON document -- a *schema package* --
which contains every resource reachable from the root, along with a
*manifest* describing each of them.
The package can later be restored into a working registry without any
network access at all.

Creating a Package
------------------

Given a root schema and a registry of the schemas it refers to, call
`pack` to produce a plain JSON object:

.. testcode::

    import json

    from referencing import Registry
    from referencing.jsonschema import DRAFT202012

    from jsonschema import packaging, validators

    root = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.com/root.json",
        "type": "object",
        "properties": {"value": {"$ref": "value.json"}},
    }
    registry = Registry().with_resource(
        "https://example.com/value.json",
        DRAFT202012.create_resource({"type": "integer"}),
    )

    package = packaging.pack(registry, root)
    serialized = json.dumps(package)

The package's ``resources`` hold the actual schemas, and its
``manifest`` records, for every resource, its specification (draft),
subresource ids, anchors, dynamic anchors, size and a ``sha256`` digest.

Packaging fails loudly rather than silently producing something unusable:

* `ConflictingResource` if the same URI is reachable with two different
  sets of contents,
* `MissingResource` if a reference points at a resource that is not in
  the registry -- the registry's retriever is *never* invoked, so
  unregistered remote references are never fetched,
* `BudgetExceeded` if one of the configured `Budget` limits is reached.

Restoring a Package
-------------------

On the offline machine, hand the parsed package to `load` (also available
as `unpack`) to get a `Restored` object whose ``registry`` and ``root``
behave like the originals:

.. testcode::

    restored = packaging.load(json.loads(serialized))
    validator = validators.Draft202012Validator(
        restored.root,
        registry=restored.registry,
    )
    print(validator.is_valid({"value": 37}))

.. testoutput::

    True

Each manifest entry is checked against its resource as the package is
loaded, so a corrupted or tampered package raises `InvalidPackage`
before any validation is attempted.

Loading only ever reads the data in the package.
The original registry and root schema are not modified, and the restored
registry has no retriever configured, so nothing can be fetched
remotely.
