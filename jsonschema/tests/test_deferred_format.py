"""
Tests for the deferred ``format`` protocol in `jsonschema._deferred`.

The protocol validates locally first, returns a data-only plan of external
checks, and only concludes once the caller supplies results. The library
itself never performs network access.
"""

from unittest import TestCase
import json

from jsonschema import (
    DeferredFormatChecker,
    DeferredFormatError,
    DeferredValidationError,
    FormatChecker,
    FormatCheckPlan,
    FormatCheckResult,
)
from jsonschema._deferred import (
    INVALID,
    PENDING,
    VALID,
    prepare,
    resolve,
    validate,
)
from jsonschema.validators import Draft202012Validator


def _digest(value):
    from jsonschema._deferred import _digest

    return _digest(value)


def _sync_checker():
    """
    A synchronous checker with the same truth table as the fake directory.
    """
    checker = FormatChecker(formats=())
    checker.checks("ext")(
        lambda instance: not isinstance(instance, str)
        or instance.startswith("ok-"),
    )
    return checker


def _truth_map(instance):
    """
    The fake external directory: strings conform iff they start with ok-.
    """
    truth = {}

    def walk(value):
        if isinstance(value, dict):
            for each in value.values():
                walk(each)
        elif isinstance(value, list):
            for each in value:
                walk(each)
        elif isinstance(value, str):
            truth[_digest(value)] = value.startswith("ok-")

    walk(instance)
    return truth


class FakeExternalDirectory:
    """
    A local, in-memory stand-in for an external format directory.

    It answers checks in batches, exactly like a caller would after having
    transported the plan to some external system.
    """

    def __init__(self, instance):
        self._truth = _truth_map(instance)
        self.batches = 0

    def check_batch(self, requests):
        self.batches += 1
        return [
            FormatCheckResult(
                id=request.id,
                conforms=self._truth.get(request.instance_digest, True),
                error=(
                    None
                    if self._truth.get(request.instance_digest, True)
                    else f"directory rejected {request.path}"
                ),
            )
            for request in requests
        ]


def _rows(errors):
    return sorted(
        (
            tuple(each.absolute_path),
            tuple(each.absolute_schema_path),
            each.validator,
            each.message,
            sorted(
                (
                    tuple(child.absolute_path),
                    child.validator,
                    child.message,
                )
                for child in each.context
            ),
        )
        for each in errors
    )


def _normalized(rows):
    return tuple(
        sorted(
            (
                path,
                schema_path,
                validator,
                "<format>" if validator == "format" else message,
                tuple(
                    sorted(
                        (
                            child_path,
                            child_validator,
                            "<format>"
                            if child_validator == "format"
                            else child_message,
                        )
                        for child_path, child_validator, child_message in ctx
                    ),
                ),
            )
            for path, schema_path, validator, message, ctx in rows
        ),
    )


def _sync_errors(instance, schema):
    validator = Draft202012Validator(schema, format_checker=_sync_checker())
    return _normalized(_rows(validator.iter_errors(instance)))


def _deferred_errors(instance, schema, results=None):
    engine, plan = prepare(instance, schema, format_checker={"ext"})
    if results is None:
        directory = FakeExternalDirectory(instance)
        results = directory.check_batch(plan.checks)
    outcome = resolve(
        instance,
        schema,
        plan,
        results,
        format_checker={"ext"},
    )
    return _normalized(_rows(outcome.errors))


