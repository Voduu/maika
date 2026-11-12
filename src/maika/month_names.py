"""English month names and the message for a day past a month's end.

Shared by the /birthday command and the reminder time parser. No
Discord or database code here.
"""

# In month order, so MONTH_NAMES[month - 1] is the month's name. Fixed
# English names rather than calendar.month_name, which follows the
# machine's locale.
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

MONTH_TOO_SHORT_MESSAGE = "{month_name} has only {days} days."
