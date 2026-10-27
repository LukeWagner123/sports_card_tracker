NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def last_name(full_name: str) -> str:
    """'Marvin Harrison Jr.' -> 'Harrison'."""
    parts = [p for p in full_name.split() if p.lower() not in NAME_SUFFIXES]
    return parts[-1]