class TestDeferredParity(TestCase):
    """
    Deferred conclusions must match fully synchronous validation.
    """

    schemas = [
        {"format": "ext"},
        {
            "type": "object",
            "properties": {
                "a": {"format": "ext"},
                "b": {"format": "ext"},
            },
            "required": ["a", "b"],
        },
        {
            "anyOf": [{"format": "ext"}, {"minLength": 5}],
        },
        {
            "oneOf": [{"format": "ext"}, {"maxLength": 10}],
        },
        {
            "if": {"format": "ext"},
            "then": {"minLength": 20},
            "else": {"maxLength": 2},
        },
        {"not": {"format": "ext"}},
        {"type": "array", "items": {"format": "ext"}},
        {"allOf": [{"format": "ext"}, {"maxLength": 3}]},
        {
            "type": "object",
            "properties": {
                "a": {"anyOf": [{"format": "ext"}, {"type": "integer"}]},
            },
        },
        {"contains": {"format": "ext"}},
        {
            "$ref": "#/$defs/x",
            "$defs": {"x": {"format": "ext"}},
        },
        {
            "unevaluatedProperties": False,
            "properties": {"a": {"format": "ext"}},
        },
        {"unevaluatedItems": False, "prefixItems": [{"format": "ext"}]},
        {
            "unevaluatedProperties": {"format": "ext"},
        },
    ]

    instances = [
        "ok-x",
        "bad",
        "ok-verylongstring",
        12,
        ["ok-x", "bad"],
        {"a": "ok-x", "b": "bad"},
        {"a": "bad", "b": "ok-x"},
    ]

    def test_conclusions_and_error_paths_match_synchronous_checks(self):
        for schema in self.schemas:
            for instance in self.instances:
                with self.subTest(schema=schema, instance=instance):
                    self.assertEqual(
                        _sync_errors(instance, schema),
                        _deferred_errors(instance, schema),
                    )


class TestPlan(TestCase):
    def test_plan_is_bound_and_data_only(self):
        schema = {"properties": {"a": {"format": "ext"}}}
        instance = {"a": "bad"}
        _, plan = prepare(instance, schema, format_checker={"ext"})

        self.assertEqual(plan.instance_digest, _digest(instance))
        self.assertEqual(plan.schema_digest, _digest(schema))
        request = plan.checks[0]
        self.assertEqual(request.path, "$['a']")
        self.assertEqual(request.format, "ext")
        self.assertEqual(request.instance_digest, _digest("bad"))

        data = plan.to_data()
        json.dumps(data)

        def walk(value):
            if isinstance(value, dict):
                for each in value.values():
                    walk(each)
            elif isinstance(value, list):
                for each in value:
                    walk(each)
            else:
                self.assertIsInstance(
                    value,
                    (str, int, float, bool, type(None)),
                )

        walk(data)
        self.assertEqual(FormatCheckPlan.from_data(data), plan)

    def test_identical_requests_are_deduplicated(self):
        schema = {
            "allOf": [
                {"properties": {"a": {"format": "ext"}}},
                {"properties": {"a": {"format": "ext"}}},
            ],
        }
        _, plan = prepare({"a": "bad"}, schema, format_checker={"ext"})
        self.assertEqual(len(plan.checks), 1)

    def test_no_plan_when_no_external_checks_are_needed(self):
        _, plan = prepare(
            "anything",
            {"type": "string"},
            format_checker={"ext"},
        )
        self.assertEqual(plan.checks, ())
        self.assertFalse(plan.pending)


