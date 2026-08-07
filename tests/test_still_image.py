"""Per-language still image (TASK 1) + per-language playback speed (TASK 3): config verbs
(`project set-image` / `project set-speed`, init maps) and their mux integration — an
image language shows one static frame for the dub's length; a speed factor uniformly
re-times the finished dubbed.mp4."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

# Reuse the packaging harness that drives a project to VIDEO_MUX-ready.
from test_packaging import VID, _drive_to_audio_qa_gate  # noqa: E402
from video_translation_house import media, packaging, project  # noqa: E402
from video_translation_house.errors import ConfigurationError  # noqa: E402
from video_translation_house.util import executable, load_yaml  # noqa: E402

ffmpeg_required = pytest.mark.skipif(
    not (executable("ffmpeg") and executable("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "schemas", root / ".claude" / "schemas")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    (root / "projects").mkdir()
    return root


def _make_image(dest: Path, *, size: str = "640x360") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s={size}:d=1",
         "-frames:v", "1", str(dest)],
        capture_output=True, check=True,
    )
    return dest


# --- config verbs ------------------------------------------------------------

def test_init_stores_images_and_speeds(repo: Path, tmp_path: Path):
    img = _make_image(tmp_path / "a.png")
    project.init_project(
        repo, VID, url="https://youtube.com/watch?v=vid00000005",
        target_languages=["en", "ur"], audio_languages=["en", "ur"],
        images={"ur": str(img)}, playback_speed={"en": 1.25, "ur": 1.0},
    )
    cfg = load_yaml(repo / "projects" / VID / "project.yaml")
    assert cfg["images"]["ur"] == str(img)
    # 1.0 is the default and is dropped; 1.25 is stored.
    assert cfg["playback_speed"] == {"en": 1.25}


def test_init_rejects_image_for_non_target(repo: Path, tmp_path: Path):
    img = _make_image(tmp_path / "a.png")
    with pytest.raises(ConfigurationError):
        project.init_project(
            repo, VID, url="https://x/y", target_languages=["en"],
            images={"zh": str(img)},
        )


def test_init_rejects_missing_image(repo: Path):
    with pytest.raises(ConfigurationError):
        project.init_project(
            repo, VID, url="https://x/y", target_languages=["en"],
            images={"en": "/no/such/file.png"},
        )


def test_init_rejects_bad_speed(repo: Path):
    with pytest.raises(ConfigurationError):
        project.init_project(
            repo, VID, url="https://x/y", target_languages=["en"],
            playback_speed={"en": 99.0},
        )


def test_set_image_and_clear(repo: Path, tmp_path: Path):
    img = _make_image(tmp_path / "a.png")
    project.init_project(repo, VID, url="https://x/y", target_languages=["en", "ur"])
    out = project.set_image(repo, VID, language="ur", path=str(img))
    assert out["action"] == "set"
    assert load_yaml(repo / "projects" / VID / "project.yaml")["images"]["ur"] == str(img)
    out = project.set_image(repo, VID, language="ur", clear=True)
    assert out["action"] == "cleared"
    assert load_yaml(repo / "projects" / VID / "project.yaml")["images"] == {}


def test_set_speed_and_reset(repo: Path):
    project.init_project(repo, VID, url="https://x/y", target_languages=["en"])
    project.set_speed(repo, VID, language="en", factor=1.5)
    assert load_yaml(repo / "projects" / VID / "project.yaml")["playback_speed"] == {"en": 1.5}
    # factor 1.0 resets (removes the entry)
    project.set_speed(repo, VID, language="en", factor=1.0)
    assert load_yaml(repo / "projects" / VID / "project.yaml")["playback_speed"] == {}


def test_set_image_rejects_non_target(repo: Path, tmp_path: Path):
    img = _make_image(tmp_path / "a.png")
    project.init_project(repo, VID, url="https://x/y", target_languages=["en"])
    with pytest.raises(ConfigurationError):
        project.set_image(repo, VID, language="zh", path=str(img))


def test_parse_lang_map():
    assert project.parse_lang_map("en=1.25,ur=1.5", kind="speeds") == {"en": "1.25", "ur": "1.5"}
    assert project.parse_lang_map("", kind="images") == {}
    with pytest.raises(ConfigurationError):
        project.parse_lang_map("en", kind="images")  # missing '='


# --- mux integration ---------------------------------------------------------

@ffmpeg_required
def test_mux_image_language_uses_still_frame(repo: Path, tmp_path: Path):
    img = _make_image(tmp_path / "still.png")
    _drive_to_audio_qa_gate(repo)  # en track, source video present
    project.set_image(repo, VID, language="en", path=str(img))
    out = packaging.run_mux(repo, VID, "en", advance=True)
    assert out["still_image"] == str(img)
    dubbed = repo / "projects" / VID / "video" / "en" / "dubbed.mp4"
    assert dubbed.is_file()
    summary = media.probe_summary(dubbed)
    assert summary["video"].get("codec") == "h264"
    assert summary["audio"].get("codec")
    # picture length tracks the dub, not the (6s) source video's own frames.
    assert media.audio_duration_ms(dubbed) == pytest.approx(6000, abs=1200)


@ffmpeg_required
def test_mux_image_language_without_source_video(repo: Path, tmp_path: Path):
    """An image language needs no source video — deleting it must not break the mux."""
    img = _make_image(tmp_path / "still.png")
    _drive_to_audio_qa_gate(repo)
    project.set_image(repo, VID, language="en", path=str(img))
    # remove the source media entirely; fetch is disabled in tests, so a source-needing path
    # would raise. The still-image path must NOT need it.
    for child in (repo / "projects" / VID / "source").glob("*.mp4"):
        child.unlink()
    out = packaging.run_mux(repo, VID, "en", advance=True)
    assert (repo / "projects" / VID / "video" / "en" / "dubbed.mp4").is_file()
    assert out["still_image"] == str(img)


@ffmpeg_required
def test_mux_applies_playback_speed(repo: Path):
    _drive_to_audio_qa_gate(repo)
    project.set_speed(repo, VID, language="en", factor=2.0)
    out = packaging.run_mux(repo, VID, "en", advance=True)
    assert out["playback_speed"] == 2.0
    dubbed = repo / "projects" / VID / "video" / "en" / "dubbed.mp4"
    # 2x uniform speedup => ~half the (6s) runtime.
    assert media.audio_duration_ms(dubbed) == pytest.approx(3000, abs=700)
