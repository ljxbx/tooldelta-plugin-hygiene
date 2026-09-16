"""Temporary DeepSource detection probe. Removed by the next commit.

This file deliberately contains one instance of each violation class that the
repository's DeepSource configuration is known to report.  It exists only to
prove that the newly added plugin directory is actually being analysed.
"""

import json
import os


DUPLICATE_KEYS = {
    "alpha": 1,
    "beta": 2,
    "alpha": 3,
}

named_lambda = lambda value: value + 1


class ProbeHost:
    def __init__(self):
        self.value = 1

    def _hidden(self):
        return self.value

    def set_late_attribute(self):
        self.late_attribute = 2
        return self.late_attribute


class ProbeUser:
    def run(self, host):
        return host._hidden()


def probe_unused_getattr(obj):
    return getattr(obj, "DUPLICATE_KEYS")


def probe_overlapping_exceptions():
    try:
        return json.dumps({})
    except (OSError, FileNotFoundError):
        return ""
