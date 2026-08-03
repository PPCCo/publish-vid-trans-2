# PeerTube (manual)

No API auto-poster here by default (PeerTube instances expose a REST API, but endpoints and
tokens are per-instance). Upload through your instance's web UI.

## How to upload
1. **Publish → Upload → select the final video** at
   `projects/<id>/outputs/<lang>/dubbed-video.mp4` (the exact file bound in
   `platform-package.json`).
2. Title = `{title}`; description = `{description}` (includes the chapter list).
3. Set channel, category, language, licence (align with your rights record), and tags from
   `{hashtags}`.
4. Add captions: upload the target-language SRT under the video's caption settings.

## From the checklist
Reuse the platform-package `{title}`/`{description}`.
