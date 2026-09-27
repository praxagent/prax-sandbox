"""Shortcuts are sent with real modifiers, and multi-line text goes in as one edit.

"Control+a" used to go out as one key literally named "Control+a" with no
modifier state — pages ignored it while the tool reported "Pressed Control+a",
so an agent believed it had selected everything and typed a second copy after
the first. And typing code key by key into an editor re-indented after every
Enter and auto-closed every bracket.
"""
from __future__ import annotations

import pytest

from prax_sandbox import cdp_service as cdp


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(cdp, "_send_cdp", lambda method, params=None, timeout=10:
                        calls.append((method, params)) or {})
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    return calls


def _events(sent):
    return [(p["type"], p["key"], p.get("modifiers", 0)) for m, p in sent if m == "Input.dispatchKeyEvent"]


def test_control_a_holds_control_down_around_the_a(sent):
    assert cdp.press_key("Control+a") == {"status": "Pressed Control+a"}
    assert _events(sent) == [
        ("rawKeyDown", "Control", 2),
        ("rawKeyDown", "a", 2),
        ("keyUp", "a", 2),
        ("keyUp", "Control", 0),
    ]
    a_down = sent[1][1]
    assert a_down["code"] == "KeyA" and a_down["windowsVirtualKeyCode"] == 65
    assert "text" not in a_down  # a shortcut must not also type an "a"


def test_multiple_modifiers_accumulate_and_release_in_reverse(sent):
    cdp.press_key("Control+Shift+End")
    assert _events(sent) == [
        ("rawKeyDown", "Control", 2),
        ("rawKeyDown", "Shift", 10),
        ("rawKeyDown", "End", 10),
        ("keyUp", "End", 10),
        ("keyUp", "Shift", 2),
        ("keyUp", "Control", 0),
    ]
    assert sent[2][1]["windowsVirtualKeyCode"] == 35


@pytest.mark.parametrize("key, code, vk", [
    ("Home", "Home", 36), ("Delete", "Delete", 46), ("PageDown", "PageDown", 34),
    ("F12", "F12", 123), ("Escape", "Escape", 27), ("esc", "Escape", 27),
])
def test_named_keys_carry_their_codes(sent, key, code, vk):
    cdp.press_key(key)
    down = sent[0][1]
    assert down["code"] == code and down["windowsVirtualKeyCode"] == vk


def test_enter_types_a_carriage_return(sent):
    cdp.press_key("Enter")
    assert sent[0][1]["type"] == "keyDown" and sent[0][1]["text"] == "\r"


def test_shift_letter_types_the_capital(sent):
    cdp.press_key("Shift+a")
    down = sent[1][1]
    assert down["type"] == "keyDown" and down["key"] == "A" and down["text"] == "A"


@pytest.mark.parametrize("key", ["Hyper+a", "Controll+a", "NotAKey"])
def test_unknown_keys_are_an_error_not_a_false_success(sent, key):
    out = cdp.press_key(key)
    assert "error" in out and "status" not in out
    assert sent == []


def test_single_line_text_is_still_typed_key_by_key(sent):
    assert cdp.type_text("hi") == {"status": "Typed 2 characters"}
    assert [p["type"] for m, p in sent] == ["keyDown", "char", "keyUp"] * 2


def test_multiline_text_is_inserted_as_one_edit(sent):
    code = "class S {\n  int f() {\n    return 1;\n  }\n};"
    assert cdp.type_text(code) == {"status": f"Inserted {len(code)} characters"}
    assert sent == [("Input.insertText", {"text": code})]


def test_insert_reports_a_cdp_failure(monkeypatch):
    monkeypatch.setattr(cdp, "_send_cdp", lambda *a, **k: {"error": "no target"})
    assert cdp.insert_text("a\nb") == {"error": "insert failed: no target"}
