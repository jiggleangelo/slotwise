import re

EMAIL_RE = re.compile(r'\b[A-Z0-9._%+-]{1,64}@[A-Z0-9.-]+\.[A-Z]{2,63}\b', re.I)
PHONE_RE = re.compile(r'(?<!\w)(?:\+?\d[\s().-]*){7,15}(?!\w)')


def contains_contact(value: str) -> bool:
    return bool(EMAIL_RE.search(value) or PHONE_RE.search(value))
