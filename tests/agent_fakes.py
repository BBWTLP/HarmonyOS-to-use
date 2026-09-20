"""Scripted devices and providers used by the harmony_agent tests.

No test in this module touches a real phone, the local Decider service or the
network. Everything is deterministic so failures point at the agent layer.
"""
from __future__ import annotations

import copy
import pathlib
import shutil
import tempfile

WEIBO = "com.sina.weibo.stage"


def node(**attributes):
    attributes.setdefault("bundleName", WEIBO)
    attributes.setdefault("visible", "true")
    return {"attributes": attributes, "children": []}


def tree(children):
    return {"attributes": {"bundleName": WEIBO, "visible": "true",
                           "bounds": "[0,0][1080,2340]", "type": "Root"},
            "children": children}


def weibo_home():
    return tree([
        # The bottom navigation follows the real build: a clickable Row contains
        # a non-clickable Text label, so grounding must resolve the ancestor.
        {"attributes": {"bundleName": WEIBO, "visible": "true", "type": "Row",
                        "bounds": "[0,2200][270,2340]", "clickable": "true",
                        "id": "tab_home", "selected": "true"},
         "children": [node(type="Text", text="首页", bounds="[100,2240][170,2300]")]},
        node(type="Button", text="发现", bounds="[270,2200][540,2340]", clickable="true",
             id="tab_discover"),
        node(type="Button", text="消息", bounds="[540,2200][810,2340]", clickable="true",
             id="tab_message"),
        node(type="Button", text="我", bounds="[810,2200][1080,2340]", clickable="true",
             id="tab_profile"),
        node(type="Button", text="搜索", bounds="[900,100][1060,200]", clickable="true",
             id="search_entry"),
        node(type="Text", text="微博推荐内容", bounds="[40,400][800,600]"),
    ])


def weibo_search_editor(text=""):
    return tree([
        node(type="TextInput", text=text, hint="搜索微博", bounds="[80,100][820,220]",
             clickable="true", focused="true", id="search_input"),
        node(type="Button", text="取消", bounds="[840,100][1020,220]", clickable="true",
             id="search_cancel"),
        node(type="Button", text="搜索", bounds="[840,240][1020,360]", clickable="true",
             id="search_confirm_btn"),
        node(type="Text", text="热搜榜", bounds="[40,400][600,500]"),
    ])


def weibo_results(query):
    return tree([
        node(type="TextInput", text=query, bounds="[80,100][820,220]", clickable="true",
             id="search_input"),
        node(type="Button", text="综合", bounds="[40,280][200,360]", clickable="true",
             id="tab_all", selected="true"),
        node(type="Button", text="用户", bounds="[200,280][360,360]", clickable="true",
             id="tab_user"),
        node(type="Text", text=f"{query} 的相关结果", bounds="[40,420][1000,600]"),
        node(type="Text", text=query, bounds="[40,620][1000,760]"),
    ])


class FakeWeiboDevice:
    """A three-step Weibo flow: home -> search editor -> results."""

    supports_foreground = True

    def __init__(self, serial="fake-device", *, dispatch_failure=None,
                 black_screen=False):
        self.serial = serial
        self.stage = "home"
        self.query = ""
        self.writes = []
        self.closed = False
        self.dispatch_failure = dispatch_failure
        self.black_screen = black_screen
        self.foreground_bundle = WEIBO

    # -- read path ----------------------------------------------------------
    def screen_state(self):
        return {"screen_on": True, "screen_locked": False}

    def foreground(self):
        return {"status": "ok", "bundle": self.foreground_bundle}

    def display(self):
        return (1080, 2340, 0)

    def tree(self):
        if self.stage == "home":
            return copy.deepcopy(weibo_home())
        if self.stage == "editor":
            return copy.deepcopy(weibo_search_editor(self.query))
        return copy.deepcopy(weibo_results(self.query))

    def screenshot(self):
        from PIL import Image
        colour = (0, 0, 0) if self.black_screen else (40, 40, 40)
        return Image.new("RGB", (1080, 2340), colour)

    # -- write path ---------------------------------------------------------
    def dispatch(self, action, target):
        if self.dispatch_failure:
            failure = self.dispatch_failure
            self.dispatch_failure = None
            raise failure
        self.writes.append({"kind": action.kind,
                            "text": getattr(action, "text", None),
                            "target_id": (target or {}).get("resource_id"),
                            "target_text": (target or {}).get("text")})
        if action.kind == "tap":
            if (target or {}).get("resource_id") == "search_entry":
                self.stage = "editor"
            elif (target or {}).get("resource_id") == "search_confirm_btn":
                self.stage = "results"
        elif action.kind in ("input_text", "replace_text"):
            # Typing keeps the editor open; a real search box only navigates
            # when the user submits.
            self.query = action.text or ""
            self.stage = "editor"
        elif action.kind == "back" and self.stage != "home":
            self.stage = "home"

    def close(self):
        self.closed = True


class FakeTransportProvider:
    """Decider provider stand-in with scripted answers."""

    def __init__(self, answers):
        self.provider = "decider"
        self.model_revision = "fake-revision"
        self.answers = answers
        self.calls = 0

    async def evaluate(self, context, questions, *, remaining_model_calls=None):
        from harmony_agent.decision.providers.base import ProviderResult
        self.calls += 1
        return ProviderResult(provider="decider", model_revision=self.model_revision,
                              answer=dict(self.answers),
                              native_scores={"score_semantics": "decider_systemone",
                                             "answers": dict(self.answers)},
                              elapsed_ms=1.0)


def decider_choice(candidate_id, probabilities=None):
    return {"action": {"type": "choice", "choice": candidate_id,
                       "confidence": max(probabilities.values()) if probabilities else 0.9,
                       "certainty": 0.8,
                       "probabilities": probabilities or {candidate_id: 0.9,
                                                          "cand_none_applicable": 0.1}}}


def calibration(calibration_id="cal-2026-09-20", *, revision="fake-revision",
                confidence=0.5, certainty=0.5):
    """A validated calibration record, as a canary deployment would load one."""
    from harmony_agent.decision.calibration import Calibration
    return Calibration(calibration_id=calibration_id, provider_revision=revision,
                       confidence_threshold=confidence,
                       certainty_threshold=certainty, dataset_sha256="a" * 64)


def temp_token_file(case, token: str = "test-token") -> pathlib.Path:
    """A throwaway Decider token file, removed when the test finishes.

    ``DeciderProvider`` defaults to the repo-relative runtime token
    (``services/decider/.runtime/api-token``). That file is gitignored and only
    exists on a machine that has started the Decider service, so a test that
    relies on the default is not reproducible from a fresh clone. Tests must
    supply their own token instead of reading machine state.
    """
    directory = tempfile.mkdtemp(prefix="decider-token-")
    case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    path = pathlib.Path(directory) / "api-token"
    path.write_text(token, encoding="utf-8")
    return path


def make_decider_provider(case, transport, *, token: str = "test-token", **kwargs):
    """``DeciderProvider`` wired to a hermetic test token, never to machine state."""
    from harmony_agent.decision.providers.decider import DeciderProvider
    return DeciderProvider(transport=transport, token_file=temp_token_file(case, token),
                           **kwargs)
