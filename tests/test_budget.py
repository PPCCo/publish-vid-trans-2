"""Budget guard: the vendor (billed) TTS spend ceiling in budget/ledger.json.

Local engines (kokoro/piper/xtts/chatterbox) never touch this. Only vendor providers
(elevenlabs/azure/google) are guarded, and the ceiling defaults to 0.0 (no spend
permitted) unless a human raises it via company.local.json.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / ".claude" / "scripts"))

from video_translation_house import budget  # noqa: E402
from video_translation_house.errors import BudgetExceededError  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".claude" / "CLAUDE.md").write_text("test repo\n")
    shutil.copytree(REPO / ".claude" / "config", root / ".claude" / "config")
    return root


def _raise_ceiling(root: Path, usd: float) -> None:
    (root / ".claude" / "config" / "company.local.json").write_text(
        json.dumps({"budget": {"vendor_spend_ceiling_usd": usd}})
    )


def test_local_provider_is_never_guarded(repo: Path):
    result = budget.check_and_record(repo, "piper")
    assert result == {"guarded": False, "provider": "piper"}
    assert not budget.ledger_path(repo).exists()


def test_vendor_call_blocked_at_zero_ceiling_by_default(repo: Path):
    assert budget.ceiling_usd(repo) == 0.0
    with pytest.raises(BudgetExceededError):
        budget.check_and_record(repo, "azure", project_id="yt-vid00000001", language="en")
    # A refused call must not be recorded.
    assert budget.spent_usd(repo) == 0.0


def test_vendor_call_recorded_once_ceiling_raised(repo: Path):
    _raise_ceiling(repo, 1.0)
    result = budget.check_and_record(repo, "azure", project_id="yt-vid00000001", language="en")
    assert result["guarded"] is True
    assert result["spent_usd"] == pytest.approx(budget.estimated_cost_usd(repo, "azure"))

    status = budget.status(repo)
    assert status["vendor_spend_ceiling_usd"] == 1.0
    assert status["spent_usd"] == pytest.approx(result["spent_usd"])
    assert status["remaining_usd"] == pytest.approx(1.0 - result["spent_usd"])

    ledger = json.loads(budget.ledger_path(repo).read_text())
    assert len(ledger["calls"]) == 1
    assert ledger["calls"][0]["provider"] == "azure"
    assert ledger["calls"][0]["project_id"] == "yt-vid00000001"


def test_vendor_calls_accumulate_and_eventually_exceed_ceiling(repo: Path):
    per_call = budget.estimated_cost_usd(repo, "google")
    _raise_ceiling(repo, per_call * 1.5)

    budget.check_and_record(repo, "google")  # first call fits
    with pytest.raises(BudgetExceededError):
        budget.check_and_record(repo, "google")  # second would exceed the ceiling

    assert budget.spent_usd(repo) == pytest.approx(per_call)


def test_unknown_provider_is_not_vendor_guarded(repo: Path):
    assert budget.is_vendor_provider("kokoro") is False
    assert budget.is_vendor_provider("some-future-engine") is False
