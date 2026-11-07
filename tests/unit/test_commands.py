import pytest

from maika.commands import (
    DESCRIPTION_MAX_LENGTH,
    CommandCategory,
    CommandDefinition,
    CommandRegistry,
    UnknownCommandError,
    Visibility,
)
from maika.permissions import (
    Level,
    PermissionKey,
    PermissionRegistry,
    UnknownPermissionKeyError,
)

CAT_VIEW = PermissionKey("cat.view", Level.EVERYONE)
CAT_RETAG = PermissionKey("cat.retag", Level.TRUSTED)
FEED_SUBSCRIBE = PermissionKey("feed.subscribe", Level.MEMBER)
CHAT_TALK = PermissionKey("chat.talk", Level.EVERYONE)
MEMORY_VIEW = PermissionKey("memory.view", Level.EVERYONE, protected=True)


def define(
    name="cat",
    description="Show a cat photo.",
    category=CommandCategory.CATS,
    key=CAT_VIEW,
    help_text="Posts a random photo of the named cat.",
    example="/cat name:buttons",
):
    return CommandDefinition(
        name=name,
        description=description,
        category=category,
        key=key,
        help_text=help_text,
        example=example,
    )


# CommandDefinition


@pytest.mark.parametrize(
    "name",
    [
        "cat",
        "feed subscribe",
        "feed filter add",
        "add_cat",
        "remove-photo",
        "top10",
    ],
)
def test_valid_names(name):
    assert define(name=name).name == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "Cat",
        "/cat",
        " cat",
        "cat ",
        "feed  subscribe",
        "feed\tsubscribe",
        "feed.subscribe",
        "café",
        None,
    ],
)
def test_invalid_names(name):
    with pytest.raises(ValueError, match="lowercase words"):
        define(name=name)


@pytest.mark.parametrize("length", [1, DESCRIPTION_MAX_LENGTH])
def test_description_length_limits_accepted(length):
    assert len(define(description="x" * length).description) == length


@pytest.mark.parametrize(
    "description", ["", "   ", "x" * (DESCRIPTION_MAX_LENGTH + 1)]
)
def test_invalid_description(description):
    with pytest.raises(ValueError, match="description must be"):
        define(description=description)


@pytest.mark.parametrize("help_text", ["", "   "])
def test_empty_help_text(help_text):
    with pytest.raises(ValueError, match="help_text"):
        define(help_text=help_text)


@pytest.mark.parametrize("example", ["", "\n"])
def test_empty_example(example):
    with pytest.raises(ValueError, match="example"):
        define(example=example)


def test_category_must_be_enum():
    with pytest.raises(ValueError, match="CommandCategory"):
        define(category="Cats")


def test_key_must_be_permission_key():
    with pytest.raises(ValueError, match="PermissionKey"):
        define(key="cat.view")


def test_category_names():
    assert [category.value for category in CommandCategory] == [
        "Cats",
        "Reminders and time",
        "Feeds",
        "About me",
        "Fun",
        "Manage",
        "Owner",
    ]


# CommandRegistry


def test_duplicate_command_name_raises():
    with pytest.raises(ValueError, match="cat is defined more than once"):
        CommandRegistry([define(), define(description="Another.")])


def test_shared_equal_key_accepted():
    registry = CommandRegistry(
        [
            define(name="retag", key=CAT_RETAG),
            define(
                name="removecat",
                key=PermissionKey("cat.retag", Level.TRUSTED),
            ),
        ]
    )
    assert registry.get("removecat").key == CAT_RETAG


def test_shared_conflicting_key_rejected():
    with pytest.raises(ValueError, match="cat.retag is defined twice"):
        CommandRegistry(
            [
                define(name="retag", key=CAT_RETAG),
                define(
                    name="removecat",
                    key=PermissionKey("cat.retag", Level.ADMIN),
                ),
            ]
        )


def test_extra_key_conflicting_with_command_key_rejected():
    with pytest.raises(ValueError, match="cat.view is defined twice"):
        CommandRegistry(
            [define()],
            extra_keys=[PermissionKey("cat.view", Level.MEMBER)],
        )


