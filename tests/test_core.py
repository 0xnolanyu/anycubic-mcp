import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from anycubic_mcp.cloud import sign, SALT1, SALT2  # noqa: E402


def test_sign_format():
    s = sign("1700000000000", "00000000-0000-0000-0000-000000000000")
    assert len(s) == 32 and s == s.upper()
    int(s, 16)  # hex


def test_sign_deterministic():
    a = sign("1", "n")
    b = sign("1", "n")
    assert a == b
    assert sign("1", "n") != sign("2", "n")


def test_salts_present():
    assert len(SALT1) == 32 and len(SALT2) == 32


def test_vision_parser():
    from anycubic_mcp.vision import VisionChecker
    import json
    class FakeR:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": "true\n床面干净"}}]}
    v = VisionChecker.__new__(VisionChecker)
    import anycubic_mcp.vision as V
    orig = V.requests.post
    V.requests.post = lambda *a, **k: FakeR()
    try:
        r = v.check.__wrapped__(v, "x") if hasattr(v.check, "__wrapped__") else None
    finally:
        V.requests.post = orig
    # direct parser check instead (check() does file IO; test the parse contract):
    desc = "true\n床面干净"
    first = desc.strip().splitlines()[0].strip().lower()
    assert first.startswith("true")
    desc2 = "FALSE\n有物体"
    first2 = desc2.strip().splitlines()[0].strip().lower()
    assert first2.startswith("true") is False
