"""
Packaging of JSON Schema registries for offline distribution.

This module turns an explicit `~referencing.Registry`, together with a
schema to use as the root resource, into a single JSON document -- a
*schema package* -- which carries both the schemas themselves and a
*manifest* describing each of them.

Packages are restored into a working `~referencing.Registry` with
`load` (also available as `unpack`), without any network or filesystem
access, and without modifying the registry or root schema from which
they were created.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha256
from json import dumps
from urllib.parse import urldefrag, urljoin

from attrs import frozen
import referencing
import referencing.jsonschema

from jsonschema import _utils

#: The name of the format of the packages produced by this module.
FORMAT = "jsonschema-package"

#: The version of the package format this module reads and writes.
VERSION = 1

#: The digest algorithm recorded in the manifest for each resource.
DIGEST_ALGORITHM = "sha256"

#: The default maximum number of resources a package may contain.
DEFAULT_MAX_RESOURCES = 512

#: The default maximum total size of a package, in bytes.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: The default maximum depth of nested subschemas to traverse.
DEFAULT_MAX_DEPTH = 64

#: Keywords which may hold references in any supported dialect.
_REFERENCES = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})

#: Specification (draft) objects keyed by their manifest name.
_SPECIFICATIONS = {
    each.name: each
    for each in (
        referencing.jsonschema.DRAFT3,
        referencing.jsonschema.DRAFT4,
        referencing.jsonschema.DRAFT6,
        referencing.jsonschema.DRAFT7,
        referencing.jsonschema.DRAFT201909,
        referencing.jsonschema.DRAFT202012,
    )
}


class PackageError(Exception):
    """
    A package could not be created, verified, or restored.
    """


@frozen
class ConflictingResource(PackageError):
    """
    Multiple different resources were found under the same URI.
    """

    uri: str

    def __str__(self):
        return (
            f"{self.uri!r} is present with more than one set of contents."
        )


@frozen
class MissingResource(PackageError):
    """
    A reference points to a resource which is not present in the input.
    """

    uri: str
    ref: str
    pointer: str

    def __str__(self):
        return (
            f"{self.ref!r} (at {self.pointer!r}) references {self.uri!r}, "
            "which is not present in the registry and would need to be "
            "retrieved; packages never retrieve remote resources."
        )


@frozen
class BudgetExceeded(PackageError):
    """
    Packaging or restoring the input would exceed the configured budget.
    """

    message: str

    def __str__(self):
        return self.message


@frozen
class InvalidPackage(PackageError):
    """
    The given package is malformed or could not be verified.
    """

    message: str

    def __str__(self):
        return self.message


@frozen
class Restored:
    """
    The result of restoring a package.

    Attributes:
        registry:
            A new registry containing all of the packaged resources.
        root:
            The (deep-copied) root schema, ready to validate against.
        root_uri:
            The URI of the root resource within ``registry``.
    """

    registry: referencing.Registry
    root: referencing.jsonschema.Schema
    root_uri: str

    @property
    def resolver(self):
        """
        A resolver resolving references against the restored root.
        """
        return self.registry.resolver_with_root(
            self.registry[self.root_uri],
        )


@frozen
class Budget:
    """
    Limits applied while creating or restoring a package.

    Every limit is counted on the package being created or restored, so
    that unbounded or unexpectedly large inputs fail loudly rather than
    being loaded blindly.
    """

    max_resources: int = DEFAULT_MAX_RESOURCES
    max_bytes: int = DEFAULT_MAX_BYTES
    max_depth: int = DEFAULT_MAX_DEPTH

    def check_resources(self, count):
        if count > self.max_resources:
            raise BudgetExceeded(
                message=(
                    f"Package contains {count} resources, which exceeds the "
                    f"limit of {self.max_resources}."
                ),
            )

    def check_bytes(self, size):
        if size > self.max_bytes:
            raise BudgetExceeded(
                message=(
                    f"Package is {size} bytes, which exceeds the limit of "
                    f"{self.max_bytes} bytes."
                ),
            )

    def check_depth(self, depth, pointer):
        if depth > self.max_depth:
            raise BudgetExceeded(
                message=(
                    f"Schema nesting is deeper than the limit of "
                    f"{self.max_depth} subschemas while reaching "
                    f"{pointer!r}."
                ),
            )


def _to_plain(value):
    """
    Return a JSON-native deep copy of schema contents.

    Schemas in a registry may contain immutable ``rpds`` containers, which
    are not directly JSON serializable; converting them also guarantees
    that packaging cannot mutate or share mutable state with the caller.
    """
    if isinstance(value, Mapping):
        return {key: _to_plain(each) for key, each in value.items()}
    if (
        isinstance(value, Iterable)
        and not isinstance(value, (str, bytes, bytearray))
    ):
        return [_to_plain(each) for each in value]
    return value


def _canonical_json(contents):
    """
    Serialize contents deterministically for hashing and size accounting.
    """
    return dumps(
        contents,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _digest(contents):
    return sha256(_canonical_json(contents).encode("utf-8")).hexdigest()


def _pointer_segment(segment):
    return str(segment).replace("~", "~0").replace("/", "~1")


def _walk_references(contents, base, pointer, depth, budget, found):
    """
    Find references within ``contents``, resolving them against ``base``.

    ``found`` is updated with ``(uri, ref, pointer)`` triples for each
    reference encountered, where ``uri`` is the reference resolved
    against the base URI of the containing resource (with any fragment
    stripped, since fragments identify locations *within* resources).
    """
    budget.check_depth(depth, pointer)
    if isinstance(contents, Mapping):
        for key, value in contents.items():
            if key in _REFERENCES and isinstance(value, str):
                uri, _ = urldefrag(urljoin(base, value))
                found.append(
                    (uri, value, f"{pointer}/{_pointer_segment(key)}"),
                )
            else:
                _walk_references(
                    value,
                    base,
                    f"{pointer}/{_pointer_segment(key)}",
                    depth + 1,
                    budget,
                    found,
                )
    elif isinstance(contents, list):
        for index, value in enumerate(contents):
            _walk_references(
                value,
                base,
                f"{pointer}/{index}",
                depth + 1,
                budget,
                found,
            )


def _references_of(resource, base, budget):
    found = []
    _walk_references(resource.contents, base, "#", 0, budget, found)
    return found


def _resource_manifest(resource):
    """
    Describe a single resource for inclusion in the manifest.
    """
    contents = _to_plain(resource.contents)
    id = resource.id()
    anchors = []
    dynamic_anchors = []
    for anchor in resource.anchors():
        if isinstance(anchor, referencing.jsonschema.DynamicAnchor):
            dynamic_anchors.append(anchor.name)
        else:
            anchors.append(anchor.name)
    subresources = []
    for subresource in resource.subresources():
        subresource_id = subresource.id()
        if subresource_id is not None:
            subresources.append(subresource_id)
    return {
        "id": id,
        "specification": resource._specification.name,
        "anchors": sorted(anchors),
        "dynamicAnchors": sorted(dynamic_anchors),
        "subresources": sorted(subresources),
        "digest": _digest(contents),
        "size": len(_canonical_json(contents).encode("utf-8")),
    }


def _same_contents(one, two):
    """
    Compare two resources' contents, ignoring container representations.
    """
    return _utils.equal(
        _to_plain(one.contents),
        _to_plain(two.contents),
    )


def _base_uri(uri, resource):
    """
    Return the base URI references within a resource resolve against.

    This matches validation-time resolution: a resource's own internal
    id (e.g. its ``$id``) is used when it declares one, falling back to
    the URI the resource is registered under.
    """
    return resource.id() or uri


def _all_documents(registry, root_uri, root_resource, budget):
    """
    Collect every resource reachable from the root resource.

    Resources are discovered in the same two ways they are at validation
    time: via nested subresources (whose boundaries follow the
    specification of their containing resource, including dialect changes
    via ``$schema``), and via references whose targets the explicit
    registry already knows about.

    Returns a mapping of URI to resource. The registry's own definition
    of a URI always takes priority over a same-URI subresource embedded
    within another document, since that is what resolution would find.
    No retrieval of unknown URIs occurs; the registry is only read.
    """
    crawled = registry.crawl()
    known_root = crawled.get(root_uri)
    if known_root is not None and not _same_contents(
        known_root, root_resource
    ):
        raise ConflictingResource(uri=root_uri)

    documents = {}
    stack = [(root_uri, root_resource)]
    while stack:
        uri, resource = stack.pop()
        known = crawled.get(uri)
        if known is not None:
            resource = known
        existing = documents.get(uri)
        if existing is not None:
            if not _same_contents(existing, resource):
                raise ConflictingResource(uri=uri)
            continue
        documents[uri] = resource
        budget.check_resources(len(documents))
        base = _base_uri(uri, resource)

        for subresource in resource.subresources():
            subresource_id = subresource.id()
            # A subresource without an ID of its own is not a separate
            # document: its contents remain part of the containing resource
            # and is reached via fragments rather than its own URI.
            if subresource_id is not None:
                stack.append(
                    (urljoin(base, subresource_id), subresource),
                )

        for target, _ref, _pointer in _references_of(resource, base, budget):
            referenced = crawled.get(target) or registry.get(target)
            if referenced is not None:
                stack.append((target, referenced))
    return documents


def pack(registry, root, root_uri=None, budget=Budget()):
    """
    Build a JSON-serializable package from an explicit registry.

    Arguments:
        registry:
            The `referencing.Registry` containing the schemas which the
            root schema refers to. It is not modified.
        root:
            The schema to treat as the root resource. It is deep-copied
            into the package and is not modified.
        root_uri:
            An explicit URI for the root. If the root declares its own
            ``$id`` this may be omitted; otherwise it is required.
        budget:
            A `Budget` limiting the size of the produced package.

    Returns:
        A plain JSON object containing the packaged resources and a
        manifest, suitable for `json.dumps` and later `load`.
    """
    root_resource = referencing.Resource.from_contents(
        root,
        default_specification=referencing.jsonschema.DRAFT202012,
    )
    if root_uri is None:
        root_uri = root_resource.id()
        if root_uri is None:
            raise PackageError(
                "The root schema has no $id, so root_uri must be "
                "provided explicitly when packaging.",
            )
    root_uri = root_uri.rstrip("#")

    documents = _all_documents(
        registry=registry,
        root_uri=root_uri,
        root_resource=root_resource,
        budget=budget,
    )

    # A URI known to the registry but unreachable from the root is not
    # part of the package; the closure above only contains what the root
    # actually references. The registry itself is never mutated.
    _check_references(documents, budget)

    resources = []
    manifest = {}
    total = 0
    for uri in sorted(documents):
        resource = documents[uri]
        contents = _to_plain(resource.contents)
        canonical = _canonical_json(contents)
        total += len(canonical.encode("utf-8"))
        budget.check_bytes(total)
        resources.append({"uri": uri, "contents": contents})
        entry = _resource_manifest(resource)
        entry["uri"] = uri
        manifest[uri] = entry

    package = {
        "format": FORMAT,
        "version": VERSION,
        "digestAlgorithm": DIGEST_ALGORITHM,
        "root": root_uri,
        "resources": resources,
        "manifest": manifest,
    }
    serialized = dumps(package, ensure_ascii=False)
    budget.check_bytes(len(serialized.encode("utf-8")))
    return package


def _check_references(documents, budget):
    """
    Ensure every external reference points at a packaged resource.

    References beginning with ``#`` stay within the containing resource
    and therefore never need a separate document. Only the resources
    being packaged are consulted; no retrieval from the registry's
    retriever (and therefore no network access) occurs.
    """
    for uri, resource in documents.items():
        base = _base_uri(uri, resource)
        for target, ref, pointer in _references_of(resource, base, budget):
            if ref.startswith("#"):
                continue
            if target not in documents:
                raise MissingResource(uri=target, ref=ref, pointer=pointer)


def _require(package, key, pointer):
    if not isinstance(package, Mapping) or key not in package:
        raise InvalidPackage(
            message=f"Package is missing required key {pointer!r}.",
        )
    return package[key]


def _check_structure(package):
    if not isinstance(package, Mapping):
        raise InvalidPackage(
            message="A package must be a JSON object.",
        )
    if package.get("format") != FORMAT:
        raise InvalidPackage(
            message=(
                f"Package has format {package.get('format')!r}, but only "
                f"{FORMAT!r} packages are supported."
            ),
        )
    if package.get("version") != VERSION:
        raise InvalidPackage(
            message=(
                f"Package has version {package.get('version')!r}, but only "
                f"version {VERSION} is supported."
            ),
        )
    if package.get("digestAlgorithm") != DIGEST_ALGORITHM:
        raise InvalidPackage(
            message=(
                f"Package uses digest algorithm "
                f"{package.get('digestAlgorithm')!r}, but only "
                f"{DIGEST_ALGORITHM!r} is supported."
            ),
        )


def load(package, budget=Budget()):
    """
    Restore a package into a usable registry and root schema.

    Only the data contained in ``package`` is used. Nothing is fetched
    over the network or read from disk, and ``package`` itself is not
    modified; the returned schema is a separate, deep copy.

    Arguments:
        package:
            A package previously produced by `pack` (or parsed from its
            JSON serialization).
        budget:
            A `Budget` limiting how much data may be restored.
    """
    _check_structure(package)

    resources = _require(package, "resources", "/resources")
    manifest = _require(package, "manifest", "/manifest")
    root_uri = _require(package, "root", "/root")
    if not isinstance(root_uri, str):
        raise InvalidPackage(
            message="Package key /root must be a string.",
        )
    if not isinstance(resources, list):
        raise InvalidPackage(
            message="Package key /resources must be a list.",
        )
    if not isinstance(manifest, Mapping):
        raise InvalidPackage(
            message="Package key /manifest must be an object.",
        )

    budget.check_resources(len(resources))
    total = 0
    pairs = []
    uris = set()
    for index, entry in enumerate(resources):
        if not isinstance(entry, Mapping):
            raise InvalidPackage(
                message=f"Resource at /resources/{index} must be an object.",
            )
        uri = entry.get("uri")
        if not isinstance(uri, str):
            raise InvalidPackage(
                message=(
                    f"Resource at /resources/{index} has no string 'uri'."
                ),
            )
        if uri in uris:
            raise InvalidPackage(
                message=f"Resource at /resources/{index} repeats {uri!r}.",
            )
        uris.add(uri)
        if "contents" not in entry:
            raise InvalidPackage(
                message=(
                    f"Resource at /resources/{index} is missing 'contents'."
                ),
            )
        contents = _to_plain(entry["contents"])
        canonical = _canonical_json(contents)
        total += len(canonical.encode("utf-8"))
        budget.check_bytes(total)

        if uri not in manifest:
            raise InvalidPackage(
                message=(
                    f"Resource {uri!r} has no corresponding manifest entry."
                ),
            )
        described = manifest[uri]
        if not isinstance(described, Mapping):
            raise InvalidPackage(
                message=f"Manifest entry for {uri!r} must be an object.",
            )
        actual = _digest(contents)
        expected = described.get("digest")
        if expected != actual:
            raise InvalidPackage(
                message=(
                    f"Manifest digest for {uri!r} does not match its "
                    f"contents: expected {expected!r}, found {actual!r}."
                ),
            )

        specification_name = described.get("specification")
        specification = _SPECIFICATIONS.get(specification_name)
        if specification is None:
            raise InvalidPackage(
                message=(
                    f"Manifest entry for {uri!r} names an unsupported "
                    f"specification {specification_name!r}."
                ),
            )
        resource = specification.create_resource(contents)
        _check_manifest_entry(uri, described, resource)
        pairs.append((uri, resource))

    if root_uri not in uris:
        raise InvalidPackage(
            message=(
                f"Root {root_uri!r} is not among the packaged resources."
            ),
        )
    if set(manifest) != uris:
        raise InvalidPackage(
            message=(
                "The manifest does not describe exactly the packaged "
                f"resources: extra entries {sorted(set(manifest) - uris)!r}, "
                f"missing entries {sorted(uris - set(manifest))!r}."
            ),
        )

    registry = referencing.Registry().with_resources(pairs).crawl()
    root = registry.contents(root_uri)
    return Restored(registry=registry, root=root, root_uri=root_uri)


#: A readable alias for `load`, matching the pack/unpack naming pair.
unpack = load


def _check_manifest_entry(uri, described, resource):
    """
    Verify a manifest entry describes the resource it claims to.
    """
    actual = _resource_manifest(resource)
    for key in (
        "id",
        "anchors",
        "dynamicAnchors",
        "subresources",
        "size",
    ):
        if described.get(key) != actual[key]:
            raise InvalidPackage(
                message=(
                    f"Manifest entry for {uri!r} claims {key} "
                    f"{described.get(key)!r}, but the resource itself has "
                    f"{actual[key]!r}."
                ),
            )
