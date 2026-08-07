"""`speed` verb: uniformly re-time ANY video (audio+video by the same factor), source
untouched, output beside the source as ``<stem>_<factor><suffix>`` unless --out given."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import media, speed  # noqa: E402
from video_translation_house.errors import VideoTranslationHouseError as VThError  # noqa: E402
from video_translation_house.util import executable  # noqa: E402

ffmpeg_required = pytest.mark.skipif(
    not (executable("ffmpeg") and executable("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)


def _fake_video(dest: Path, *, duration_ms: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    secs = f"{duration_ms / 1000:.3f}"
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={secs}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs}",
         "-c:v", "libx264", "-t", secs, "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dest)],
        capture_output=True, check=True,
    )
    return dest


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_default_dest_naming():
    src = Path("/tmp/some-dir/my-file-name.mp4")
    assert speed._default_dest(src, 1.25).name == "my-file-name_1.25.mp4"
    assert speed._default_dest(src, 2.0).name == "my-file-name_2.mp4"
    assert speed._default_dest(src, 1.5).name == "my-file-name_1.5.mp4"


@ffmpeg_required
def test_respeed_writes_new_file_source_untouched(tmp_path: Path):
    src = _fake_video(tmp_path / "clip.mp4", duration_ms=4000)
    before = _sha(src)
    out = speed.respeed(tmp_path, 1.25, str(src))
    dst = Path(out["dest"])
    # default dest is beside the source, factor-tagged
    assert dst == src.with_name("clip_1.25.mp4")
    assert dst.is_file()
    # source never modified
    assert _sha(src) == before
    assert out["source_untouched"] is True


@ffmpeg_required
def test_respeed_scales_duration(tmp_path: Path):
    src = _fake_video(tmp_path / "clip.mp4", duration_ms=4000)
    out = speed.respeed(tmp_path, 2.0, str(src), dest=str(tmp_path / "fast.mp4"))
    # 2x => ~half the duration; allow generous slack for keyframe/encoder rounding.
    assert out["src_duration_ms"] == pytest.approx(4000, abs=250)
    assert out["dest_duration_ms"] == pytest.approx(2000, abs=400)


@ffmpeg_required
def test_respeed_audio_video_stay_aligned(tmp_path: Path):
    src = _fake_video(tmp_path / "clip.mp4", duration_ms=4000)
    out = speed.respeed(tmp_path, 1.25, str(src), dest=str(tmp_path / "o.mp4"))
    dst = Path(out["dest"])
    summary = media.probe_summary(dst)
    # both streams present and re-encoded
    assert summary["video"].get("codec")
    assert summary["audio"].get("codec")


@ffmpeg_required
def test_respeed_refuses_to_overwrite_source(tmp_path: Path):
    src = _fake_video(tmp_path / "clip.mp4", duration_ms=2000)
    with pytest.raises(VThError):
        speed.respeed(tmp_path, 1.25, str(src), dest=str(src))


def test_respeed_rejects_nonpositive(tmp_path: Path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"not really a video")
    with pytest.raises(VThError):
        speed.respeed(tmp_path, 0, str(src))


def test_respeed_missing_source(tmp_path: Path):
    with pytest.raises(VThError):
        speed.respeed(tmp_path, 1.25, str(tmp_path / "nope.mp4"))
