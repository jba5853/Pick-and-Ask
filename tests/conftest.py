"""Shared test setup."""

import os

# Tests never write to the real insights database; tests that need it make their own.
os.environ["INSIGHTS_DISABLED"] = "1"
