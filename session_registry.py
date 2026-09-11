"""Per-platform session ownership and containment validation."""

from __future__ import annotations

from pathlib import Path


class SessionPathError(ValueError):
    pass


class SessionRegistry:
    def __init__(self, roots):
        self._roots = {
            platform: Path(path).resolve()
            for platform, path in roots.items()
        }
        self._sessions = {platform: "" for platform in roots}

    def set(self, platform, path):
        if platform not in self._roots:
            raise SessionPathError("unknown platform")
        if not path:
            self._sessions[platform] = ""
            return ""
        candidate = Path(path).resolve()
        root = self._roots[platform]
        if candidate == root or not candidate.is_relative_to(root):
            raise SessionPathError(
                f"session path is outside the {platform} session root"
            )
        self._sessions[platform] = str(candidate)
        return str(candidate)

    def get(self, platform):
        if platform not in self._sessions:
            raise SessionPathError("unknown platform")
        return self._sessions[platform]

    def root(self, platform):
        if platform not in self._roots:
            raise SessionPathError("unknown platform")
        return str(self._roots[platform])

    def snapshot(self):
        return dict(self._sessions)
