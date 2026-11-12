"""
Single-pass validation of JSON arrays represented as streams.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import pickle
import tempfile

from jsonschema import _utils, exceptions

_STREAM_KEYWORDS = frozenset(
    [
        "contains",
        "items",
        "maxContains",
        "maxItems",
        "minContains",
        "minItems",
        "prefixItems",
        "uniqueItems",
    ],
)
_ANNOTATION_KEYWORDS = frozenset(
    [
        "$anchor",
        "$comment",
        "$defs",
        "$dynamicAnchor",
        "$id",
        "$schema",
        "$vocabulary",
        "default",
        "definitions",
        "deprecated",
        "description",
        "examples",
        "id",
        "readOnly",
        "title",
        "writeOnly",
    ],
)
_SUPPORTED_KEYWORDS = _STREAM_KEYWORDS | _ANNOTATION_KEYWORDS

_DEFAULT_MEMORY_LIMIT = 1024 * 1024
_LENGTH_BYTES = 8


def _unsupported_schema(schema):
    return exceptions.SchemaError(
        "validate_array_stream supports only the items, prefixItems, "
        "minItems, maxItems, contains, minContains, maxContains and "
        f"uniqueItems keywords; got {schema!r}",
    )


def _check_stream_schema(schema, validator):
    if schema is True:
        return (), "prefixItems", True
    if schema is False:
        raise _unsupported_schema(schema)
    if not isinstance(schema, Mapping):
        raise _unsupported_schema(schema)

    unsupported = schema.keys() - _SUPPORTED_KEYWORDS
    if unsupported:
        raise _unsupported_schema(schema)

    items = schema.get("items", True)
    prefix_items = schema.get("prefixItems", ())
    prefix_keyword = "prefixItems"

    if isinstance(items, Sequence) and not isinstance(
        items,
        (str, bytes, bytearray),
    ):
        if "prefixItems" in schema:
            raise _unsupported_schema(schema)
        prefix_items = items
        prefix_keyword = "items"
        items = True
    elif not isinstance(items, (bool, Mapping)):
        raise _unsupported_schema(schema)

    if not isinstance(prefix_items, Sequence) or isinstance(
        prefix_items,
        (str, bytes, bytearray),
    ):
        raise _unsupported_schema(schema)
    if any(not isinstance(each, (bool, Mapping)) for each in prefix_items):
        raise _unsupported_schema(schema)

    if "contains" in schema and not isinstance(
        schema["contains"],
        (bool, Mapping),
    ):
        raise _unsupported_schema(schema)

    for keyword in ["minItems", "maxItems", "minContains", "maxContains"]:
        value = schema.get(keyword)
        if value is None:
            continue
        if not validator.TYPE_CHECKER.is_type(value, "integer") or value < 0:
            raise _unsupported_schema(schema)

    if "uniqueItems" in schema and not isinstance(
        schema["uniqueItems"],
        bool,
    ):
        raise _unsupported_schema(schema)

    return prefix_items, prefix_keyword, items


def _estimate_size(element):
    try:
        return len(_serialize(element))
    except Exception:  # noqa: BLE001
        return len(repr(element))


def _canonicalize(value):
    if isinstance(value, float) and value != value:
        return "nan", id(value)
    if isinstance(value, Mapping):
        items = sorted(
            (
                (_canonicalize(key), _canonicalize(item))
                for key, item in value.items()
            ),
            key=_serialize,
        )
        return "mapping", items
    if isinstance(value, frozenset):
        items = sorted(
            (_canonicalize(item) for item in value),
            key=_serialize,
        )
        return frozenset(items)
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return "sequence", tuple(_canonicalize(item) for item in value)
    return value


def _serialize(value):
    return pickle.dumps(
        _canonicalize(value),
        protocol=pickle.HIGHEST_PROTOCOL,
    )


class _BudgetedUniqueStore:
    """
    Track previously seen stream elements within a memory budget.

    Once the budget is exhausted, previously seen elements are staged in a
    temporary file. Uniqueness checks continue in either mode.
    """

    def __init__(self, memory_limit, temp_dir):
        self._memory_limit = memory_limit
        self._temp_dir = temp_dir
        self._keys = set()
        self._elements = []
        self._size = 0
        self._file = None
        self._spilled = False

    def add(self, element):
        key = _utils._uniq_key(element)
        if key is _utils._UNSUPPORTED:
            return self._add_unsupported(element)
        return self._add_key(key)

    def _add_unsupported(self, element):
        if self._file is None:
            for previous in self._elements:
                if _utils.equal(previous, element):
                    return False
            size = _estimate_size(element)
            if (
                self._memory_limit is not None
                and self._size + size > self._memory_limit
            ):
                self._spill()
            else:
                self._elements.append(element)
                self._size += size
                return True

        data = _serialize(element)
        if self._contains(data):
            return False
        self._write(data)
        return True

    def _add_key(self, key):
        if self._file is None:
            if key in self._keys:
                return False
            size = _estimate_size(key)
            if (
                self._memory_limit is not None
                and self._size + size > self._memory_limit
            ):
                self._spill()
            else:
                self._keys.add(key)
                self._size += size
                return True

        data = _serialize(key)
        if self._contains(data):
            return False
        self._write(data)
        return True

    def _spill(self):
        self._file = tempfile.TemporaryFile(dir=self._temp_dir)
        self._spilled = True
        for key in self._keys:
            self._write(_serialize(key))
        for element in self._elements:
            self._write(_serialize(element))
        self._keys.clear()
        self._elements.clear()
        self._size = 0

    def _write(self, data):
        self._file.write(len(data).to_bytes(_LENGTH_BYTES, "big"))
        self._file.write(data)

    def _contains(self, data):
        self._file.seek(0)
        offset = 0
        end = self._file.seek(0, 2)
        while offset < end:
            self._file.seek(offset)
            size = int.from_bytes(
                self._file.read(_LENGTH_BYTES),
                "big",
            )
            stored = self._file.read(size)
            if size == len(data) and stored == data:
                return True
            offset = self._file.tell()
        return False

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
        self._keys.clear()
        self._elements.clear()

    @property
    def spilled(self):
        return self._spilled


class ArrayStreamValidationResult:
    """
    The result of validating a one-pass iterable as a JSON array.

    ``complete`` is true only when the iterable reached its normal end.
    If iteration raised, ``complete`` is false and ``source_error`` contains
    the exception before it is re-raised.
    """

    def __init__(
        self,
        validator,
        iterable,
        memory_limit,
        temp_dir,
        stream_items,
    ):
        self._validator = validator
        self._schema = {} if validator.schema is True else validator.schema
        self._iterator = iter(iterable)
        self._unique_store = _BudgetedUniqueStore(memory_limit, temp_dir)
        prefix_items, prefix_keyword, items = stream_items

        self.errors = []
        self.complete = False
        self.source_error = None
        self.item_count = 0
        self.contains_count = 0
        self._stopped = False

        self._prefix_items = prefix_items
        self._prefix_keyword = prefix_keyword
        self._items = items
        self._contains = self._schema.get("contains")
        self._min_contains = self._schema.get("minContains", 1)
        self._max_contains = self._schema.get("maxContains")
        self._unique_items = self._schema.get("uniqueItems", False)
        self._max_items_failed = False
        self._max_contains_failed = False

        self._contains_validator = None
        if self._contains is not None:
            self._contains_validator = validator.evolve(schema=self._contains)

    def __iter__(self):
        return iter(self.errors)

    def __len__(self):
        return len(self.errors)

    @property
    def stopped_early(self):
        """
        Whether iteration stopped before reaching normal EOF.

        This is true for a source exception; a source which is closed
        before EOF reports this through its own termination exception.
        """
        return not self._stopped

    @property
    def used_temporary_storage(self):
        return self._unique_store.spilled

    @property
    def incomplete(self):
        return not self.complete

    @property
    def valid(self):
        return self.complete and not self.errors

    def run(self):
        try:
            while True:
                try:
                    item = next(self._iterator)
                except StopIteration:
                    self._stopped = True
                    break
                except BaseException as error:  # noqa: BLE001
                    if not isinstance(
                        error,
                        (KeyboardInterrupt, SystemExit),
                    ):
                        self.source_error = error
                    raise
                self._validate_item(item)
            self._finish()
        finally:
            self._unique_store.close()
        return self

    def _validate_item(self, item):
        index = self.item_count
        self.item_count += 1

        self._validate_items_keyword(item, index)
        self._validate_max_items(index)
        self._validate_contains(item)
        self._validate_unique(item, index)

    def _validate_items_keyword(self, item, index):
        if index < len(self._prefix_items):
            subschema = self._prefix_items[index]
            schema_path = (self._prefix_keyword, index)
        elif self._items is False:
            extra = self.item_count - len(self._prefix_items)
            self._append_error(
                f"Expected at most {len(self._prefix_items)} "
                f"{'item' if len(self._prefix_items) == 1 else 'items'} "
                f"but found {extra} extra: {item!r}",
                validator="items",
                validator_value=False,
                instance=item,
                path=(index,),
                schema_path=("items",),
            )
            return
        else:
            subschema = self._items
            schema_path = ("items",)

        self._descend(item, subschema, index, schema_path)

    def _validate_max_items(self, index):
        max_items = self._schema.get("maxItems")
        if (
            max_items is not None
            and index >= max_items
            and not self._max_items_failed
        ):
            self._max_items_failed = True
            message = (
                "is expected to be empty" if max_items == 0 else "is too long"
            )
            self._append_error(
                f"Stream with more than {index} items {message}",
                validator="maxItems",
                validator_value=max_items,
                path=(),
                schema_path=("maxItems",),
            )

    def _validate_contains(self, item):
        if self._contains_validator is None:
            return
        if self._contains_validator.is_valid(item):
            self.contains_count += 1
            if (
                self._max_contains is not None
                and self.contains_count > self._max_contains
                and not self._max_contains_failed
            ):
                self._max_contains_failed = True
                self._append_error(
                    "Too many items match the given schema "
                    f"(expected at most {self._max_contains})",
                    validator="maxContains",
                    validator_value=self._max_contains,
                    path=(),
                    schema_path=("maxContains",),
                )

    def _validate_unique(self, item, index):
        if not self._unique_items:
            return
        if not self._unique_store.add(item):
            self._append_error(
                "Stream has non-unique elements",
                validator="uniqueItems",
                validator_value=True,
                instance=item,
                path=(index,),
                schema_path=("uniqueItems",),
            )

    def _descend(self, item, subschema, index, schema_path):
        if subschema is True:
            return

        for error in self._validator.descend(item, subschema, path=index):
            for element in reversed(schema_path):
                error.schema_path.appendleft(element)
            self.errors.append(error)

    def _append_error(
        self,
        message,
        *,
        validator,
        validator_value,
        instance=exceptions._unset,
        schema=exceptions._unset,
        path,
        schema_path,
    ):
        if schema is exceptions._unset:
            schema = self._schema

        self.errors.append(
            exceptions.ValidationError(
                message,
                validator=validator,
                validator_value=validator_value,
                instance=instance,
                schema=schema,
                path=path,
                schema_path=schema_path,
                type_checker=self._validator.TYPE_CHECKER,
            ),
        )

    def _finish(self):
        self.complete = True
        self._validate_min_items()
        self._validate_min_contains()

    def _validate_min_items(self):
        min_items = self._schema.get("minItems")
        if min_items is not None and self.item_count < min_items:
            message = (
                "should be non-empty" if min_items == 1 else "is too short"
            )
            self._append_error(
                f"Stream with {self.item_count} items {message}",
                validator="minItems",
                validator_value=min_items,
                path=(),
                schema_path=("minItems",),
            )

    def _validate_min_contains(self):
        if self._contains is None:
            return
        if self.contains_count >= self._min_contains:
            return

        if self.contains_count == 0:
            message = "Stream does not contain items matching the given schema"
            validator = "contains"
            validator_value = self._contains
        else:
            message = (
                "Too few items match the given schema (expected at least "
                f"{self._min_contains} but only {self.contains_count} matched)"
            )
            validator = "minContains"
            validator_value = self._min_contains
        self._append_error(
            message,
            validator=validator,
            validator_value=validator_value,
            path=(),
            schema_path=(validator,),
        )


def validate_array_stream(
    instance,
    schema,
    cls=None,
    *args,
    memory_limit=_DEFAULT_MEMORY_LIMIT,
    temp_dir=None,
    spill_threshold=None,
    **kwargs,
):
    """
    Validate a one-pass iterable as a JSON array.

    This function supports a deliberately small, streaming-friendly subset of
    JSON Schema: :kw:`items`, :kw:`prefixItems`, :kw:`minItems`,
    :kw:`maxItems`, :kw:`contains`, :kw:`minContains`, :kw:`maxContains` and
    :kw:`uniqueItems`. Schemas using other validation keywords are rejected
    before the iterable is read.

    The returned `ArrayStreamValidationResult` contains all validation errors
    with source indices in their paths. Its ``complete`` attribute is true
    only if the iterable reached normal EOF. If iteration raises, the result
    is marked incomplete, temporary staging is cleaned up, and the original
    exception is re-raised.

    ``memory_limit`` bounds the memory used to remember values for
    :kw:`uniqueItems`. Beyond that budget, a temporary file under ``temp_dir``
    is used so uniqueness checks remain available. ``spill_threshold`` is an
    alias for ``memory_limit``.
    """
    if cls is None:
        from jsonschema.validators import validator_for

        cls = validator_for(schema)

    cls.check_schema(schema)
    stream_items = _check_stream_schema(schema, cls)

    if spill_threshold is not None:
        memory_limit = spill_threshold
    if memory_limit is not None and memory_limit < 0:
        raise ValueError("memory_limit must be non-negative or None")

    validator = cls(schema, *args, **kwargs)
    result = ArrayStreamValidationResult(
        validator,
        instance,
        memory_limit,
        temp_dir,
        stream_items,
    )
    return result.run()
