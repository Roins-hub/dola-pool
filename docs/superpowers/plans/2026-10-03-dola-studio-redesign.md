# Dola Studio Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cải tiến Studio thành luồng video tiếng Việt có phân tích AI từng đoạn, batch/TXT/folder, tiến độ Dola và quản lý vận hành thực tế.

**Architecture:** Giữ FastAPI/SQLite và worker hiện có. Sửa lớp message dùng chung, mở rộng contract preflight và context gate, rồi nối UI và các tính năng vận hành vào persistence hiện tại. Không thay worker bằng stub thành công trong browser test.

**Tech Stack:** Python, FastAPI/Pydantic, SQLite, httpx, HTML/CSS/JavaScript hiện có, pytest, Patchright, ffmpeg, stdlib zipfile.

**Spec:** `docs/superpowers/specs/2026-10-03-dola-studio-redesign-design.md` (bản được duyệt, commit f715ee5).

## Global Constraints

- Giữ framework, database, scheduler, worker, kết nối AI, tài khoản, video và cấu hình hiện có; không thêm provider/queue.
- Excel không thuộc phạm vi. Không mua tài khoản hoặc tạo video live dùng điểm trong kiểm thử mặc định.
- Một prompt 1–10 bản; tối đa 50 video/mẻ; TXT UTF-8 tối đa 1 MB; giữ dòng trùng.
- Theo dõi tự động 30 phút; ambiguous submit không tự retry; phục hồi dùng account/conversation cũ.
- UI refresh 3 giây, Pure API poll mặc định 10 giây; không fake render percent, model, chi phí hoặc native 1080p.
- Nguyên văn lỗi chỉ trong chi tiết kỹ thuật đã che secrets; thông báo chính bằng tiếng Việt.
- Migration chỉ bổ sung; sao lưu DB/config và thử migration trên bản sao trước triển khai.
- Worktree đang có thay đổi Hermes và đợt trước: không reset, không git add toàn repo. Mỗi commit chỉ chứa thay đổi của task, dùng staging chọn phần khi file có thay đổi cũ.

## Review Focus

- Prompt có emoji, dấu tổ hợp, đoạn lặp/chồng lấn: không thay nhầm đoạn (Task 3/4).
- Mất phản hồi POST rồi token hết hạn: trả biên nhận cũ, không gọi AI/gửi lại (Task 3/8).
- Ảnh cùng tên nhưng khác byte hoặc URL đổi nội dung: context kiểm tra phải sai lệch (Task 3).
- Tắt kho khi video đã được nhận: video vẫn hoàn tất bằng account cũ (Task 5).
- Tải ZIP khi một job vừa bị xóa hoặc tên mẻ chứa ../: không đọc ngoài media root, không crash cả server (Task 7).

## File map và ranh giới

- `failure_text.py`: message/category/action và sanitization cho task/account/progress.
- `ai_provider.py`: lời gọi AI và parser rich response; `ai_preflight.py`: cache/token/context gate.
- Thêm `prompt_rules.py`: dữ liệu rule có nguồn/phiên bản và chọn đúng scope; không runtime fetch GitHub.
- `server.py`: API authenticated, task serializer, batch manifest, ZIP và cấu hình đầu ra.
- `store.py`: task/batch persistence; `browser_pool.py`: account metadata và chọn kho; `config.py`: môi trường test/output.
- `web/index.html`: tiếp tục pattern UI hiện tại, không chuyển React/framework.
- `tests/conftest.py`: cô lập trước import; `tests/studio_mock_server.py` và `tests/run_studio_journey.py`: browser/process journey tại ranh giới ngoài.
- Test mới tập trung theo contract, không tạo framework test mới.

Mỗi task là deliverable kiểm tra độc lập; có thể dừng triển khai sau task đã qua test, nhưng goal tổng thể chỉ hoàn tất sau Task 8. Các phần tạo ảnh/asset/âm thanh/marketplace/provider proxy mới chưa có tích hợp trong spec không được dựng control giả.

### Task 1: Cô lập test và thông điệp tiếng Việt dùng chung

**Files:** Modify `tests/conftest.py`, `failure_text.py`, `server.py`, `browser_pool.py`, `pure_api_gen.py`; Test `tests/test_failure_text.py`, `tests/test_studio_lifecycle.py`, Create `tests/test_studio_messages.py`.

**Interfaces:** Produces `failure_text.present(text: str, *, code: str = '', category: str = '') -> dict` với category/message_vi/next_action_vi/upstream_code/details. `failure_text.sanitize_details(text: str) -> str` dùng lại sanitizer hiện có. API thêm `failure_display` và `upstream_display` bên cạnh dữ liệu cũ; business category mới dùng mã ổn định.

