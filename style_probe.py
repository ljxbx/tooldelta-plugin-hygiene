"""Style and documentation probe.

Deliberately contains only STYLE / DOCUMENTATION class violations, so that
we can tell whether those two issue categories are enabled for this repo.
"""


def public_function_without_docstring(value):
    return value


class PublicClassWithoutDocstring:
    def __init__(self):
        self.value = 1

    def public_method_without_docstring(self):
        return self.value


# The next line is deliberately longer than 88 and longer than 100 characters to trigger FLK-E501 line-too-long.
VERY_LONG_ASSIGNMENT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
