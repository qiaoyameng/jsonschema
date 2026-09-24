from collections.abc import Mapping
from math import nan
from unittest import TestCase

from jsonschema import _utils
from jsonschema._utils import equal, find_duplicate_indices, uniq


class Unhashable:
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, Unhashable) and self.value == other.value


class TestEqual(TestCase):
    def test_none(self):
        self.assertTrue(equal(None, None))

    def test_nan(self):
        self.assertTrue(equal(nan, nan))


class TestDictEqual(TestCase):
    def test_equal_dictionaries(self):
        dict_1 = {"a": "b", "c": "d"}
        dict_2 = {"c": "d", "a": "b"}
        self.assertTrue(equal(dict_1, dict_2))

    def test_equal_dictionaries_with_nan(self):
        dict_1 = {"a": nan, "c": "d"}
        dict_2 = {"c": "d", "a": nan}
        self.assertTrue(equal(dict_1, dict_2))

    def test_missing_key(self):
        dict_1 = {"a": "b", "c": "d"}
        dict_2 = {"c": "d", "x": "b"}
        self.assertFalse(equal(dict_1, dict_2))

    def test_additional_key(self):
        dict_1 = {"a": "b", "c": "d"}
        dict_2 = {"c": "d", "a": "b", "x": "x"}
        self.assertFalse(equal(dict_1, dict_2))

    def test_missing_value(self):
        dict_1 = {"a": "b", "c": "d"}
        dict_2 = {"c": "d", "a": "x"}
        self.assertFalse(equal(dict_1, dict_2))

    def test_empty_dictionaries(self):
        dict_1 = {}
        dict_2 = {}
        self.assertTrue(equal(dict_1, dict_2))

    def test_one_none(self):
        dict_1 = None
        dict_2 = {"a": "b", "c": "d"}
        self.assertFalse(equal(dict_1, dict_2))

    def test_same_item(self):
        dict_1 = {"a": "b", "c": "d"}
        self.assertTrue(equal(dict_1, dict_1))

    def test_nested_equal(self):
        dict_1 = {"a": {"a": "b", "c": "d"}, "c": "d"}
        dict_2 = {"c": "d", "a": {"a": "b", "c": "d"}}
        self.assertTrue(equal(dict_1, dict_2))

    def test_nested_dict_unequal(self):
        dict_1 = {"a": {"a": "b", "c": "d"}, "c": "d"}
        dict_2 = {"c": "d", "a": {"a": "b", "c": "x"}}
        self.assertFalse(equal(dict_1, dict_2))

    def test_mixed_nested_equal(self):
        dict_1 = {"a": ["a", "b", "c", "d"], "c": "d"}
        dict_2 = {"c": "d", "a": ["a", "b", "c", "d"]}
        self.assertTrue(equal(dict_1, dict_2))

    def test_nested_list_unequal(self):
        dict_1 = {"a": ["a", "b", "c", "d"], "c": "d"}
        dict_2 = {"c": "d", "a": ["b", "c", "d", "a"]}
        self.assertFalse(equal(dict_1, dict_2))


class TestListEqual(TestCase):
    def test_equal_lists(self):
        list_1 = ["a", "b", "c"]
        list_2 = ["a", "b", "c"]
        self.assertTrue(equal(list_1, list_2))

    def test_equal_lists_with_nan(self):
        list_1 = ["a", nan, "c"]
        list_2 = ["a", nan, "c"]
        self.assertTrue(equal(list_1, list_2))

    def test_unsorted_lists(self):
        list_1 = ["a", "b", "c"]
        list_2 = ["b", "b", "a"]
        self.assertFalse(equal(list_1, list_2))

    def test_first_list_larger(self):
        list_1 = ["a", "b", "c"]
        list_2 = ["a", "b"]
        self.assertFalse(equal(list_1, list_2))

    def test_second_list_larger(self):
        list_1 = ["a", "b"]
        list_2 = ["a", "b", "c"]
        self.assertFalse(equal(list_1, list_2))

    def test_list_with_none_unequal(self):
        list_1 = ["a", "b", None]
        list_2 = ["a", "b", "c"]
        self.assertFalse(equal(list_1, list_2))

        list_1 = ["a", "b", None]
        list_2 = [None, "b", "c"]
        self.assertFalse(equal(list_1, list_2))

    def test_list_with_none_equal(self):
        list_1 = ["a", None, "c"]
        list_2 = ["a", None, "c"]
        self.assertTrue(equal(list_1, list_2))

    def test_empty_list(self):
        list_1 = []
        list_2 = []
        self.assertTrue(equal(list_1, list_2))

    def test_one_none(self):
        list_1 = None
        list_2 = []
        self.assertFalse(equal(list_1, list_2))

    def test_same_list(self):
        list_1 = ["a", "b", "c"]
        self.assertTrue(equal(list_1, list_1))

    def test_equal_nested_lists(self):
        list_1 = ["a", ["b", "c"], "d"]
        list_2 = ["a", ["b", "c"], "d"]
        self.assertTrue(equal(list_1, list_2))

    def test_unequal_nested_lists(self):
        list_1 = ["a", ["b", "c"], "d"]
        list_2 = ["a", [], "c"]
        self.assertFalse(equal(list_1, list_2))