- [ ] Viết test `test_operational_paths_are_isolated_before_import`: DB/profile/media/AI/credentials đều ngoài repo và operational paths trước import app; không mở key thật. Profile hiện lấy từ `Path("accounts")` tương đối cwd, không có env profile riêng: assert cwd đã đổi sang thư mục temp trước import, `BrowserPool.accounts_dir.resolve()` ở temp và không symlink accounts thật. Giữ cơ chế cwd cô lập hiện có, không thêm config profile chỉ để test.
- [ ] Viết test `test_copyright_refusal_has_vietnamese_message` với đầu vào `涉嫌侵权`; assert category là copyright, message_vi chứa Bản quyền, details giữ nguyên văn, không có CJK trong message/action. Thêm `test_unknown_error_does_not_invent_cause`, `test_legacy_note_preserves_user_note`, `test_progress_details_redact_secrets`.
- [ ] Run `.venv/bin/python -m pytest tests/test_studio_messages.py -q`; phải FAIL vì interface chưa có, không do import live.
- [ ] Implement present/sanitization bằng mapping mã và rule hiện có, giữ helper cũ tương thích caller. Nối serializer task/list/batch/account/progress; confirmed failure lưu code/group từ exception. Unknown giữ unknown, không ghi account lỗi chỉ do polling.
- [ ] Run `.venv/bin/python -m pytest tests/test_failure_text.py tests/test_studio_messages.py tests/test_studio_lifecycle.py -q`; PASS. Điều chỉnh test cũ chỉ khi assertion ngôn ngữ cũ đã không còn là contract, giữ kiểm tra loại lỗi/không mất raw.
- [ ] Commit riêng task `fix: present Dola failures and progress in Vietnamese`.

### Task 2: Rule có nguồn và contract AI phân tích chi tiết

**Files:** Create `prompt_rules.py`, `tests/test_prompt_analysis.py`; Modify `ai_provider.py`, `ai_preflight.py`, `server.py`, `tests/test_ai_prompt.py`, `tests/test_ai_preflight.py`.

**Interfaces:** `prompt_rules.RULES_VERSION: str`; `rules_for_context(context: dict) -> list[dict]`. Mở rộng `preflight_check_prompt(prompt, override_config=None, client=None, *, context=None, upstream_error=None) -> dict` giữ positional cũ. Kết quả status/reason/suggestions + quality_summary/issues/rules_version/context_fingerprint. Issue: id/kind/severity/quote/occurrence/replacement/reason_vi/confidence/source_ids; confidence high/medium/low, kind content/capability/quality. Nhận xét toàn prompt quote rỗng, không cho thay thế trực tiếp.

- [ ] Viết `test_rich_response_survives_provider_cache_api`: MockTransport trả issues, summary, confidence; assert mọi trường qua provider/record/API và source_ids chỉ nhận id có thật. `test_quality_warning_does_not_block_safe_prompt`, `test_invalid_schema_never_issues_token`, `test_surface_rule_does_not_claim_dola_api_limits`.
- [ ] Run `.venv/bin/python -m pytest tests/test_prompt_analysis.py -q`; FAIL vì fields bị drop.
- [ ] Implement schema validation tối đa 20 issues, chuỗi quote/replacement/reason tối đa 4000 ký tự mỗi trường; chỉ status hợp lệ cấp token. Lưu trường rich trong cache/response. Rule chọn theo scope, nguồn và mức bằng chứng; không copy toàn repo/skill ngoài. Rewrite giữ ngôn ngữ/scene structure, bỏ cấm timeline tuyệt đối, instructions vẫn tối đa 4000 ký tự.
- [ ] Run `.venv/bin/python -m pytest tests/test_prompt_analysis.py tests/test_ai_prompt.py tests/test_ai_preflight.py -q`; PASS. Corpus semantic dùng prompt đơn giản, nhiều cảnh/thiếu ảnh, tên nhân vật, storyboard; ghi input/output đánh giá riêng, mock không chứng minh ngữ nghĩa.
- [ ] Commit riêng task `feat: return grounded prompt analysis and editing suggestions`.

### Task 3: Context gate và neo sửa an toàn

**Files:** Modify `ai_preflight.py`, `ai_provider.py`, `server.py`; Test `tests/test_prompt_analysis.py`, Create `tests/test_preflight_context.py`.