class TestResultValidation(TestCase):
    def setUp(self):
        self.schema = {"format": "ext"}
        self.instance = "bad"
        _, self.plan = prepare(
            self.instance,
            self.schema,
            format_checker={"ext"},
        )
        self.request_id = self.plan.checks[0].id

    def _resolve(self, results, instance=None, schema=None, plan=None):
        return resolve(
            self.instance if instance is None else instance,
            self.schema if schema is None else schema,
            self.plan if plan is None else plan,
            results,
            format_checker={"ext"},
        )

    def test_missing_results_are_rejected(self):
        with self.assertRaises(DeferredFormatError) as ctx:
            self._resolve([])
        self.assertIn("Missing external results", str(ctx.exception))

    def test_duplicated_results_are_rejected(self):
        results = [
            {"id": self.request_id, "conforms": True},
            {"id": self.request_id, "conforms": False},
        ]
        with self.assertRaises(DeferredFormatError) as ctx:
            self._resolve(results)
        self.assertIn("more than once", str(ctx.exception))

    def test_conflicting_results_are_rejected(self):
        # The same id arriving twice with different conclusions is a
        # conflict, and conflicts are rejected like any duplication.
        results = [
            FormatCheckResult(id=self.request_id, conforms=True),
            FormatCheckResult(id=self.request_id, conforms=False),
        ]
        with self.assertRaises(DeferredFormatError):
            self._resolve(results)

    def test_results_from_another_plan_are_rejected(self):
        _, other_plan = prepare("zzz", self.schema, format_checker={"ext"})
        other_id = other_plan.checks[0].id
        with self.assertRaises(DeferredFormatError) as ctx:
            self._resolve([{"id": other_id, "conforms": True}])
        self.assertIn("does not belong to this plan", str(ctx.exception))

    def test_results_for_a_different_instance_are_rejected(self):
        with self.assertRaises(DeferredFormatError) as ctx:
            self._resolve(
                [{"id": self.request_id, "conforms": True}],
                instance="other",
            )
        self.assertIn("instance", str(ctx.exception))

    def test_results_for_a_different_schema_are_rejected(self):
        with self.assertRaises(DeferredFormatError) as ctx:
            self._resolve(
                [{"id": self.request_id, "conforms": True}],
                schema={"format": "ext", "minLength": 1},
            )
        self.assertIn("schema", str(ctx.exception))

    def test_non_boolean_conforms_is_rejected(self):
        with self.assertRaises(DeferredFormatError):
            self._resolve([{"id": self.request_id, "conforms": "yes"}])

    def test_unknown_request_ids_are_rejected(self):
        with self.assertRaises(DeferredFormatError):
            self._resolve([{"id": "nope", "conforms": True}])

    def test_non_json_data_in_results_is_rejected(self):
        with self.assertRaises(DeferredFormatError):
            self._resolve([object()])


class TestPendingSemantics(TestCase):
    """
    An unknown external result is never treated as a pass or a failure.
    """

    def test_anyOf_stays_pending_until_results_arrive(self):
        schema = {"anyOf": [{"format": "ext"}, {"minLength": 100}]}
        outcome = validate("bad", schema, format_checker={"ext"})
        self.assertEqual(outcome.status, PENDING)
        self.assertFalse(outcome.is_valid)
        self.assertTrue(outcome.is_pending)
        self.assertEqual(outcome.errors, ())

        resolved = resolve(
            "bad",
            schema,
            outcome.plan,
            [{"id": outcome.plan.checks[0].id, "conforms": True}],
            format_checker={"ext"},
        )
        self.assertEqual(resolved.status, VALID)

        rejected = resolve(
            "bad",
            schema,
            outcome.plan,
            [
                {
                    "id": outcome.plan.checks[0].id,
                    "conforms": False,
                    "error": "nope",
                },
            ],
            format_checker={"ext"},
        )
        self.assertEqual(rejected.status, INVALID)

    def test_oneOf_with_undecided_branch_is_pending(self):
        # The second branch cannot match this instance, so only the
        # external result decides the outcome.
        schema = {"oneOf": [{"format": "ext"}, {"minLength": 100}]}
        outcome = validate("bad", schema, format_checker={"ext"})
        self.assertEqual(outcome.status, PENDING)

        request_id = outcome.plan.checks[0].id
        passing = resolve(
            "bad",
            schema,
            outcome.plan,
            [{"id": request_id, "conforms": True}],
            format_checker={"ext"},
        )
        self.assertEqual(passing.status, VALID)
        failing = resolve(
            "bad",
            schema,
            outcome.plan,
            [{"id": request_id, "conforms": False, "error": "nope"}],
            format_checker={"ext"},
        )
        self.assertEqual(failing.status, INVALID)

    def test_conditional_branches_stay_pending(self):
        schema = {
            "if": {"format": "ext"},
            "then": {"minLength": 100},
            "else": {"maxLength": 100},
        }
        outcome = validate("bad", schema, format_checker={"ext"})
        self.assertEqual(outcome.status, PENDING)
        self.assertEqual(outcome.errors, ())

        request_id = outcome.plan.checks[0].id
        resolved = resolve(
            "bad",
            schema,
            outcome.plan,
            [{"id": request_id, "conforms": True}],
            format_checker={"ext"},
        )
        self.assertEqual(resolved.status, INVALID)
        self.assertEqual(
            [each.message for each in resolved.errors],
            ["'bad' is too short"],
        )

    def test_unresolved_local_errors_are_invalid_without_results(self):
        schema = {"type": "integer", "format": "ext"}
        outcome = validate("bad", schema, format_checker={"ext"})
        self.assertEqual(outcome.status, INVALID)
        self.assertEqual(
            [each.message for each in outcome.errors],
            ["'bad' is not of type 'integer'"],
        )

    def test_local_errors_are_reported_immediately(self):
        schema = {"type": "integer", "format": "ext"}
        engine, plan = prepare("bad", schema, format_checker={"ext"})
        outcome = resolve(
            "bad",
            schema,
            plan,
            [{"id": plan.checks[0].id, "conforms": True}],
            format_checker={"ext"},
        )
        self.assertEqual(outcome.status, INVALID)
        self.assertEqual(
            [each.message for each in outcome.errors],
            ["'bad' is not of type 'integer'"],
        )

    def test_local_errors_are_preserved_alongside_external_failures(self):
        schema = {
            "type": "object",
            "properties": {"a": {"format": "ext"}},
            "required": ["missing"],
        }
        engine, plan = prepare({"a": "bad"}, schema, format_checker={"ext"})
        outcome = resolve(
            {"a": "bad"},
            schema,
            plan,
            [
                {
                    "id": plan.checks[0].id,
                    "conforms": False,
                    "error": "directory said no",
                },
            ],
            format_checker={"ext"},
        )
        self.assertEqual(outcome.status, INVALID)
        messages = sorted(each.message for each in outcome.errors)
        self.assertEqual(
            messages,
            ["'missing' is a required property", "directory said no"],
        )


