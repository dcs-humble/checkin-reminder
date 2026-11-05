import datetime
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'app'), str(ROOT/'lib')]
import weekly
import docs

class WeeklyTests(unittest.TestCase):
    def test_cross_year_week(self):
        days = weekly.last_week(datetime.date(2027, 1, 4))
        self.assertEqual(days[0], datetime.date(2026, 12, 28))
        self.assertEqual(days[-1], datetime.date(2027, 1, 3))

    def export(self, header=None):
        calls = []
        def read(url, wait, day):
            calls.append(day)
            return dict(logged_in=True, header=header or f"{day.month}月{day.day}日打卡情况",
                        unchecked_ok=True, unchecked=['测试甲'])
        c = dict(roster='fake.xlsx',name='测试班')
        with patch.object(weekly.cfgmod, 'load', return_value={}), \
             patch.object(weekly.cfgmod, 'active_class', return_value=c), \
             patch.object(docs, 'read_roster', return_value=['测试甲']), \
             patch.object(weekly, 'form_url', return_value='https://example.invalid'), \
             patch.object(docs, 'port_open', return_value=True), \
             patch.object(docs, 'read_status', side_effect=read), \
             patch.object(docs, 'who_is_missing', return_value={'missing':['测试甲']}), \
             patch.object(weekly.clip, 'put'):
            result = weekly._export(today=datetime.date(2026,11,5))
        return result,calls

    def test_exports_all_seven_dates_across_month(self):
        result,calls = self.export()
        self.assertEqual(calls,weekly.last_week(datetime.date(2026,11,5)))
        self.assertEqual(result['errors'],0)
        self.assertEqual(len(result['text'].splitlines()),7)

    def test_same_day_wrong_month_is_rejected(self):
        result,_ = self.export(header='11月26日打卡情况')
        self.assertEqual(result['errors'],7)

    def test_missing_date_header_is_rejected(self):
        result,_ = self.export(header='没有日期')
        self.assertEqual(result['errors'],7)

class CalendarTests(unittest.TestCase):
    def test_cross_year_navigation_uses_previous_month(self):
        class Page:
            def __init__(self): self.scripts=[];self.shown=iter([[2027,1],[2026,12]])
            def ev(self, js):
                self.scripts.append(js)
                if 'parseInt' in js: return next(self.shown)
                if 'statistics-section-header' in js: return '12月31日打卡情况'
                return True
        p=Page()
        with patch.object(docs.time,'sleep'):
            self.assertTrue(docs.select_day(p,datetime.date(2026,12,31)))
        self.assertTrue(any('header-prev' in js for js in p.scripts))
        self.assertTrue(any('2026' in js and 'td.title' in js for js in p.scripts))

    def test_disabled_or_missing_date_fails(self):
        class Page:
            def ev(self, js):
                if 'parseInt' in js: return [2026,10]
                if 'td.title' in js: return False
                return True
        with patch.object(docs.time,'sleep'):
            self.assertFalse(docs.select_day(Page(),datetime.date(2026,10,31)))

if __name__ == '__main__': unittest.main()