**Interfaces:** `context_fingerprint(context: dict) -> str`, `validate_issue_anchors(prompt: str, issues: list[dict]) -> list[dict]` trong ai_preflight; issue thêm applicable và anchor_error. `record_preflight_result(..., *, context=None)` và `validate_preflight_gate(..., *, context=None, rules_version=None)` giữ signature cũ. Context chuẩn hóa model thực/duration/ratio và references [{sha256,role}], thứ tự có ý nghĩa. Preflight multipart tùy chọn bổ sung cùng endpoint cho ảnh thật; JSON cũ vẫn nhận prompt/context metadata nhưng không chứng nhận pixel.

- [ ] Viết `test_context_change_invalidates_token` đổi model/duration/ratio/ảnh/rules từng trường; `test_same_filename_different_bytes_invalidates`, `test_changed_remote_image_invalidates`, `test_repeated_unicode_quote_requires_occurrence`, `test_overlapping_edits_not_applied_together`.
- [ ] Viết `test_existing_receipt_skips_expired_gate_and_ai`: POST lặp sau token expiry trả cùng task/batch và AI/submission count không tăng. `test_text_only_token_gets_real_context_check` chứng minh allow/review/block và lỗi AI đều đúng, không nhận task trước khi gate xong.
- [ ] Run `.venv/bin/python -m pytest tests/test_preflight_context.py -q`; FAIL.
- [ ] Implement hash byte thật từ validation/materialization hiện có; không tin digest client. Neo trên nguyên văn, occurrence xác minh, không lấy offset Python làm offset JS. Context-token mismatch từ chối trước task; legacy token hợp lệ bổ sung AI với context, review trả token mới và yêu cầu xác nhận. Biên nhận tồn tại được xét trước gate/AI. Không nới SSRF validation URL.
- [ ] Run `.venv/bin/python -m pytest tests/test_preflight_context.py tests/test_studio_batch_progress.py tests/test_phase4_edge_cases.py -q`; PASS, duplicate submission count giữ 1.
- [ ] Commit riêng task `fix: bind prompt checks to video context and exact edits`.

### Task 4: Workspace, AI editor và batch UX

**Files:** Modify `web/index.html`, `tests/studio_mock_server.py`, `tests/run_studio_journey.py`, `STUDIO.md`.

**Interfaces:** JS `renderPromptAnalysis(result, snapshot)`, `applySelectedSuggestions()`, `analysisSnapshot()`; snapshot gồm prompt nguyên văn/editor revision và context. Dùng present payload Task 1 và rich fields Task 2/3. Không đưa raw upstream text vào dòng chính.

- [ ] Thêm journey assertions: issue quote/replacement hiển thị, lựa chọn từng sửa, compare/undo; emoji/đoạn lặp đúng; đổi prompt/ảnh/params trong request bỏ response cũ; AI lỗi giữ draft. Batch duplicate prompt không bị dedupe, 2 prompt × 2 bản = 4 task, confirm tổng và gate đúng.
- [ ] Run `.venv/bin/python tests/run_studio_journey.py /tmp/dola-redesign-task4`; FAIL tại các selector/behavior mới, giữ real scheduler/worker.
- [ ] Implement bố cục ảnh 1/5: creator tham số gọn; prompt/AI trái, reference phải; queue Bảng/Lưới phía dưới và preview selection riêng. Native controls, CSS responsive, focus/labels, error sát input. Three nav hiện có; draft/history/images tiếp tục storage hiện có.
- [ ] Nối analysis/suggestion chọn áp dụng/undo, context invalidation, confirmed refusal → draft sửa có liên kết task cũ, không auto gửi. Hiển thị progress steps, timestamp, message_vi, technical details mở rộng; phân biệt backend/Dola health. Giữ stale polling guards và player khi chỉ progress đổi.
- [ ] Run journey desktop/mobile/keyboard, 3 giây refresh và backend poll bằng mock nhận updates; kiểm tra không tràn page ngang và screenshot đối chiếu ảnh 1/5. PASS chỉ khi assertions thực chạy, không từ ảnh đơn.
- [ ] Commit riêng task `feat: build focused Studio workspace with selectable AI edits`.

### Task 5: Kho tài khoản và onboarding có tiến độ

**Files:** Modify `browser_pool.py`, `server.py`, `web/index.html`; Create `tests/test_account_inventory.py`; Modify `tests/test_cookie_batch_import.py`, `tests/test_groups.py`, `tests/studio_mock_server.py`.

