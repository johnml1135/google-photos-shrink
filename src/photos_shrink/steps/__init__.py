"""One module per step of the run, each a command of `photos-shrink`.

A step owns its argument parsing and its reporting; the checks that decide
what may be encoded, uploaded or trashed live in the package beside them, so
a step is the thin part and the rules are the tested part.
"""
