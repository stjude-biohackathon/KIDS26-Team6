"""Shell hook scripts, shipped as package data.

These are read with ``importlib.resources`` and materialized into
``~/.wfrec/hooks/`` with the runtime directory substituted in. They are data
rather than code: nothing here is importable Python.
"""
