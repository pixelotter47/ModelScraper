import re


class LooseVersion:
    def __init__(self, vstring=None):
        if vstring is None:
            vstring = ""
        self.vstring = vstring
        self.version = self._parse(vstring)

    def _parse(self, vstring):
        components = []
        for part in re.split(r"[.\-+_]", vstring):
            if not part:
                continue
            for chunk in re.findall(r"\d+|[A-Za-z]+", part):
                if chunk.isdigit():
                    components.append(int(chunk))
                else:
                    components.append(chunk)
        return components

    def __repr__(self):
        return "LooseVersion ('%s')" % self.vstring

    def __str__(self):
        return self.vstring

    def _cmp(self, other):
        if isinstance(other, str):
            other = LooseVersion(other)
        if not isinstance(other, LooseVersion):
            return NotImplemented

        left = self.version
        right = other.version
        max_len = max(len(left), len(right))

        for idx in range(max_len):
            left_item = left[idx] if idx < len(left) else 0
            right_item = right[idx] if idx < len(right) else 0

            if left_item == right_item:
                continue

            if isinstance(left_item, int) and isinstance(right_item, int):
                return -1 if left_item < right_item else 1
            if isinstance(left_item, int) and isinstance(right_item, str):
                return 1
            if isinstance(left_item, str) and isinstance(right_item, int):
                return -1

            return -1 if str(left_item) < str(right_item) else 1

        return 0

    def __eq__(self, other):
        return self._cmp(other) == 0

    def __lt__(self, other):
        return self._cmp(other) < 0

    def __le__(self, other):
        return self._cmp(other) <= 0

    def __gt__(self, other):
        return self._cmp(other) > 0

    def __ge__(self, other):
        return self._cmp(other) >= 0
