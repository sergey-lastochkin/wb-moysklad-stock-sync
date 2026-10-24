import unittest

from stock_sync import fix_plan, overstock, unknown

ROWS = [
    {'warehouse': 1, 'sku': 'a', 'article': 'A', 'wb': 5, 'ms': 2},    # больше МС → снизить до 2
    {'warehouse': 1, 'sku': 'b', 'article': 'B', 'wb': 3, 'ms': 3},    # совпадает
    {'warehouse': 1, 'sku': 'c', 'article': 'C', 'wb': 1, 'ms': 4},    # на WB меньше → не трогаем
    {'warehouse': 2, 'sku': 'd', 'article': 'D', 'wb': 2, 'ms': -1},   # минус в МС → в ноль
    {'warehouse': 2, 'sku': 'e', 'article': '', 'wb': 7, 'ms': None},  # нет в МС → не трогаем
]


class Logic(unittest.TestCase):
    def test_overstock_only_where_wb_exceeds(self):
        self.assertEqual([r['sku'] for r in overstock(ROWS)], ['a', 'd'])

    def test_unknown_articles_are_reported_not_fixed(self):
        self.assertEqual([r['sku'] for r in unknown(ROWS)], ['e'])

    def test_plan_only_lowers_and_never_below_zero(self):
        self.assertEqual(fix_plan(ROWS), {1: [{'sku': 'a', 'amount': 2}], 2: [{'sku': 'd', 'amount': 0}]})

    def test_plan_never_raises(self):
        for stocks in fix_plan(ROWS).values():
            for s in stocks:
                row = next(r for r in ROWS if r['sku'] == s['sku'])
                self.assertLess(s['amount'], row['wb'])


if __name__ == '__main__':
    unittest.main()
