"""Phase 6 tests: the distribution & promotion lifecycle (READY_FOR_REVIEW -> MONITORING).

Driven through the no-network dry-run path — no external write ever occurs. Covers:
  * the full lifecycle reaching MONITORING via package -> release_authorization ->
    youtube (dry-run) -> promote queue -> promotion_review -> promote publish (dry-run);
  * both human gates (release_authorization, promotion_review) block until approved;
  * the rights re-check refuses to package/upload under non-distributable rights;
  * channel resolution + framework-validate rejecting two default:true channels.

The mux step needs ffmpeg; tests skip cleanly where it is absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import (  # noqa: E402
    approvals,
    chapters,
    distribution,
    dubbing,
    langid,
    packaging,
    project,
    rights,
    state,
    transcript,
    transcript_qa,
    translate,
    validation,
)
from video_translation_house import segments as segments_mod  # noqa: E402
from video_translation_house.engines import asr  # noqa: E402
from video_translation_house.errors import DistributionError, StateTransitionError  # noqa: E402
from video_translation_house.util import executable  # noqa: E402

VID = "yt-vid00000007"

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
    shutil.copytree(REPO / "docs", root / "docs")  # guide templates for the renderer
    (root / "projects").mkdir()
    return root


def _silence_wav(dest: Path, *, duration_ms: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
         "-t", f"{duration_ms / 1000:.3f}", "-ac", "1", "-ar", "24000",
         "-c:a", "pcm_s16le", str(dest)],
        capture_output=True, check=True,
    )
    return dest


def _fake_source_video(dest: Path, *, duration_ms: int) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    secs = f"{duration_ms / 1000:.3f}"
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={secs}",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={secs}",
         "-c:v", "libx264", "-t", secs, "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(dest)],
        capture_output=True, check=True,
    )
    return dest


def _drive_to_ready_for_review(root: Path, *, distributable: bool = True) -> None:
    """Take a project all the way to READY_FOR_REVIEW (with distributable rights by default)."""
    project.init_project(root, VID, url="https://youtube.com/watch?v=vid00000007",
                         target_languages=["en"], audio_languages=["en"])
    paths = root / "projects" / VID
    _fake_source_video(paths / "source" / "video.mp4", duration_ms=6000)
    (paths / "source" / "metadata.json").write_text(json.dumps({
        "title": "Test speech", "channel": "Test channel",
        "url": "https://youtube.com/watch?v=vid00000007", "duration_seconds": 6.0,
    }))

    state.transition(root, VID, "LANGUAGE_ID", "agent")
    langid.set_language(root, VID, "fa", source="manual", confidence=0.9)
    state.transition(root, VID, "TRANSCRIPTION", "agent")
    result = asr.ASRResult(
        provider="manual", model=None, language="fa",
        cues=[
            asr.TranscriptCue(0, 0, 2500, "جمله اول.", confidence=-0.2),
            asr.TranscriptCue(1, 2500, 6000, "جمله دوم اینجاست.", confidence=-0.3),
        ],
        has_word_timing=False, duration_seconds=6.0,
    )
    doc = transcript.build_transcript_doc(VID, "fa", result, actor="agent")
    transcript.write_transcript(root, VID, doc, actor="agent")
    state.transition(root, VID, "TRANSCRIPT_QA_GATE", "agent")
    transcript_qa.run_transcript_qa(root, VID)
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    src_hash = next(a["sha256"] for a in manifest["artifacts"] if a["type"] == "transcript")
    approvals.grant_approval(root, VID, "transcript_qa", "human", [src_hash],
                             scope="transcript", language=None)
    state.transition(root, VID, "SEGMENT_RESOLUTION", "human")
    segments_mod.run_resolve(root, VID, advance=True)

    translate.export_worksheet(root, VID, "en")
    ws_path = paths / "captions" / "en.worksheet.json"
    ws = json.loads(ws_path.read_text())
    for cue in ws["cues"]:
        cue["target_text"] = f"English line {cue['id']}."
    ws_path.write_text(json.dumps(ws, ensure_ascii=False))
    out = translate.import_worksheet(root, VID, "en", advance=True)
    approvals.grant_approval(root, VID, "translation_qa", "human",
                             [out["artifact"]["sha256"]], scope="captions", language="en")
    translate.run_translation_qa(root, VID)
    state.transition(root, VID, "CAPTION_TIMING", "human")
    translate.build_captions(root, VID, "en")
    translate.run_caption_validation(root, VID)
    state.transition(root, VID, "CAPTION_VALIDATION", "agent")

    src = _silence_wav(paths / "tmp" / "dub_src.wav", duration_ms=6000)
    dubbing.import_dub(root, VID, "en", from_path=src, advance=True)
    dubbing.run_audio_qa(root, VID)
    state.transition(root, VID, "AUDIO_SYNC_ADJUST", "agent")
    state.transition(root, VID, "AUDIO_QA_GATE", "agent")
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    dub_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dub-wav" and a.get("language") == "en")
    approvals.grant_approval(root, VID, "audio_qa", "human", [dub_hash],
                             scope="audio", language="en")
    state.transition(root, VID, "VIDEO_MUX", "human")
    packaging.run_mux(root, VID, "en", advance=True)
    state.transition(root, VID, "FINAL_QA_GATE", "agent")
    packaging.run_final_qa(root, VID)
    manifest = json.loads((paths / "artifacts" / "manifest.json").read_text())
    vid_hash = next(a["sha256"] for a in manifest["artifacts"]
                    if a["type"] == "dubbed-video" and a.get("language") == "en")
    approvals.grant_approval(root, VID, "final_qa", "human", [vid_hash],
                             scope="video", language=None)
    state.transition(root, VID, "PACKAGE", "human")
    packaging.run_package(root, VID)
    if distributable:
        rights.set_rights(root, VID, status="self-authored", reviewer="human")
        state.transition(root, VID, "READY_FOR_REVIEW", "human")


def _authorize_release(root: Path) -> str:
    """Grant release_authorization bound to the active dubbed-video hash. Returns the hash."""
    state_doc = json.loads((root / "projects" / VID / "state.json").read_text())
    vid_hash = state_doc["active_artifacts"]["dubbed-video@en"]
    approvals.grant_approval(root, VID, "release_authorization", "human", [vid_hash],
                             scope="video", language=None)
    return vid_hash


# --- packaging + release gate ------------------------------------------------

@ffmpeg_required
def test_platform_packaging_binds_video_and_chapters(repo: Path):
    _drive_to_ready_for_review(repo)
    # Add chapters so the description carries timecodes.
    state.transition(repo, VID, "PLATFORM_PACKAGING", "human")
    chapters.export_chapter_worksheet(repo, VID, "en")
    ws_path = repo / "projects" / VID / "chapters" / "en.chapters-worksheet.json"
    ws = json.loads(ws_path.read_text())
    ws["chapters"] = [{"start_ms": 0, "title": "Intro"}]
    ws_path.write_text(json.dumps(ws))
    chapters.import_chapter_worksheet(repo, VID, "en")

    out = distribution.run_platform_packaging(repo, VID, advance=True)
    assert out["decision"] == "PASS"
    assert out["distributable"] is True
    target = out["targets"][0]
    assert target["language"] == "en"
    assert target["video_sha256"].startswith("sha256:")
    assert "0:00 Intro" in target["description"]  # chapter timecodes appended
    assert target["channel_config_id"] == "default-en"
    # Per-project guide rendered from the template with video-specific fields filled.
    guide = repo / "projects" / VID / target["guide_path"]
    assert guide.is_file() and "{{TITLE}}" not in guide.read_text()
    # Advanced to the human gate.
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "RELEASE_AUTHORIZATION"


@ffmpeg_required
def test_release_authorization_gate_blocks_until_approved(repo: Path):
    _drive_to_ready_for_review(repo)
    state.transition(repo, VID, "PLATFORM_PACKAGING", "human")
    # Entering RELEASE_AUTHORIZATION (the human-review state) is free; the release_authorization
    # gate guards the EXIT edge RELEASE_AUTHORIZATION -> YOUTUBE_UPLOAD.
    out = distribution.run_platform_packaging(repo, VID, advance=True)
    assert out["advanced_to"] == "RELEASE_AUTHORIZATION"
    # Without the approval, the exit to YOUTUBE_UPLOAD is blocked.
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "YOUTUBE_UPLOAD", "agent")
    assert "release_authorization approval required" in str(exc.value)
    # Grant the approval bound to the exact final-video hash -> the edge opens.
    _authorize_release(repo)
    state.transition(repo, VID, "YOUTUBE_UPLOAD", "agent")
    st = json.loads((repo / "projects" / VID / "state.json").read_text())
    assert st["current_state"] == "YOUTUBE_UPLOAD"


@ffmpeg_required
def test_rights_recheck_refuses_upload_when_not_distributable(repo: Path):
    _drive_to_ready_for_review(repo)
    state.transition(repo, VID, "PLATFORM_PACKAGING", "human")
    distribution.run_platform_packaging(repo, VID)
    _authorize_release(repo)
    state.transition(repo, VID, "RELEASE_AUTHORIZATION", "agent")
    state.transition(repo, VID, "YOUTUBE_UPLOAD", "agent")
    # A human flips rights to do-not-distribute after authorization; upload must refuse.
    rights.set_rights(repo, VID, status="do-not-distribute", reviewer="human")
    with pytest.raises(DistributionError):
        distribution.run_youtube_upload(repo, VID, "en", dry_run=True)


# --- full lifecycle to MONITORING (all dry-run) ------------------------------

@ffmpeg_required
def test_full_distribution_lifecycle_dry_run_reaches_monitoring(repo: Path):
    _drive_to_ready_for_review(repo)
    state.transition(repo, VID, "PLATFORM_PACKAGING", "human")
    distribution.run_platform_packaging(repo, VID)
    _authorize_release(repo)
    state.transition(repo, VID, "RELEASE_AUTHORIZATION", "agent")
    state.transition(repo, VID, "YOUTUBE_UPLOAD", "agent")

    # Dry-run upload: prepared, no advance (advance only on a live upload).
    up = distribution.run_youtube_upload(repo, VID, "en", dry_run=True)
    assert up["dry_run"] is True and up["record"]["status"] == "prepared"
    assert up["advanced_to"] == "YOUTUBE_UPLOAD"
    # A human advances the (non-gated) edge to PROMOTION_QUEUE.
    state.transition(repo, VID, "PROMOTION_QUEUE", "agent")

    q = distribution.run_promotion_queue(repo, VID, advance=True)
    assert q["advanced_to"] == "PROMOTION_REVIEW"
    # Manual-only config: every platform is a checklist item (none enabled by default).
    assert all(p["status"] == "checklist" for p in q["posts"])

    # promotion_review gate blocks publish until approved.
    with pytest.raises(DistributionError):
        distribution.run_promotion_publish(repo, VID, dry_run=True)
    approvals.grant_approval(repo, VID, "promotion_review", "human",
                             [q["artifact"]["sha256"]], scope="promotion", language=None)
    pub = distribution.run_promotion_publish(repo, VID, dry_run=True, advance=True)
    # Dry-run: advances through the gate to PROMOTION_PUBLISHED but NOT to MONITORING.
    assert pub["advanced_to"] == "PROMOTION_PUBLISHED"

    # A live run would advance to MONITORING; assert the terminal edge is reachable.
    assert "MONITORING" in state.allowed_transitions(repo, "PROMOTION_PUBLISHED")


@ffmpeg_required
def test_promotion_queue_enabled_platform_is_queued(repo: Path):
    # Enable Telegram so at least one automatable post is queued (still dry-run at publish).
    cfg_path = repo / ".claude" / "config" / "promotion.config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["platforms"]["telegram"]["enabled"] = True
    cfg_path.write_text(json.dumps(cfg))

    _drive_to_ready_for_review(repo)
    state.transition(repo, VID, "PLATFORM_PACKAGING", "human")
    distribution.run_platform_packaging(repo, VID)
    _authorize_release(repo)
    state.transition(repo, VID, "RELEASE_AUTHORIZATION", "agent")
    state.transition(repo, VID, "YOUTUBE_UPLOAD", "agent")
    state.transition(repo, VID, "PROMOTION_QUEUE", "agent")
    q = distribution.run_promotion_queue(repo, VID)
    tg = next(p for p in q["posts"] if p["platform"] == "telegram")
    assert tg["status"] == "queued" and tg["automatable"] is True


# --- rights precede distribution ---------------------------------------------

@ffmpeg_required
def test_ready_for_review_blocked_under_unreviewed_rights(repo: Path):
    _drive_to_ready_for_review(repo, distributable=False)
    # Sits at PACKAGE with unreviewed rights; the terminal edge is blocked.
    with pytest.raises(StateTransitionError) as exc:
        state.transition(repo, VID, "READY_FOR_REVIEW", "agent")
    assert "rights_status" in str(exc.value)


# --- channel resolution + framework validate ---------------------------------

def test_resolve_channel_prefers_default_then_first():
    cfg = {"channels": [
        {"id": "en-a", "platform": "youtube", "language": "en", "default": False,
         "channelId": "A"},
        {"id": "en-b", "platform": "youtube", "language": "en", "default": True,
         "channelId": "B"},
    ]}
    chosen = distribution._resolve_channel(cfg, "en", None)
    assert chosen["id"] == "en-b"  # default wins
    # No default -> first for the language.
    cfg2 = {"channels": [{"id": "en-a", "platform": "youtube", "language": "en",
                          "channelId": "A"}]}
    assert distribution._resolve_channel(cfg2, "en", None)["id"] == "en-a"
    # Unknown language -> None.
    assert distribution._resolve_channel(cfg, "xx", None) is None


def test_resolve_channel_project_override_wins():
    cfg = {"channels": [{"id": "en-b", "platform": "youtube", "language": "en",
                         "default": True, "channelId": "B", "credentials_ref": "YT_DEFAULT",
                         "privacy_default": "private", "category_id": "25", "playlist_id": None}]}
    chosen = distribution._resolve_channel(cfg, "en", {"channelId": "OVERRIDE"})
    assert chosen["channelId"] == "OVERRIDE"


def test_framework_validate_rejects_two_default_channels(repo: Path):
    cfg_path = repo / ".claude" / "config" / "channels.config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["channels"].append({
        "id": "default-en-2", "platform": "youtube", "channelId": "OTHER",
        "language": "en", "default": True, "credentials_ref": "YT_DEFAULT",
        "privacy_default": "private", "category_id": "25", "playlist_id": None,
    })
    cfg_path.write_text(json.dumps(cfg))
    report = validation.validate_framework(repo)
    assert report["valid"] is False
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["channels-single-default-per-language"]["status"] == "fail"


def test_framework_validate_clean_by_default(repo: Path):
    report = validation.validate_framework(repo)
    assert report["valid"] is True
