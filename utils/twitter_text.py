"""Conservative X weighted-length splitting (CJK counts twice, web links 23).

Unicode weight ranges: https://github.com/twitter/twitter-text/blob/master/config/v3.json
Emoji sequences are counted per code point, which may split sooner than X requires.
"""

import re
import unicodedata

_URL_OR_CHAR = re.compile(
    r"https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::[0-9]+)?"
    r"(?:[/?#][A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?|.",
    re.DOTALL,
)


def _weight(token: str) -> int:
    if len(token) > 1:
        # Reserve punctuation too; X may exclude it from the shortened link.
        return 23 + len(token) - len(token.rstrip(".,!?;:)'\""))
    point = ord(token)
    return (
        1
        if point <= 0x10FF
        or 0x2000 <= point <= 0x200D
        or 0x2010 <= point <= 0x201F
        or 0x2032 <= point <= 0x2037
        else 2
    )


def split_x_text(text: str, max_weight: int = 280) -> list[str]:
    chunks = []
    current: list[str] = []
    weight = 0
    for match in _URL_OR_CHAR.finditer(unicodedata.normalize("NFC", text.strip())):
        token = match.group()
        token_weight = _weight(token)
        if token_weight > max_weight:
            raise ValueError("X text limit is too small for a link")
        if current and weight + token_weight > max_weight:
            if chunk := "".join(current).strip():
                chunks.append(chunk)
            current = []
            weight = 0
        current.append(token)
        weight += token_weight
    if chunk := "".join(current).strip():
        chunks.append(chunk)
    return chunks