class TestTwoBatchFlow(TestCase):
    """
    Results may arrive in multiple batches, like a remote directory lookup.
    """

    schema = {
        "type": "object",
        "properties": {
            "a": {"format": "ext"},
            "b": {"format": "ext"},
            "c": {"format": "ext"},
            "nested": {"items": {"format": "ext"}},
        },
    }
    instance = {
        "a": "ok-a",
        "b": "nope-1",
        "c": "nope-2",
        "nested": ["ok-nested", "nope-3"],
    }

    def test_resolution_after_two_batches_matches_synchronous(self):
        engine, plan = prepare(
            self.instance,
            self.schema,
            format_checker={"ext"},
        )
        directory = FakeExternalDirectory(self.instance)

        half = (len(plan.checks) + 1) // 2
        first_batch = directory.check_batch(plan.checks[:half])
        second_batch = directory.check_batch(plan.checks[half:])
        self.assertEqual(directory.batches, 2)

        outcome = resolve(
            self.instance,
            self.schema,
            plan,
            first_batch + second_batch,
            format_checker={"ext"},
        )
        self.assertEqual(outcome.status, INVALID)
        self.assertEqual(
            _normalized(_rows(outcome.errors)),
            _sync_errors(self.instance, self.schema),
        )
        self.assertEqual(
            sorted(tuple(each.absolute_path) for each in outcome.errors),
            [("b",), ("c",), ("nested", 1)],
        )

    def test_partial_batches_are_not_enough_to_finish(self):
        _, plan = prepare(self.instance, self.schema, format_checker={"ext"})
        directory = FakeExternalDirectory(self.instance)
        half = (len(plan.checks) + 1) // 2
        with self.assertRaises(DeferredFormatError):
            resolve(
                self.instance,
                self.schema,
                plan,
                directory.check_batch(plan.checks[:half]),
                format_checker={"ext"},
            )


