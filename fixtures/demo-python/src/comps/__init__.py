"""TEST ONLY: the Python code of the demo input. The entry imports it as `from comps import zone`."""


def zone() -> str:
    """The time zone of the machine."""
    return "Europe/Oslo"


def broken() -> str:
    """Raise an error, to show the file and the line in the message."""
    raise ValueError("comps broke")
