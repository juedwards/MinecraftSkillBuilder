import io

from mcchat.console import Console, describe_os_error, status_tone


def plain() -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(out, colour=False, unicode=True), out


def test_event_lines_are_aligned_plain_text():
    console, out = plain()
    console.event({"type": "question", "player": "Alex", "text": "how do I build a bridge?"})
    console.event({"type": "build", "player": "Alex", "status": "map: done: 7 buildings, 0 commands failed"})
    lines = out.getvalue().splitlines()
    assert lines[0][10:].startswith("CHAT    Alex › how do I build a bridge?")
    assert lines[1][10:].startswith("BUILD   Alex › map: done")
    assert "\033[" not in out.getvalue()


def test_long_answers_stay_on_one_line_without_a_terminal():
    console, out = plain()
    console.event({"type": "answer", "player": "Alex", "text": "word " * 200})
    assert len(out.getvalue().splitlines()) == 1


def test_colour_wraps_under_the_message(monkeypatch):
    out = io.StringIO()
    console = Console(out, colour=True, unicode=True)
    monkeypatch.setattr(console, "width", lambda: 60)
    console.event({"type": "question", "player": "Alex", "text": "word " * 40})
    lines = out.getvalue().splitlines()
    assert len(lines) > 1
    assert lines[1].startswith(" " * 20)


def test_assessment_lists_each_criterion():
    console, out = plain()
    console.event({"type": "assessment", "player": "Alex", "rubric": "Build a Bridge", "attempt": 2, "report": "r.md",
                   "criteria": [{"name": "Spanning the gap", "level": "Secure"}, {"name": "Materials", "level": "Developing"}]})
    text = out.getvalue()
    assert "RESULT  Alex › Build a Bridge, attempt 2" in text
    assert "Spanning the gap" in text and "Secure" in text and "Developing" in text and "report: r.md" in text


def test_ascii_fallback():
    out = io.StringIO()
    Console(out, colour=False, unicode=False).event({"type": "answer", "player": "Alex", "text": "hi"})
    assert "-> Alex > hi" in out.getvalue()


def test_status_tone():
    assert status_tone("map: done: 7 buildings, 0 commands failed") == "32"
    assert status_tone("done: 100 commands, 3 failed") == "33"
    assert status_tone("failed: timeout") == "31"
    assert status_tone("map: placing 1756 commands") == ""


def test_port_in_use_message():
    exc = OSError(98, "error while attempting to bind on address ('0.0.0.0', 3000): [errno 98] address already in use")
    assert describe_os_error(exc).startswith("Port 3000 is already in use")
