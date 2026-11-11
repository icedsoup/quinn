from pathlib import Path

from Levenshtein import ratio


def isSomething(text, keywords, accuracy):
    if isinstance(keywords, str):
        return ratio(text, keywords) >= accuracy
    return any(ratio(text, keyword) >= accuracy for keyword in keywords)


def isSomethingChar(char_text, anime_text, keywords, accuracy):
    return any(
        name and ratio(char_text, name) >= accuracy
        and (anime is None or ratio(anime_text, anime) >= accuracy)
        for name, anime in keywords
    )


class FileWatch:
    def __init__(self, path):
        self.path = Path(path)
        self._stamp = self._modified_at()

    def _modified_at(self):
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def watch(self):
        stamp = self._modified_at()
        changed = stamp is not None and stamp != self._stamp
        self._stamp = stamp
        return changed
