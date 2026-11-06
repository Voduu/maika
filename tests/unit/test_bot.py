from maika.bot import format_shutdown_line, format_startup_line


def test_startup_line_test_environment():
    line = format_startup_line("test", "0.1.0", "2.7.1")
    assert line == "Maika (test) is online. Version 0.1.0, discord.py 2.7.1."


def test_startup_line_prod_environment():
    line = format_startup_line("prod", "1.2.3", "2.8.0")
    assert line == "Maika (prod) is online. Version 1.2.3, discord.py 2.8.0."


def test_shutdown_line_test_environment():
    assert format_shutdown_line("test") == "Maika (test) is shutting down."


def test_shutdown_line_prod_environment():
    assert format_shutdown_line("prod") == "Maika (prod) is shutting down."