**Interfaces:** Pool DB hiện có thêm `account_pools(id TEXT PRIMARY KEY,name TEXT,enabled INTEGER)` và accounts_meta.pool_id mặc định default; không dùng quota group làm pool. Methods `list_inventory_pools() -> list[dict]`, `set_inventory_pool(pool_id: str, *, name=None, enabled=None) -> dict`, `assign_inventory_pool(names: list[str], pool_id: str) -> None`. Auth API GET/POST/PATCH `/api/admin/account-pools`, PATCH account nhận pool_id. Scheduler filter pool chỉ cho job chưa gửi.

- [ ] Viết `test_legacy_accounts_use_enabled_default_pool`, `test_disabled_pool_never_receives_new_job`, `test_disabling_pool_does_not_stop_existing_conversation`, `test_import_preview_dedupes_identity_and_reports_rows`. Bulk Facebook fixture xác nhận đúng entry flow, không dùng credentials thật.
- [ ] Run `.venv/bin/python -m pytest tests/test_account_inventory.py -q`; FAIL.
- [ ] Implement migration bổ sung trong init DB pool hiện có, preserve notes/quota/session. Apply filter ở chung candidate selection cho browser/Pure API; resume account/conversation bỏ qua filter new-job. UI tên kho/bật tắt, waiting reason. Preview bulk input dùng parser hiện có; tiến độ qua job polling hiện có, không gửi Hello khi chỉ xem.
- [ ] Run `.venv/bin/python -m pytest tests/test_account_inventory.py tests/test_groups.py tests/test_cookie_batch_import.py tests/test_restart_recovery.py -q`; PASS; journey thử kho disable trong lúc poll.
- [ ] Commit riêng task `feat: select account inventory pools without interrupting running jobs`.

### Task 6: Proxy, nhật ký và chẩn đoán thực

**Files:** Modify `server.py`, `browser_pool.py`, `proxy_store.py`, `web/index.html`; Create `tests/test_studio_diagnostics.py`.

**Interfaces:** GET authenticated `/api/admin/diagnostics/events?task_id=&account=&level=&limit=` trả events [{time,kind,task_id,account,message_vi,details}], tối đa 200; dùng record task/account và logging hiện có, bounded buffer nếu thiếu persistence log, không dựng log platform. Tuyến hiện tại dùng `/api/admin/route` và proxy probe hiện có.

- [ ] Viết `test_proxy_failure_never_falls_back_direct`, `test_route_reports_relay_without_claiming_account_proxy`, `test_diagnostic_api_redacts_credentials_before_serialization`, `test_diagnostic_filter_does_not_return_unrelated_task`. Cả masked-password URL, cookie/header và session trong exception đều không xuất API.
- [ ] Run `.venv/bin/python -m pytest tests/test_studio_diagnostics.py -q`; FAIL.
- [ ] Implement dùng sanitizer Task 1 trước event store/response; UI nâng cao có probe rõ timeout/kết quả và filters/log details. Không thêm rotating proxy mode chưa có provider. Buffer nếu dùng phải ghi chú ponytail về giới hạn không giữ qua restart, task history vẫn persisted.
- [ ] Run `.venv/bin/python -m pytest tests/test_studio_diagnostics.py tests/test_dynamic_proxy.py -q`; PASS; browser failure message đúng vị trí.
- [ ] Commit riêng task `feat: expose accurate routing and sanitized diagnostics`.

### Task 7: Nhập folder và xuất kết quả

**Files:** Modify `server.py`, `store.py`, `config.py`, `web/index.html`, `STUDIO.md`; Create `tests/test_folder_batch_output.py`; Modify `tests/run_studio_journey.py`.

**Interfaces:** BatchPrompt thêm reference_indices tùy chọn; multipart batch JSON giữ schema cũ, thêm per-prompt index vào images uploads. Omitted dùng ảnh chung cũ. Fingerprint receipt gồm thứ tự/group/byte ảnh của từng prompt. GET authenticated `/api/admin/videos/batches/{id}/download` trả ZIP file, job/task ids lấy từ DB; không nhận path từ query. Thiết lập output admin bổ sung export_directory; đường dẫn local host được validate, file gốc DB không đổi.

