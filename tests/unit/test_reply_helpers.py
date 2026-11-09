from maika.discord_adapter import REPLY_PART_LIMIT, split_message
from maika.reply_helpers import (
    ANNOUNCE_PART_SEPARATOR,
    REPLY_PART_SEPARATOR,
    join_parts,
)


def test_separators():
    assert REPLY_PART_SEPARATOR == "\n"
    assert ANNOUNCE_PART_SEPARATOR == " "


def test_no_parts_leaves_main_unchanged():
    assert join_parts("Done.", [], REPLY_PART_SEPARATOR) == "Done."
    assert join_parts("Done.", [], ANNOUNCE_PART_SEPARATOR) == "Done."


def test_reply_parts_go_on_their_own_lines():
    text = join_parts(
        "Main.", ["Cleared 1.", "Note: a.", "Note: b."], REPLY_PART_SEPARATOR
    )
    assert text == "Main.\nCleared 1.\nNote: a.\nNote: b."


def test_announce_parts_stay_on_one_line():
    text = join_parts("Main.", ["Cleared 1."], ANNOUNCE_PART_SEPARATOR)
    assert text == "Main. Cleared 1."


def test_long_joined_reply_splits_at_part_boundaries():
    parts = ["p" * 900, "q" * 900, "r" * 900]
    text = join_parts("Main.", parts, REPLY_PART_SEPARATOR)

    pieces = split_message(text)

    assert len(text) > REPLY_PART_LIMIT
    assert pieces == ["Main.\n" + "p" * 900 + "\n" + "q" * 900, "r" * 900]
