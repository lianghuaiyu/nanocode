# tests/tools/test_sandbox_defaults.py
from nanocode.tools import sandbox_defaults as sd


def setup_function():
    sd.reset_defaults()


def test_defaults_are_strictest():
    d = sd.get_defaults()
    assert d == {"persist": False, "network": "none", "mount_workspace": False, "deps": "reuse", "trace": False}


def test_set_bool_via_string_on_off():
    assert sd.set_default("persist", "on") is True
    assert sd.get_defaults()["persist"] is True
    sd.set_default("persist", "off")
    assert sd.get_defaults()["persist"] is False


def test_set_network_enum():
    sd.set_default("network", "public")
    assert sd.get_defaults()["network"] == "public"


def test_set_deps_enum():
    sd.set_default("deps", "install")
    assert sd.get_defaults()["deps"] == "install"


def test_invalid_key_raises():
    try:
        sd.set_default("nope", "x")
        assert False, "should have raised"
    except ValueError as e:
        assert "unknown sandbox setting" in str(e)


def test_invalid_network_value_raises():
    try:
        sd.set_default("network", "wifi")
        assert False
    except ValueError as e:
        assert "network must be" in str(e)


def test_invalid_bool_value_raises():
    try:
        sd.set_default("persist", "maybe")
        assert False
    except ValueError as e:
        assert "on" in str(e) and "off" in str(e)


def test_trace_default_off_and_toggle():
    sd.reset_defaults()
    assert sd.get_defaults()["trace"] is False
    assert sd.set_default("trace", "on") is True
    assert sd.get_defaults()["trace"] is True
