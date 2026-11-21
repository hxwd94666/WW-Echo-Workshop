# 验证配装搜索预算只要求正整数，并能按档案立即写入读取。
import unittest
import uuid

from ww_echo_workshop.core.search_budget import (
    SearchBudget,
    allocation_budget,
    set_allocation_budget,
)


class SearchBudgetContract(unittest.TestCase):
    def setUp(self):
        self.config = {}
        self.profile = str(uuid.uuid4())

    def test_large_positive_values_are_accepted_and_read_back(self):
        budget = SearchBudget(121, 5_000_001)
        set_allocation_budget(self.config, self.profile, budget)
        self.assertEqual(allocation_budget(self.config, self.profile), budget)

    def test_only_positive_integers_are_accepted(self):
        for seconds, nodes in ((0, 1), (-1, 1), (1, 0), (1, -1)):
            with self.subTest(seconds=seconds, nodes=nodes):
                with self.assertRaises(ValueError):
                    SearchBudget(seconds, nodes)

    def test_large_values_are_written_immediately(self):
        set_allocation_budget(self.config, self.profile, SearchBudget(300, 20_000_000))
        self.assertIn(
            '"search_budget":{"nodes":20000000,"seconds":300}',
            self.config['AllocationSettingsJSON'],
        )
