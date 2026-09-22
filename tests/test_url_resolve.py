import pytest

from app.canvas_client import resolve_assignment_url, submission_fingerprint


def test_assignment_url():
    u = "https://oc.sjtu.edu.cn/courses/94139/assignments/428317"
    assert resolve_assignment_url(u) == {"course_id": 94139, "assignment_id": 428317}


def test_speedgrader_url():
    u = "https://oc.sjtu.edu.cn/courses/94139/gradebook/speed_grader?assignment_id=428317&student_id=1"
    assert resolve_assignment_url(u)["assignment_id"] == 428317


def test_reject_wrong_host():
    with pytest.raises(ValueError):
        resolve_assignment_url("https://canvas.instructure.com/courses/1/assignments/2")


def test_fingerprint_stable():
    a = submission_fingerprint("t", 1, ["2", "1"])
    b = submission_fingerprint("t", 1, ["1", "2"])
    assert a == b
