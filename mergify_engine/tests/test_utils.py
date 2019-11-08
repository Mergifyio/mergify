from mergify_engine import utils


def test_unicode_truncate():
    s = "hé ho! how are you√2?"
    assert utils.unicode_truncate(s, 0) == ""
    assert utils.unicode_truncate(s, 1) == "h"
    assert utils.unicode_truncate(s, 2) == "h"
    assert utils.unicode_truncate(s, 3) == "hé"
    assert utils.unicode_truncate(s, 4) == "hé "
    assert utils.unicode_truncate(s, 10) == "hé ho! ho"
    assert utils.unicode_truncate(s, 18) == "hé ho! how are yo"
    assert utils.unicode_truncate(s, 19) == "hé ho! how are you"
    assert utils.unicode_truncate(s, 20) == "hé ho! how are you"
    assert utils.unicode_truncate(s, 21) == "hé ho! how are you"
    assert utils.unicode_truncate(s, 22) == "hé ho! how are you√"
    assert utils.unicode_truncate(s, 23) == "hé ho! how are you√2"
    assert utils.unicode_truncate(s, 50) == s
