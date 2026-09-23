"""Shared sensitive-target taxonomy.

The Runtime Guard is the authority that decides whether an action may be
dispatched. The agent layer classifies risk only to route and pre-filter, so the
two must read the *same* words: one list, one classifier, one verdict.

The agent may classify a target as riskier than the runtime does, but the
runtime can never be made more permissive by an agent decision.
"""
from __future__ import annotations

import re
from typing import Iterable

#: Risk classes understood by both layers, from least to most restricted.
RISK_CLASSES = ("low", "medium", "high")

#: Categories, each with Chinese terms and English terms.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "payment": ("支付", "付款", "转账", "购买", "下单", "充值", "扣款", "退款",
                "pay", "payment", "purchase", "buy", "checkout", "transfer",
                "refund", "subscribe"),
    "delete": ("删除", "卸载", "清空", "格式化", "恢复出厂",
               "delete", "remove", "uninstall", "erase", "reset"),
    "send": ("发送", "发表", "提交", "发布", "上传",
             "send", "submit", "post", "publish", "upload"),
    "permission": ("允许", "授权", "权限", "同意并继续",
                   "permission", "authorize", "authorise", "grant", "allow"),
    "credential": ("密码", "验证码", "动态码", "指纹", "面容", "人脸",
                   "password", "passcode", "pin", "otp", "verification code",
                   "fingerprint", "face id"),
    "session": ("退出登录", "注销", "登出", "切换账号", "账号", "账户", "解绑",
                "logout", "log out", "sign out", "signout", "account",
                "sign in", "login", "log in"),
}

#: Flat term list, kept for callers that only need membership.
SENSITIVE_TERMS: tuple[str, ...] = tuple(
    dict.fromkeys(term for terms in CATEGORIES.values() for term in terms))


def normalize_label(label: str | None) -> str:
    """Lower-case the label and treat identifier separators as word breaks.

    `account_avatar` and `delete-all` therefore contain the standalone words
    "account" and "delete". CamelCase is split too so `passwordInput` matches
    "password". That is deliberately conservative: a false positive costs one
    refused action, a false negative costs an unintended write, and the
    trusted approval flow that would resolve the ambiguity is not implemented.
    """
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(label or ""))
    text = " ".join(text.split()).lower()
    return re.sub(r"[_./\\-]+", " ", text)


def scan(label: str | None) -> list[str]:
    """Every sensitive term present in the label, in taxonomy order."""
    text = normalize_label(label)
    if not text:
        return []
    found: list[str] = []
    for term in SENSITIVE_TERMS:
        if term.isascii():
            # Word-bounded so identifiers such as `account_avatar` do not trip
            # the rule just because they contain an English word. The label is
            # lower-cased first, so excluding [a-z0-9_] is enough.
            if re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text):
                found.append(term)
        elif term in text:
            found.append(term)
    return found


def categories(label: str | None) -> list[str]:
    """Which sensitive categories the label falls into."""
    text = normalize_label(label)
    hit = {term for term in scan(text)}
    return [name for name, terms in CATEGORIES.items()
            if any(term in hit for term in terms)]


def is_sensitive(label: str | None) -> bool:
    """True when the Runtime Guard must refuse to dispatch this target."""
    return bool(scan(label))


def risk_class_for(label: str | None) -> str:
    """Classification used by the agent layer for routing and pre-filtering."""
    return "high" if is_sensitive(label) else "low"


def label_of(target: dict | None, *,
             keys: Iterable[str] = ("text", "hint", "description", "resource_id", "type",
                                    "visual_evidence")) -> str:
    """Join the inspectable fields of a resolved target into one label.

    `visual_evidence` carries the *device-derived* text that overlaps a visual
    region. It is part of the label on purpose: a visual proposal's own label is
    supplied by the proposer, so the risk decision must see independent evidence
    as well and take the union of both.
    """
    if not target:
        return ""
    # Keep original case so normalize_label can split camelCase identifiers
    # (passwordInput) before lower-casing.
    return " ".join(str(target.get(key, "")) for key in keys)


def control_label_of(target: dict | None) -> str:
    """Label used to judge *control* risk for input actions.

    Uses the control's identity (resource id, type, independent visual
    evidence), not its current value or placeholder. A search box showing
    "小米18pro系列今日发布" is not a publish control; a field whose id is
    ``passwordInput`` or ``pinSix`` still is.
    """
    return label_of(target, keys=("resource_id", "type", "visual_evidence"))

