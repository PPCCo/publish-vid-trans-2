# TODO

I want to make some enhancements to the framework. The frame should accept a playlist link, and for a playlist the framework should add all the files in the playlist to the index. I also want the index to have a next appropriate command as a property for human convenience, so that this index containing the playlist videos, each individual video will have the next command property which the user can just copy and paste to start the process. This next command will apply to all the phases so that for example at if a file is at QA gate that specific command for approving the gate would be the next command. In case of the QA gate the files to be reviewed should also be in an array of file relative parts that are to be reviewed. If a file ID within the playlist being added is already processed or is being processed, that file along with all its status will move to the playlist.

## zh, fr, es, pt, ru

can the process/workflow be modified so that even when some of the translation (in this case, ar/en/ur) have already been created, I want to add more translations, in this case, zh, fr, es, pt, ru

Modify/Enhance the `.claude/` framework to behave this way:

- Translation and dubbing will be for all selected languages. The worksheet and things other than translation and dubbing would only be for the source language and English.
- At the start of a new video translation present user with a checkbox list (selectable options for `AskUserQuestion`) to select which language they want for translation and another AskUserQuestion for dubbing. This will allow for example, if the user selects `es` for translation but not for dubbing, the user at a later stage can ask `add es dubbing for "yt-YP0FDR7Wc-8"` and Claude will use the already generated translation to generate the dubbed video, after re-downloading the video only in case the video has been deleted (please that the framework allows for that).
- Also have the natural language capture of adding a video translation or dubbing, for example, the user can ask `add es for "yt-YP0FDR7Wc-8"` in which case, Claude can check if translation already exists, etc from the video's manifest.

project add-languages <id> 

Present the rights decision as selectable options for `AskUserQuestion`