def test_permission_registry_checks_owner_only_areas():
    with pytest.raises(ValueError, match="owner_only"):
        CommandRegistry(
            [
                define(
                    name="perm level",
                    key=PermissionKey("perm.level", Level.ADMIN),
                )
            ]
        )


def test_get_and_unknown():
    definition = define()
    registry = CommandRegistry([definition])

    assert registry.get("cat") is definition
    with pytest.raises(UnknownCommandError, match="'dog'") as error:
        registry.get("dog")
    assert error.value.name == "dog"


def test_commands_sorted_by_name():
    registry = CommandRegistry(
        [
            define(name="retag", key=CAT_RETAG),
            define(name="cat"),
            define(name="feed subscribe", key=FEED_SUBSCRIBE),
            define(name="addcat"),
        ]
    )
    assert [d.name for d in registry.commands()] == [
        "addcat",
        "cat",
        "feed subscribe",
        "retag",
    ]


def test_empty_registry():
    registry = CommandRegistry([])
    assert registry.commands() == ()


def test_commands_in_category():
    registry = CommandRegistry(
        [
            define(name="retag", category=CommandCategory.MANAGE),
            define(name="cat"),
            define(name="addcat"),
            define(
                name="feed subscribe",
                category=CommandCategory.FEEDS,
                key=FEED_SUBSCRIBE,
            ),
        ]
    )

    cats = registry.commands_in_category(CommandCategory.CATS)
    assert [d.name for d in cats] == ["addcat", "cat"]
    manage = registry.commands_in_category(CommandCategory.MANAGE)
    assert [d.name for d in manage] == ["retag"]
    assert registry.commands_in_category(CommandCategory.FUN) == ()


def test_permission_registry_holds_commands_and_extras():
    registry = CommandRegistry(
        [
            define(name="cat"),
            define(name="addcat"),
            define(name="retag", key=CAT_RETAG),
            define(name="memories", key=MEMORY_VIEW),
        ],
        extra_keys=[CHAT_TALK, CAT_VIEW],
    )

    permissions = registry.permission_registry()

    assert isinstance(permissions, PermissionRegistry)
    for key in (CAT_VIEW, CAT_RETAG, MEMORY_VIEW, CHAT_TALK):
        assert permissions.get(key.key) == key
    with pytest.raises(UnknownPermissionKeyError):
        permissions.get("feed.subscribe")
    # PermissionRegistry refuses duplicates, so building it at all
    # shows each key was added once.
    assert permissions is registry.permission_registry()


# Discord name limits


def test_three_word_name_accepted():
    assert define(name="feed filter add").name == "feed filter add"


def test_four_word_name_rejected():
    with pytest.raises(ValueError, match="has 4 words"):
        define(name="feed filter add more")


def test_32_character_word_accepted():
    name = "x" * 32
    assert define(name=name).name == name


def test_33_character_word_rejected():
    with pytest.raises(ValueError, match="has 33 characters"):
        define(name="feed " + "x" * 33)


@pytest.mark.parametrize("name", ["feed Subscribe", "", " ", "   "])
def test_uppercase_or_empty_name_rejected(name):
    with pytest.raises(ValueError, match="lowercase words"):
        define(name=name)


# Visibility


def test_visibility_defaults_to_ephemeral():
    assert define().visibility is Visibility.EPHEMERAL


def test_visibility_can_be_public():
    definition = CommandDefinition(
        name="cat",
        description="Show a cat photo.",
        category=CommandCategory.CATS,
        key=CAT_VIEW,
        help_text="Posts a random photo of the named cat.",
        example="/cat name:buttons",
        visibility=Visibility.PUBLIC,
    )
    assert definition.visibility is Visibility.PUBLIC


def test_visibility_must_be_enum():
    with pytest.raises(ValueError, match="must be a Visibility"):
        CommandDefinition(
            name="cat",
            description="Show a cat photo.",
            category=CommandCategory.CATS,
            key=CAT_VIEW,
            help_text="Posts a random photo of the named cat.",
            example="/cat name:buttons",
            visibility="public",
        )
