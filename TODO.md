# TODO

Add one more framework enhancement. The frame should accept a playlist link, and for a playlist the framework should add all the files in the playlist to the index. I also want the index to have a next appropriate command as a property for human convenience, so that this index containing the playlist videos, each individual video will have the next command property which the user can just copy and paste to start the process. This next command will apply to all the phases so that for example at if a file is at QA gate that specific command for approving the gate would be the next command. In case of the QA gate the files to be reviewed should also be in an array of file relative parts that are to be reviewed. If a file ID within the playlist being added is already processed or is being processed, that file along with all its status will move to the playlist.

## zh, fr, es, pt, ru

can the process/workflow be modified so that even when some of the translation (in this case, ar/en/ur) have already been created, I want to add more translations, in this case, zh, fr, es, pt, ru

Modify/Enhance the `.claude/` framework to behave this way:

- Translation and dubbing will be for all selected languages. The worksheet and things other than translation and dubbing would only be for the source language and English.
- At the start of a new video translation present user with a checkbox list (selectable options for `AskUserQuestion`) to select which language they want for translation and another AskUserQuestion for dubbing. This will allow for example, if the user selects `es` for translation but not for dubbing, the user at a later stage can ask `add es dubbing for "yt-YP0FDR7Wc-8"` and Claude will use the already generated translation to generate the dubbed video, after re-downloading the video only in case the video has been deleted (please that the framework allows for that).
- Also have the natural language capture of adding a video translation or dubbing, for example, the user can ask `add es for "yt-YP0FDR7Wc-8"` in which case, Claude can check if translation already exists, etc from the video's manifest.

---

I want to pitch this Claude framework to the teams in my organisation, all the strengths and features it provides, that will not only help with some activities but throughout they suffer life cycle from integration with confluence Jara and two, how it keeps everything in sync, and the powerful quality gates that prevent either bad code or wrong specifications to go in to the comments, the powerful husky hooks unit test integration test that make sure the code is well tested, the natural language inquiries about the general task, confluence, the powerful command commands that cover all the different activities involved in a project. The purpose of the presentation is To highlight all the strengths and features of the framework, how it stands out from what is available and no other framework compete with the full feature said set, how it has all the standards and patterns baked in to ensure the documentation generated the specification generated, and the code implemented is of the best quality. The goal is to sell it to the team with evidence after features available. How different YouTube functions like the commit command help the developers to commit with his while ensuring on the quality checks. How the specific case is generated are very detailed and professional, how it works like a team normally works breaking down those functional specifications into EX and breaking down EX into task with every little little detail including acceptance criteria technical specifications data design testing requirements in every activity activity that and enter prize application needs to deliver a high-quality product. Also highlight the commands that would allow projects to repurposed the framework according to their tech technologies stock, or according to the code they already have existing, and the command that allows bringing in features from other frameworks to enhance it further.

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