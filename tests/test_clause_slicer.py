"""Clause slicer rules (PARSE_VERSION = clause-index-4).

These are the rules that stopped mid-sentence citations from being sliced out as
fake articles. They are pure string logic, so they run without any standards
text in the repository.
"""

from __future__ import annotations

import unittest

import clause_index as ci


SAMPLE = """第一条 为了规范管理，制定本办法。
第二条 生产经营者应当遵守本办法。
违反本办法第八条、第九条规定的，依照有关规定处理。
第三条 监督检查人员依法履行职责。
第四条 本办法自发布之日起施行。
"""


class HeadingRulesTests(unittest.TestCase):
    def test_line_start_articles_are_headings(self):
        numbers = [row["number"] for row in ci.find_headings(SAMPLE)]
        self.assertEqual(numbers, ["第一条", "第二条", "第三条", "第四条"])

    def test_mid_line_citation_is_not_a_heading(self):
        numbered = {row["number"] for row in ci.find_headings(SAMPLE)}
        self.assertNotIn("第八条", numbered)
        self.assertNotIn("第九条", numbered)

    def test_article_followed_by_citation_continuation_is_rejected(self):
        text = "第一条 总则。\n依照第八条规定的程序办理。\n第二条 附则。\n"
        numbered = [row["number"] for row in ci.find_headings(text)]
        self.assertEqual(numbered, ["第一条", "第二条"])

    def test_duplicate_and_backward_numbers_are_dropped(self):
        text = "第一条 总则。\n第二条 检查。\n第二条 检查（重复）。\n第一条 总则（回退）。\n第三条 附则。\n"
        numbered = [row["number"] for row in ci.find_headings(text)]
        self.assertEqual(numbered, ["第一条", "第二条", "第三条"])

    def test_parse_version_is_recorded(self):
        self.assertEqual(ci.PARSE_VERSION, "clause-index-4")


class ArticleNumberTests(unittest.TestCase):
    def test_article_number_value_parses_chinese_numerals(self):
        self.assertEqual(ci.article_number_value("第一条"), 1)
        self.assertEqual(ci.article_number_value("第十条"), 10)
        self.assertEqual(ci.article_number_value("第一百二十五条"), 125)
        self.assertIsNone(ci.article_number_value("不是条号"))


if __name__ == "__main__":
    unittest.main()
