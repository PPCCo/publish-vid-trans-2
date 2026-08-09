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
    "segments",
    "captions/translation-qa",
    "audio",
    "video",
    "reviews",
    "rights/evidence",
    "approvals",
    "artifacts",
    "packages",
    "chapters",
    "distribution/guides",
    "outputs",
    "tmp",
]

# A YouTube-derived video id looks like `yt-<11 chars>`; we accept any short
# filesystem-safe slug so the framework isn't YouTube-only. YouTube's 11-char ids are
# case-sensitive base64url (`[A-Za-z0-9_-]`, e.g. `YP0FDR7Wc-8`), so the id MUST allow
# uppercase — lowercasing it would break the mapping back to the real video. Keep the
# charset filesystem-safe and the leading char alphanumeric.
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


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
    def segments_dir(self) -> Path:
        return self.directory / "segments"

    @property
    def segments_manifest(self) -> Path:
        """Resolved-segments artifact: segments/segments.json (null selection = whole video)."""
        return self.directory / "segments" / "segments.json"

    def segment_clip_dir(self, index: int) -> Path:
        """Per-segment extracted media: segments/clips/<index>/ (audio.wav / video.mp4)."""
        return self.directory / "segments" / "clips" / str(index)

    @property
    def captions_dir(self) -> Path:
        return self.directory / "captions"

    @property
    def audio_dir(self) -> Path:
        return self.directory / "audio"

    @property
    def video_dir(self) -> Path:
        return self.directory / "video"

    @property
    def chapters_dir(self) -> Path:
        return self.directory / "chapters"

    @property
    def distribution_dir(self) -> Path:
        """Phase 6 workspace: platform packages, upload/promotion manifests, rendered guides.

        Gitignored (may hold large per-platform renders / draft copy); the JSON manifests are
        content-addressed artifacts registered through the CLI like every other deliverable."""
        return self.directory / "distribution"

    def notes_file(self, language: str) -> Path:
        """Per-language YouTube upload-description doc (plain text, human/AI-authored).

        When present, this is the authoritative description for that language edition —
        `_render_description` uses it verbatim (prepended before source/chapters) instead of the
        single shared `distribution.summary`. Lets each language carry its own summary + cited
        verses + tafsir. A distribution deliverable, not CLI-owned state."""
        return self.directory / "distribution" / "notes" / f"{language}.txt"

    @property
    def platform_package_manifest(self) -> Path:
        return self.directory / "distribution" / "platform-package.json"

    @property
    def upload_manifest(self) -> Path:
        return self.directory / "distribution" / "upload-manifest.json"

    @property
    def promotion_manifest(self) -> Path:
        return self.directory / "distribution" / "promotion-manifest.json"

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