class TestUniq(TestCase):
    def test_scalars(self):
        self.assertTrue(uniq([1, 2, 3]))
        self.assertFalse(uniq([1, 2, 1]))

    def test_bool_and_int_differ(self):
        self.assertTrue(uniq([True, 1]))
        self.assertTrue(uniq([False, 0]))
        self.assertFalse(uniq([True, True]))
        self.assertFalse(uniq([False, False]))

    def test_sequence_types_compare_structurally(self):
        self.assertFalse(uniq([[1, 2], (1, 2)]))
        self.assertTrue(uniq([[1, 2], (2, 1)]))

    def test_mappings_ignore_key_order(self):
        self.assertFalse(uniq([
            {"a": [1, 2], "b": {"c": 3}},
            {"b": {"c": 3}, "a": (1, 2)},
        ]))

    def test_falls_back_for_unhashable_scalars(self):
        self.assertFalse(uniq([Unhashable(1), Unhashable(1)]))
        self.assertTrue(uniq([Unhashable(1), Unhashable(2)]))

    def test_nan_falls_back(self):
        self.assertFalse(uniq([nan, nan]))
        self.assertTrue(uniq([nan, -nan]))

    def test_sequence_with_nan_falls_back(self):
        self.assertFalse(uniq([[nan], [nan]]))
        self.assertTrue(uniq([[nan], [-nan]]))

    def test_mapping_with_nan_falls_back(self):
        self.assertFalse(uniq([{"x": nan}, {"x": nan}]))
        self.assertTrue(uniq([{"x": nan}, {"x": -nan}]))

    def test_nested_bool_and_int_differ(self):
        # Exercises the _TRUE/_FALSE sentinels through recursion.
        self.assertTrue(uniq([[True], [1]]))
        self.assertTrue(uniq([{"k": True}, {"k": 1}]))

    def test_falls_back_for_unhashable_mapping_key(self):
        class FrozenDict(Mapping):
            def __init__(self, items):
                self._items = list(items)

            def __getitem__(self, key):
                for k, v in self._items:
                    if k == key:
                        return v
                raise KeyError(key)

            def __iter__(self):
                return (k for k, _ in self._items)

            def __len__(self):
                return len(self._items)

        a = FrozenDict([([1, 2], "x")])
        b = FrozenDict([([1, 2], "x")])
        self.assertFalse(uniq([a, b]))

    def test_unhashable_inside_sequence_falls_back(self):
        self.assertFalse(uniq([[Unhashable(1)], [Unhashable(1)]]))
        self.assertTrue(uniq([[Unhashable(1)], [Unhashable(2)]]))

    def test_unhashable_inside_mapping_falls_back(self):
        self.assertFalse(
            uniq([{"k": Unhashable(1)}, {"k": Unhashable(1)}]),
        )
        self.assertTrue(
            uniq([{"k": Unhashable(1)}, {"k": Unhashable(2)}]),
        )


class TestFindDuplicateIndices(TestCase):
    def test_no_duplicates(self):
        self.assertEqual(find_duplicate_indices([1, 2, 3]), [])
        self.assertEqual(find_duplicate_indices([]), [])

    def test_single_group(self):
        self.assertEqual(find_duplicate_indices([1, 2, 1]), [[0, 2]])

    def test_groups_are_ordered_by_first_occurrence(self):
        self.assertEqual(
            find_duplicate_indices([2, 1, 1, 2, 3, 2]),
            [[0, 3, 5], [1, 2]],
        )

    def test_bool_and_int_differ(self):
        self.assertEqual(find_duplicate_indices([True, 1]), [])
        self.assertEqual(find_duplicate_indices([False, 0]), [])
        self.assertEqual(find_duplicate_indices([True, True]), [[0, 1]])

    def test_int_and_float_compare_equal(self):
        self.assertEqual(find_duplicate_indices([1, 1.0]), [[0, 1]])

    def test_mappings_ignore_key_order(self):
        self.assertEqual(
            find_duplicate_indices([
                {"a": [1, 2], "b": {"c": 3}},
                {"b": {"c": 3}, "a": [1, 2]},
            ]),
            [[0, 1]],
        )

    def test_sequence_order_matters(self):
        self.assertEqual(find_duplicate_indices([[1, 2], [2, 1]]), [])

    def test_nested_containers(self):
        self.assertEqual(
            find_duplicate_indices([[1, {"a": (2,)}], [1, {"a": [2]}]]),
            [[0, 1]],
        )

    def test_falls_back_for_unhashable_scalars(self):
        self.assertEqual(
            find_duplicate_indices([Unhashable(1), 5, Unhashable(1)]),
            [[0, 2]],
        )
        self.assertEqual(
            find_duplicate_indices([Unhashable(1), Unhashable(2)]),
            [],
        )

    def test_nan_falls_back(self):
        self.assertEqual(find_duplicate_indices([nan, nan]), [[0, 1]])
        self.assertEqual(find_duplicate_indices([nan, -nan]), [])

    def test_common_values_do_not_degrade_to_quadratic(self):
        """
        Hashable JSON values are grouped without pairwise comparisons.
        """
        instance = list(range(5000)) + [{"a": [1, 2]}, {"b": 2, "a": 1}]
        instance.append(instance[1234])

        comparisons = []
        real_equal = _utils.equal

        def counting_equal(one, two):
            comparisons.append((one, two))
            return real_equal(one, two)

        original = _utils.equal
        try:
            _utils.equal = counting_equal
            groups = find_duplicate_indices(instance)
        finally:
            _utils.equal = original

        self.assertEqual(groups, [[1234, 5002]])
        self.assertEqual(comparisons, [])
