from __future__ import annotations


class VideoTranslationHouseError(Exception):
    """Base class for all framework errors."""


class ConfigurationError(VideoTranslationHouseError):
    """Repository or config could not be located or is invalid."""


class SecurityError(VideoTranslationHouseError):
    """An operation would escape the sanctioned boundary (path traversal, egress)."""


class ProjectNotFoundError(VideoTranslationHouseError):
    """Referenced project does not exist on disk."""


class StateTransitionError(VideoTranslationHouseError):
    """A requested lifecycle transition is not allowed or is blocked."""


class ApprovalError(VideoTranslationHouseError):
    """An approval / release authorization is invalid or cannot be granted."""


class ValidationError(VideoTranslationHouseError):
    """Data failed JSON Schema (or contract) validation."""


class FetchDisabled(SecurityError):
    """Network egress attempted while the sanctioned fetch flag is off."""


class PublishDisabled(SecurityError):
    """External publication attempted while the sanctioned publish flag is off.

    The distribution twin of FetchDisabled: every function in net/publish.py raises
    this before touching a platform API unless VIDTRANS_PUBLISH_ENABLED is truthy.
    """


class EngineUnavailableError(VideoTranslationHouseError):
    """A requested ML engine adapter (ASR/MT/TTS) is not installed or not verified.

    This is a *graceful-degradation* signal, not a bug: the framework is local-first
    and every engine is opt-in, so callers are expected to catch this and defer to a
    human (or to a different provider) rather than crash.
    """


class TranslationError(VideoTranslationHouseError):
    """A translation worksheet is malformed, incomplete, or inconsistent with its source."""


class CaptionError(VideoTranslationHouseError):
    """Caption rendering/validation could not proceed (unknown format, corrupt doc)."""


class DubbingError(VideoTranslationHouseError):
    """A dub could not be produced/imported, or its sync report is inconsistent."""


class MuxError(VideoTranslationHouseError):
    """A dubbed video could not be muxed (missing source/dub, or ffmpeg failed)."""


class PackageError(VideoTranslationHouseError):
    """A deliverable package could not be assembled or its manifest is inconsistent."""


class DistributionError(VideoTranslationHouseError):
    """A distribution/promotion step could not be prepared or executed.

    Covers chapter worksheet round-trips, platform packaging, and promotion queueing —
    the deterministic (no-network) side of Phase 6. Actual egress failures raise
    PublishDisabled (flag off) or surface the platform error text via this type.
    """


class BudgetExceededError(VideoTranslationHouseError):
    """A vendor (billed) engine call was attempted without a sufficient human-set budget.

    Local engines never raise this — only vendor providers (elevenlabs/azure/google TTS,
    or any future billed ASR/MT integration) are spend-guarded.
    """
