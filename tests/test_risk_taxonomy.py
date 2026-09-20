"""v3.2 Phase 8: one sensitive-target taxonomy, one authority.

`CandidateRegistry.risk_class_for` and `Runtime._policy` used to keep separate
word lists, which is how the agent could drift into being more permissive than
the guard. Both now read `harmony_runtime.risk`.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from harmony_agent.candidates import risk_class_for
from harmony_runtime.risk import (CATEGORIES, RISK_CLASSES, SENSITIVE_TERMS, categories,
                                  is_sensitive, label_of, risk_class_for as runtime_class,
                                  scan)


class TaxonomyTests(unittest.TestCase):
    def test_every_category_covers_chinese_and_english(self):
        for name, terms in CATEGORIES.items():
            self.assertTrue(any(term.isascii() for term in terms),
                            f"{name} has no English term")
            self.assertTrue(any(not term.isascii() for term in terms),
                            f"{name} has no Chinese term")

    def test_the_required_categories_exist(self):
        for name in ("payment", "delete", "send", "permission", "credential",
                     "session"):
            self.assertIn(name, CATEGORIES)

    def test_classes_are_the_documented_three(self):
        self.assertEqual(RISK_CLASSES, ("low", "medium", "high"))

    def test_terms_are_unique(self):
        self.assertEqual(len(SENSITIVE_TERMS), len(set(SENSITIVE_TERMS)))


class SensitivityTests(unittest.TestCase):
    CHINESE_CASES = {
        "支付": "payment", "付款": "payment", "转账": "payment", "购买": "payment",
        "删除": "delete", "卸载": "delete", "清空": "delete",
        "发送": "send", "提交": "send", "发布": "send",
        "允许": "permission", "授权": "permission",
        "密码": "credential", "验证码": "credential",
        "退出登录": "session", "注销": "session", "切换账号": "session",
    }

    ENGLISH_CASES = {
        "Pay now": "payment", "Purchase": "payment", "Transfer funds": "payment",
        "Delete account": "delete", "Uninstall": "delete",
        "Send": "send", "Submit": "send", "Publish": "send",
        "Allow": "permission", "Authorize": "permission",
        "Password": "credential", "OTP": "credential", "PIN": "credential",
        "Log out": "session", "Logout": "session", "Sign out": "session",
        "Account": "session",
    }

    def test_chinese_terms_are_sensitive_in_the_right_category(self):
        for term, expected in self.CHINESE_CASES.items():
            self.assertTrue(is_sensitive(term), term)
            self.assertIn(expected, categories(term), term)
            self.assertEqual(risk_class_for(term), "high", term)

    def test_english_terms_are_sensitive_in_the_right_category(self):
        for term, expected in self.ENGLISH_CASES.items():
            self.assertTrue(is_sensitive(term), term)
            self.assertIn(expected, categories(term), term)

    def test_low_risk_labels_stay_low(self):
        for label in ("搜索", "首页", "我", "取消", "返回", "Search", "Home",
                      "Cancel", "Next", "播放"):
            self.assertFalse(is_sensitive(label), label)
            self.assertEqual(risk_class_for(label), "low", label)

    def test_identifier_separators_are_word_boundaries(self):
        # Deliberately conservative: an identifier that names a sensitive action
        # is treated as that action. A false positive only refuses a tap; the
        # trusted approval flow that would resolve it is not implemented.
        for label in ("account_avatar", "submit_button", "send_icon",
                      "password_field_v2", "delete-all"):
            self.assertTrue(is_sensitive(label), label)

    def test_a_letter_run_is_not_split_into_a_sensitive_word(self):
        # `pinsix` has no separator, so it is not the standalone word "pin".
        self.assertFalse(is_sensitive("pinsix"))
        self.assertTrue(is_sensitive("pin_six"))

    def test_empty_and_missing_labels_are_low_risk(self):
        for label in ("", None, "   ", "未知控件"):
            self.assertFalse(is_sensitive(label), label)
            self.assertEqual(risk_class_for(label), "low", label)

    def test_scan_returns_the_matched_terms(self):
        self.assertIn("删除", scan("删除账号"))
        self.assertIn("account", scan("Delete account"))

    def test_case_and_spacing_do_not_matter(self):
        self.assertTrue(is_sensitive("  DELETE   ACCOUNT  "))
        self.assertTrue(is_sensitive("Delete\tAccount"))

    def test_a_chinese_phrase_is_matched_inside_longer_copy(self):
        self.assertTrue(is_sensitive("同意并授权后继续"))
        self.assertTrue(is_sensitive("请输入支付密码以完成验证"))


class AuthorityParityTests(unittest.TestCase):
    """The runtime verdict is the ceiling: the agent can never be looser."""

    def test_the_agent_classifier_delegates_to_the_shared_taxonomy(self):
        for term in SENSITIVE_TERMS:
            self.assertEqual(risk_class_for(term), runtime_class(term), term)
            self.assertEqual(risk_class_for(term), "high", term)

    def test_a_runtime_blocked_label_is_always_high_risk_for_the_agent(self):
        samples = ["支付", "删除", "发送", "授权", "密码", "退出登录",
                   "Pay", "Delete", "Submit", "Allow", "Password", "Log out",
                   "账号", "Account", "OTP"]
        for label in samples:
            self.assertTrue(is_sensitive(label), label)
            self.assertEqual(risk_class_for(label), "high", label)

    def test_the_label_used_by_the_guard_includes_every_selector(self):
        target = {"text": "确认", "hint": "", "description": "",
                  "resource_id": "delete_all", "type": "Button"}
        self.assertIn("delete_all", label_of(target))
        self.assertTrue(is_sensitive(label_of(target)))

    def test_a_missing_target_is_not_sensitive(self):
        self.assertEqual(label_of(None), "")
        self.assertFalse(is_sensitive(label_of(None)))


if __name__ == "__main__":
    unittest.main()