- [ ] Viết `test_folder_groups_one_txt_with_own_images`, `test_ambiguous_group_rejected_before_submit`, `test_batch_reference_inputs_owned_per_job`, `test_zip_names_cannot_escape_root`, `test_zip_missing_deleted_video_reports_skipped`, `test_export_path_rejected_if_unwritable`. Root 1 group, subfolders, TXT multiline nguyên file, thứ tự nhóm ổn định; unsupported types báo preview.
- [ ] Run `.venv/bin/python -m pytest tests/test_folder_batch_output.py -q`; FAIL.
- [ ] Implement directory picker browser và preview nhóm, validate tổng 50/copies10/TXT1MB và ảnh như hiện có; không gửi file chưa duyệt. Giữ receipt transaction trước workers. ZIP dùng zipfile ghi temporary file, cleanup sau response, không gom video RAM; tên file safe chứa ordinal/task id, không symlink escape. Bản sao xuất giữ source media và DB.
- [ ] Implement Mở thư mục chỉ local host có xác thực, mở đường cấu hình chứ không thực thi chuỗi shell; tunnel chỉ tải. UI xử lý unsupported picker bằng chọn TXT/ảnh rõ ràng, không mất draft.
- [ ] Run `.venv/bin/python -m pytest tests/test_folder_batch_output.py tests/test_reference_materialize.py tests/test_studio_batch_progress.py -q`; PASS; journey tạo 2 folder, mỗi group ảnh riêng, restart không trùng, download ZIP playable.
- [ ] Commit riêng task `feat: import prompt folders and export batch results`.

### Task 8: Regression, đánh giá AI, migration và triển khai

**Files:** Modify `tests/run_studio_journey.py`, `tests/studio_mock_server.py`, `STUDIO.md`; evidence ngoài repo dưới workspace Video Agent 2. Cleanup chỉ script đã xác minh không được import/call.

**Interfaces:** Evidence JSON có test counts, command exits, mock boundary, submit counts, old/new process pid, migration counts, screenshots và source hashes; separate mock/live labels. Không ghi secrets.

- [ ] Mở rộng journey assertions video sau 300 giây bằng clock/protocol fixture; polling lỗi rồi hồi phục; 30 phút và check-result; restart process thật; ambiguous submit; short clip và confirmed one-child; double-click/change selected job; rich AI schema và UX. Scheduler/persistence thật, không mock toàn worker.
- [ ] Run `.venv/bin/python -m pytest -q`; PASS toàn suite, điều tra failure theo systematic-debugging, không bỏ test để xanh. Run `.venv/bin/python tests/run_studio_journey.py /tmp/dola-redesign-final`; PASS tất cả invariants và desktop/mobile.
- [ ] Đánh giá semantic corpus qua kết nối AI đã có bằng prompt không tạo Dola video; ghi 6–10 mẫu input/output, nguồn rule, exact quote validity và người dùng kiểm tra được. Tách semantic evaluation khỏi mocked-contract pass; không hứa provider acceptance.
- [ ] Sao lưu SQLite bằng backup API và cấu hình/credentials với quyền 0700; không print nội dung. Migrate bản sao DB task/pool, assert task/account/video/receipt counts giữ, default pool và fields mới đúng; record diff schema.
- [ ] Revalidate process/listener thực tế và pending conversations; triển khai source đã verified với env giữ nguyên. Stop process cũ có giới hạn và xác nhận dừng, launch process mới, quan sát recovery job cũ, tuyệt đối không tạo live request mới. Nếu failure rollback bằng source/env đã lưu, giữ DB bổ sung tương thích, không restore snapshot đè task mới.
- [ ] Audit 10 tiêu chí spec bằng evidence row-by-row; cleanup script một lần chỉ khi rg caller chứng minh không dùng; chạy lại checks liên quan cleanup. Ghi STUDIO.md giới hạn/modes/mock/live và những tính năng riêng chưa tích hợp.
- [ ] Commit riêng task `test: verify Studio redesign and recovery end to end`. Chỉ báo hoàn tất khi evidence chứng minh toàn phạm vi được duyệt, không dùng số test xanh làm bằng chứng semantic/UI.

## Self-review và handoff

Coverage: spec 1/2 → constraints/all tasks; 3 → Task 1/6; 4 → Task 2; 5 → Task 3/4; 6 → Task 4/8; 7 → Task 5/6/7; 8 → Task 8; 9 → tests ở Task 1–8; 10 → thứ tự tasks. Các Review Focus đã có test mang tên trong task sở hữu. Interfaces rich fields/context/payload cùng tên xuyên các task. Không cần dependency mới hoặc rewrite framework.

Khuyến nghị Native: các task dùng chung server.py và web/index.html, một implementer giữ mạch contract/context/lifecycle giảm xung đột. Review độc lập toàn nhánh sau cùng theo skill; không áp dụng review model override nếu chưa có cơ sở được phép. Người dùng duyệt kế hoạch và chọn phương thức trước khi viết code.
