# Odysee / LBRY (manual)

No API auto-poster here. Upload through the Odysee web UI (or the LBRY desktop app).

## How to upload
1. **Upload → select the final video** at `projects/<id>/outputs/<lang>/dubbed-video.mp4`
   (the exact file bound in `platform-package.json`).
2. Title = `{title}`; description = `{description}` (includes the chapter list).
3. Set the channel, thumbnail, tags from `{hashtags}`, and language.
4. Attach the target-language SRT if the uploader supports captions.

## From the checklist
Reuse the platform-package `{title}`/`{description}`.
