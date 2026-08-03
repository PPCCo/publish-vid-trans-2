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
