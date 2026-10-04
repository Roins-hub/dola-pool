# Remaining approved Studio UX features

Scope: finish accepted 03/10 audit additions after deployment 047b0a3/5a2616d: date filter, video naming, complete batch input confirmation and opt-in notification sound. Keep current runtime/framework/provider and direct Create. No live Dola generation/AI requests.

- Library backend: title-only update preserves every lifecycle column; added date boundaries/search and additive migration. Tests reproduced missing API/column RED, then GREEN.
- UI: local-date inclusive end, title naming/search, configuration/image-aware batch confirmation; date and confirmation tests RED→GREEN.
- Browser caught duplicate hidden title controls. Static drawer form removes duplicate IDs and preserves typed name during polling; regression covers same-job refresh and changing job.
- Independent review: important sound observation stops in Settings; test RED→GREEN for independent opt-in background GET observer. Original title polling/duplicate issue fixed at shared renderer. No critical findings.
- Final: minor (deferred): SQLite Vietnamese non-ASCII search is case-sensitive outside batch filtering; titles/prompts remain searchable with matching letter case. Existing limitation, does not change generation or data.
- Complete: 446 regression tests and direct/full browser journeys passed; real scheduler/store/download/process restart, external services mocked. Sound browser checks mock only Web Audio sink. Migration on current DB copy preserved all columns/32 rows and integrity.
- Complete: deployed 1efcf53 after private backup. Preserved 32 tasks, 3 accounts, 22 media files and existing configuration. Live read-only desktop/mobile/keyboard/retained-video playback passed without console or HTTP errors. No live video generation or AI calls; no active jobs at restart.

Ruling: title is editable task metadata only; do not rename downloaded media or alter prompt/send identity. Cost if wrong: output filenames retain task IDs rather than user title.
Ruling: sound is opt-in for the current page session, defaults off after reload; keep no notification permissions or new dependency. Cost if wrong: user must re-enable after reload; browser background restrictions still apply.
