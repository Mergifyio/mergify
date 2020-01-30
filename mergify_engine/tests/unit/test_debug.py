from mergify_engine import debug


def test_debug_wrong_path():
    debug.report("foobar")
