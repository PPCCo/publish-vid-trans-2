"""yt-app — a standalone, non-Claude A/V transformation toolkit.

This package reuses the publish-vid-trans framework's proven *leaf* functions
(ffmpeg/ffprobe wrappers, ASR/TTS/MT subprocess adapters, caption/size/speed/network
helpers) WITHOUT touching the project state machine, gates, approvals, or event log.
See ``docs/non-claude-user-guide.md`` at the repo root.

Import-order rule: ``lib.env.bootstrap()`` MUST run before any
``video_translation_house`` import. Every module here does its framework imports
*lazily inside functions* so import order can't be violated by whichever module
``cli.py`` loads first.
"""
