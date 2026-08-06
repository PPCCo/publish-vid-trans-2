# TODO

Add one more framework enhancement. The frame should accept a playlist link, and for a playlist the framework should add all the files in the playlist to the index. I also want the index to have a next appropriate command as a property for human convenience, so that this index containing the playlist videos, each individual video will have the next command property which the user can just copy and paste to start the process. This next command will apply to all the phases so that for example at if a file is at QA gate that specific command for approving the gate would be the next command. In case of the QA gate the files to be reviewed should also be in an array of file relative parts that are to be reviewed. If a file ID within the playlist being added is already processed or is being processed, that file along with all its status will move to the playlist.

## zh, fr, es, pt, ru

can the process/workflow be modified so that even when some of the translation (in this case, ar/en/ur) have already been created, I want to add more translations, in this case, zh, fr, es, pt, ru

Modify/Enhance the `.claude/` framework to behave this way:

- Translation and dubbing will be for all selected languages. The worksheet and things other than translation and dubbing would only be for the source language and English.
- At the start of a new video translation present user with a checkbox list (selectable options for `AskUserQuestion`) to select which language they want for translation and another AskUserQuestion for dubbing. This will allow for example, if the user selects `es` for translation but not for dubbing, the user at a later stage can ask `add es dubbing for "yt-YP0FDR7Wc-8"` and Claude will use the already generated translation to generate the dubbed video, after re-downloading the video only in case the video has been deleted (please that the framework allows for that).
- Also have the natural language capture of adding a video translation or dubbing, for example, the user can ask `add es for "yt-YP0FDR7Wc-8"` in which case, Claude can check if translation already exists, etc from the video's manifest.

---

There is a big issue, the source speech is in a male voice, but the en and zh were translated in female voice. This should be a hard restraint, the default should be male voice.

Also, I'd like to add es and ru to default languages, in addition to en/ur/fa/ar. Also add es and ru for `yt-YP0FDR7Wc-8`

---

The video generation (transcribe/translate + dubbing) is eating up too many tokens. Since we have fixed most of the scripts that were generating wrong output, is it possible to have parts of the workflow/process done manually, by handoffs, where you pause the workflow, and tell me to run the scripts that can be run outside of ClaudeCode, whether it's the python scripts or tools like whisper/huggingface. And once I have successfully executed the scripts, I will let the ongoing video-processing claude session, and it can continue from there until the next manual step. So ClaudeCode should be used only for the activities where using ClaudeCode is the only option. Keep the current Claude-based workflow intact (as an alternate route), so either create a new command `/process-manual <PROJECT-ID>` or add a `--manual` flag in existing workflow, but the idea is, both options should be possible, either run the whole process using Claude, or doing some parts manually. Ideally, if possible, I'd like the whole transcribe-translate-dub process be done using only scripts, without using Claude.

---
API Error: 412 Portkey Usage Limits Policy 9551434e-12fd-462e-83d7-ee27f5e86a5d Exceeded for value key metadata._user
---



---

project add-languages <id> 

Present the rights decision as selectable options for `AskUserQuestion`

---

Some corrections in what I asked earlier:

1. Playlist ingest — relax the VIDTRANS_FETCH_ENABLED rule for this.
2. `next_command` doesn't need to be natural language. It can be a specific command, so ! command is acceptable here (I believe that would be more efficient). But in all other cases, AskUserQuestion and natural language is preferred.
3. At QA gates, also a `review_files` array (relative paths of the artifacts to review, alongside the `next_command`) where applicable, remove it if `review_files` doesn't apply to the `next_command` being set.
4. Playlist grouping — the index groups videos under their playlist. If a video being added via a playlist is already processed / in progress, that existing video (with its full status) moves under the playlist rather than being duplicated.

Let me also reconcile with the two-axis translate/dub work already in flight (those Explore agents are still running). Let me ask the clarifying
questions now so the whole design is coherent.