import pytest

from maika.cooldowns import (
    CooldownScope,
    delete_expired_cooldowns,
    is_on_cooldown,
    try_start_cooldown,
)

NOW = 1_800_000_000
DURATION = 60
USER = CooldownScope.USER
ALEX = 1001
SAM = 1002
CHANNEL_ID = 2001
KEY = "nickname_use"


def expiry(connection, scope=USER, scope_id=ALEX, key=KEY):
    row = connection.execute(
        "SELECT expires_at FROM cooldowns "
        "WHERE scope = ? AND scope_id = ? AND key = ?",
        (scope.value, scope_id, key),
    ).fetchone()
    assert row is not None
    return row[0]


def start(
    connection, now, duration=DURATION, scope=USER, scope_id=ALEX, key=KEY
):
    return try_start_cooldown(connection, scope, scope_id, key, duration, now)


def active(connection, now, scope=USER, scope_id=ALEX, key=KEY):
    return is_on_cooldown(connection, scope, scope_id, key, now)


def test_enum_values():
    assert [scope.value for scope in CooldownScope] == [
        "user",
        "channel",
        "server",
        "global",
    ]


def test_first_start_gets_slot_and_stores_expiry(migrated_db):
    assert not active(migrated_db, NOW)

    assert start(migrated_db, NOW) is True

    assert expiry(migrated_db) == NOW + DURATION
    row = migrated_db.execute("SELECT scope, scope_id, key FROM cooldowns")
    assert tuple(row.fetchone()) == ("user", ALEX, KEY)
    assert active(migrated_db, NOW)


def test_second_start_while_active_is_refused(migrated_db):
    start(migrated_db, NOW)

    assert start(migrated_db, NOW + 30, duration=500) is False

    assert expiry(migrated_db) == NOW + DURATION
    assert active(migrated_db, NOW + 30)


def test_start_after_expiry_moves_expiry(migrated_db):
    start(migrated_db, NOW)
    later = NOW + DURATION + 15

    assert not active(migrated_db, later)
    assert start(migrated_db, later) is True

    assert expiry(migrated_db) == later + DURATION
    assert active(migrated_db, later)


def test_expiry_equal_to_now_counts_as_expired(migrated_db):
    start(migrated_db, NOW)
    at_expiry = NOW + DURATION

    assert active(migrated_db, at_expiry - 1)
    assert not active(migrated_db, at_expiry)
    assert start(migrated_db, at_expiry) is True
    assert expiry(migrated_db) == at_expiry + DURATION


def test_never_expiring_cooldown(migrated_db):
    assert start(migrated_db, NOW, duration=None) is True
    assert expiry(migrated_db) is None

    far_future = NOW + 10 * 365 * 24 * 60 * 60
    for later in (NOW, NOW + 1, far_future):
        assert active(migrated_db, later)
        assert start(migrated_db, later) is False

    assert delete_expired_cooldowns(migrated_db, far_future) == 0
    assert expiry(migrated_db) is None


def test_scopes_ids_and_keys_are_independent(migrated_db):
    start(migrated_db, NOW)

    assert not active(migrated_db, NOW, scope_id=SAM)
    assert start(migrated_db, NOW, scope_id=SAM) is True

    assert not active(migrated_db, NOW, key="memory_callback:14")
    assert start(migrated_db, NOW, key="memory_callback:14") is True

    assert not active(
        migrated_db, NOW, scope=CooldownScope.CHANNEL, scope_id=ALEX
    )
    assert (
        start(migrated_db, NOW, scope=CooldownScope.CHANNEL, scope_id=ALEX)
        is True
    )

    global_started = start(
        migrated_db, NOW, scope=CooldownScope.GLOBAL, scope_id=0
    )
    assert global_started is True
    server_started = start(
        migrated_db, NOW, scope=CooldownScope.SERVER, scope_id=ALEX
    )
    assert server_started is True

    count = migrated_db.execute("SELECT COUNT(*) FROM cooldowns").fetchone()
    assert count[0] == 6
    # The original is still active and still refuses a second start.
    assert start(migrated_db, NOW + 1) is False


def test_delete_expired_removes_only_expired(migrated_db):
    start(migrated_db, NOW, duration=10, key="short")
    start(migrated_db, NOW, duration=60, key="exact")
    start(migrated_db, NOW, duration=600, key="long")
    start(migrated_db, NOW, duration=None, key="forever")

    assert delete_expired_cooldowns(migrated_db, NOW + 60) == 2

    remaining = {
        row[0]
        for row in migrated_db.execute("SELECT key FROM cooldowns").fetchall()
    }
    assert remaining == {"long", "forever"}
    assert delete_expired_cooldowns(migrated_db, NOW + 60) == 0


def test_delete_expired_with_no_rows(migrated_db):
    assert delete_expired_cooldowns(migrated_db, NOW) == 0


# Validation


@pytest.mark.parametrize("scope_id", [1, -1, ALEX])
def test_global_requires_scope_id_zero(migrated_db, scope_id):
    with pytest.raises(ValueError, match="global cooldown must use"):
        start(migrated_db, NOW, scope=CooldownScope.GLOBAL, scope_id=scope_id)
    with pytest.raises(ValueError, match="global cooldown must use"):
        active(migrated_db, NOW, scope=CooldownScope.GLOBAL, scope_id=scope_id)


@pytest.mark.parametrize(
    "scope", [CooldownScope.USER, CooldownScope.CHANNEL, CooldownScope.SERVER]
)
@pytest.mark.parametrize("scope_id", [0, -5])
def test_other_scopes_require_positive_id(migrated_db, scope, scope_id):
    with pytest.raises(ValueError, match="positive scope_id"):
        start(migrated_db, NOW, scope=scope, scope_id=scope_id)
    with pytest.raises(ValueError, match="positive scope_id"):
        active(migrated_db, NOW, scope=scope, scope_id=scope_id)


@pytest.mark.parametrize("scope_id", [True, 1.0, "1001", None])
def test_scope_id_must_be_whole_number(migrated_db, scope_id):
    with pytest.raises(ValueError, match="whole number"):
        start(migrated_db, NOW, scope_id=scope_id)


@pytest.mark.parametrize("scope", ["user", None])
def test_scope_must_be_enum(migrated_db, scope):
    with pytest.raises(ValueError, match="CooldownScope"):
        start(migrated_db, NOW, scope=scope)


@pytest.mark.parametrize("key", ["", "   "])
def test_key_must_not_be_empty(migrated_db, key):
    with pytest.raises(ValueError, match="key"):
        start(migrated_db, NOW, key=key)
    with pytest.raises(ValueError, match="key"):
        active(migrated_db, NOW, key=key)


@pytest.mark.parametrize("duration", [0, -10, 1.5, True])
def test_duration_must_be_positive_or_none(migrated_db, duration):
    with pytest.raises(ValueError, match="duration_seconds"):
        start(migrated_db, NOW, duration=duration)


def test_validation_failure_writes_nothing(migrated_db):
    with pytest.raises(ValueError):
        start(migrated_db, NOW, duration=0)
    count = migrated_db.execute("SELECT COUNT(*) FROM cooldowns").fetchone()
    assert count[0] == 0