class TestLocalCallbacksRunOnce(TestCase):
    def test_synchronous_format_callback_is_not_reinvoked_on_resolve(self):
        calls = []

        sync = FormatChecker(formats=())

        def check(instance):
            calls.append(instance)
            return not isinstance(instance, str) or instance.startswith("ok-")

        sync.checks("syncfmt")(check)

        schema = {
            "type": "object",
            "properties": {
                "a": {"format": "syncfmt"},
                "b": {"format": "ext"},
            },
        }
        instance = {"a": "ok-x", "b": "external"}
        engine, plan = prepare(
            instance,
            schema,
            format_checker={"ext"},
            sync_format_checker=sync,
        )
        self.assertEqual(calls, ["ok-x"])

        before_resolve = len(calls)
        outcome = engine.resolve(
            instance,
            plan,
            {plan.checks[0].id: (True, None)},
        )
        self.assertEqual(outcome.status, VALID)
        self.assertEqual(len(calls), before_resolve)

    def test_failing_synchronous_format_is_cached(self):
        calls = []

        sync = FormatChecker(formats=())

        def check(instance):
            calls.append(instance)
            return not isinstance(instance, str) or instance.startswith("ok-")

        sync.checks("syncfmt")(check)

        schema = {
            "type": "object",
            "properties": {"a": {"format": "syncfmt", "type": "string"}},
        }
        instance = {"a": "bad"}
        engine, plan = prepare(
            instance,
            schema,
            format_checker={"ext"},
            sync_format_checker=sync,
        )
        before = len(calls)
        outcome = engine.resolve(instance, plan, {})
        self.assertEqual(outcome.status, INVALID)
        self.assertEqual(len(calls), before)


class TestSynchronousCheckerUnchanged(TestCase):
    def test_ordinary_format_checker_is_unaffected(self):
        checker = FormatChecker()
        self.assertTrue(checker.conforms("anything", "no-such-format"))
        self.assertTrue(checker.conforms("1.2.3.4", "ipv4"))
        self.assertFalse(checker.conforms("not-an-ipv4", "ipv4"))
        Draft202012Validator(
            {"format": "ipv4"},
            format_checker=checker,
        ).validate("1.2.3.4")

    def test_deferred_checker_only_names_formats(self):
        checker = DeferredFormatChecker(["ext"])
        self.assertEqual(checker.formats, frozenset(["ext"]))
        self.assertTrue(checker.is_deferred("ext"))
        self.assertFalse(checker.is_deferred("ipv4"))
        self.assertEqual(
            repr(checker),
            "<DeferredFormatChecker formats=['ext']>",
        )

    def test_raise_for_errors(self):
        schema = {"format": "ext"}
        engine, plan = prepare("bad", schema, format_checker={"ext"})
        outcome = engine.resolve(
            "bad",
            plan,
            {plan.checks[0].id: (False, "directory rejected")},
        )
        with self.assertRaises(DeferredValidationError) as ctx:
            outcome.raise_for_errors()
        self.assertEqual(len(ctx.exception.errors), 1)


class TestNoNetworking(TestCase):
    def test_the_deferred_module_performs_no_network_access(self):
        import inspect

        import jsonschema._deferred

        source = inspect.getsource(jsonschema._deferred)
        for forbidden in (
            "urllib",
            "urlopen",
            "requests.get",
            "requests.post",
            "http.client",
            "socket",
            "aiohttp",
        ):
            self.assertNotIn(forbidden, source)

    def test_results_and_plans_hold_no_callables(self):
        _, plan = prepare(
            {"a": "bad"},
            {"properties": {"a": {"format": "ext"}}},
            format_checker={"ext"},
        )

        def assert_data(value):
            self.assertFalse(callable(value))
            if isinstance(value, dict):
                for each in value.values():
                    assert_data(each)
            elif isinstance(value, (list, tuple)):
                for each in value:
                    assert_data(each)

        assert_data(plan.to_data())
        result = FormatCheckResult(id=plan.checks[0].id, conforms=True)
        assert_data(result.to_data())
