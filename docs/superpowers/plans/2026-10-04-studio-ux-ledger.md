# Studio UX ledger — audit accepted through “tiếp tục” on 04/10/2026

Authority: /Users/juy/Documents/ChatGPT/Video Agent 2/studio-ux-audit-20261003/AUDIT.md and in-chat three-screen design. Existing implementation worktree reused; operational checkout stays live until verified deployment.

- Complete: reopened operational app with exact saved environment; health 200, browser opened.
- Complete: library search/status filtering/pagination + durable batch browser. New test RED on old API then GREEN, including literal wildcard search and >100 jobs.
- Complete: unified selection, contextual toolbar, completed-only confirmed deletion; removed duplicate file table/render path.
- Complete: editor/active panel, native details modal, completed presentation, quiet styling/mobile.
- Complete: browser prompt templates, source association cleared, active-to-terminal notification; simple account cards/advanced operations preserved.
- Complete: explain analysis AI and checkboxes; independent findings on modal navigation, stale provenance, stale preview corrected.
- Complete: 439 regression tests; direct browser and full process-restart journeys passed with external-service mocks. No live generation or live AI requests.
- Complete: backed up SQLite/configuration/source; deployed 047b0a3. Preserved 31 tasks, 3 accounts, 22 media files and configuration. Live read-only desktop/mobile/keyboard/video playback verified with zero console/HTTP errors, zero live generation or AI calls. No active jobs at deployment; active-job restart recovery verified with mocks.

Ruling: browser prompt templates save prompt/configuration/instructions; they do not duplicate reference image storage. Explicitly disclose selecting images separately; preserve existing browser draft/image persistence.
Ruling: keep existing single-failure analysis and batch repair endpoints; unify viewing/selection rather than rewrite generation/provenance flows during a UX change.
Ruling: use installed Chrome for browser journeys because cached Patchright headless runtime changed; no dependency installation.
