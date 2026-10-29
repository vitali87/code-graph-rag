# Pass/fail marks, shared by every table that prints one: `cgr doctor`
# (#1910) and the query result table (#1914) both reach the terminal's
# codec through these names, and they are defined here ONCE so the two
# cannot drift into different alphabets.
#
# Rich swaps box-drawing characters for ASCII on a stream that cannot
# encode them but leaves CELL TEXT alone, so a glyph written into a cell
# arrives at the codec unchanged and a code page that lacks it raises
# UnicodeEncodeError instead of printing. Both reports came from a CP950
# Windows terminal, where `✓` failed before a single row was shown.
#
# The ASCII pair is four characters wide so a column of mixed marks stays
# aligned. Do not redefine these in another constants module: `constants/
# __init__.py` star-imports every submodule, so a second definition is
# silently shadowed by import order rather than flagged as a conflict.
HEALTH_MARK_PASS = "✓"
HEALTH_MARK_FAIL = "✗"
HEALTH_MARK_PASS_ASCII = "PASS"
HEALTH_MARK_FAIL_ASCII = "FAIL"
