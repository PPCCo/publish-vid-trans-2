# Podcast / RSS (manual)

Distribute the **audio** dub as a podcast episode via your podcast host's RSS feed. No
auto-poster here.

## How to publish an episode
1. Source audio: `projects/<id>/outputs/<lang>/dub.wav` (or your host's preferred format —
   transcode to MP3/AAC if required).
2. In your podcast host (Transistor, Buzzsprout, self-hosted feed, etc.): **new episode →
   upload the audio**.
3. Episode title = `{title}`; show notes = `{description}` (the chapter list doubles as
   timestamped show notes; many players render them as chapters).
4. Add the target-language transcript from `outputs/<lang>/captions.<lang>.srt` if your host
   supports transcripts.
5. Your host regenerates the RSS feed; Apple/Spotify/etc. pull from it automatically.

## From the checklist
Reuse the platform-package `{title}`/`{description}`; the chapter timecodes are already
formatted as show-note timestamps.
