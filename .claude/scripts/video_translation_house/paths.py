from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ProjectNotFoundError
from .util import ensure_within

# Fixed directory skeleton created at `project init`, mirroring plan §2. Per-language
# subdirectories under captions/audio/video are created lazily as target languages are
# added, but the parents exist from the start so tooling never races on mkdir.
PROJECT_DIRS = [
    "source",
    "transcript",
    "captions/translation-qa",
    "audio",
    "video",
    "reviews",
    "rights/evidence",
    "approvals",
    "artifacts",
    "packages",
    "outputs",
    "tmp",
]

# A YouTube-derived video id looks like `yt-<11 chars>`; we accept any short
# filesystem-safe slug so the framework isn't YouTube-only.
VIDEO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def is_valid_video_id(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.match(video_id))


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    project_id: str

    @property
    def directory(self) -> Path:
        return self.root / "projects" / self.project_id

    @property
    def config(self) -> Path:
        return self.directory / "project.yaml"

    @property
    def state(self) -> Path:
        return self.directory / "state.json"

    @property
    def events(self) -> Path:
        return self.directory / "events.ndjson"

    @property
    def lock(self) -> Path:
        return self.directory / ".lock"

    @property
    def artifact_manifest(self) -> Path:
        return self.directory / "artifacts" / "manifest.json"

    @property
    def rights_record(self) -> Path:
        return self.directory / "rights" / "record.json"

    @property
    def source_dir(self) -> Path:
        return self.directory / "source"

    @property
    def transcript_dir(self) -> Path:
        return self.directory / "transcript"

    @property
    def captions_dir(self) -> Path:
        return self.directory / "captions"

    @property
    def audio_dir(self) -> Path:
        return self.directory / "audio"

    @property
    def video_dir(self) -> Path:
        return self.directory / "video"

    def gate_report(self, gate: str) -> Path:
        """Path to the latest gate report a state blocker reads (decision must be PASS)."""
        return self.directory / "reviews" / f"{gate}-gate-latest.json"

    def require(self) -> ProjectPaths:
        if not self.config.is_file() or not self.state.is_file():
            raise ProjectNotFoundError(f"Project not found: {self.project_id}")
        return self

    def resolve_inside(self, relative_or_absolute: str | Path) -> Path:
        p = Path(relative_or_absolute)
        if not p.is_absolute():
            p = self.directory / p
        return ensure_within(p, self.directory)
