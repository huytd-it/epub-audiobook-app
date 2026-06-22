## Context

Hiện tại patch list được tạo một lần lúc upload bằng `chunker.group_into_patches(patch_size)` (xem `app/repository.create_book:65`) và cố định suốt đời sách. Worker mỗi lần claim patch sẽ lấy text các chapter trong range qua `repository.get_chapters_in_range(conn, book_id, start, end)`, nối lại, chunk bằng `split_into_tts_chunks`, rồi gọi TTS. Sách cũ không có cơ chế sửa: user phải xóa và upload lại. Bảng `book` có nhiều field nhưng `chapter` và `patch` đều không có cờ nào liên quan đến exclusion hoặc text transformation. Cũng không có bảng nào cho text-replace rules.

Vì thay đổi chạm vào 3 module chính (DB schema, worker synthesis, UI), cần design cẩn thận để tránh mutation ngầm của dữ liệu gốc và tránh race condition giữa user sửa rule và worker đang chạy.

## Goals / Non-Goals

**Goals:**
- Cho phép user đánh dấu chapter exclude và tự định nghĩa patch list.
- Cho phép user quản lý find/replace rules và tự động re-process patch khi rule thay đổi.
- Cung cấp preview text + play audio ngay trong bảng patch.
- Giữ `chapter.text` nguyên bản (rules chỉ áp dụng khi build patch text).

**Non-Goals:**
- Không thêm full-text-search trong chapter (chỉ find/replace đơn giản).
- Không cho phép user tự sửa text chapter.
- Không hỗ trợ rule per-chapter (chỉ per-book).
- Không tự động detect từ viết tắt trong TTS output.

## Decisions

### 1. Replace rules: apply at patch-text-build time, không mutate DB

**Lựa chọn:** Rules được apply khi worker build patch text (và khi endpoint preview text trả response). `chapter.text` trong DB giữ nguyên gốc. Hàm `repository.build_patch_text(conn, patch_id) -> str` join text các chapter trong range, sort theo `chapter_index`, skip chapter `is_excluded=1`, rồi apply rules theo `position` tăng dần (regex hoặc literal).

**Lý do:**
- Tách concerns: DB lưu text gốc (audit), transform là pure function.
- Revert rule = reset patch về pending, không cần restore text.
- Cho phép rule order thay đổi mà không cần ghi lại chapter.

**Alternative considered:** Apply rule lúc upload, mutate `chapter.text`. Bỏ vì phá audit trail, revert phức tạp.

### 2. Schema: cột `is_excluded` + bảng `text_replace_rule`

**Lựa chọn:**
- `chapter`: thêm cột `is_excluded INTEGER NOT NULL DEFAULT 0`. Default 0 = backward-compat.
- Bảng mới `text_replace_rule`: `(id, book_id, find, replace, is_regex, position)` với FK + ON DELETE CASCADE.

**Lý do:**
- Cột boolean đơn giản trên `chapter` cho phép query nhanh (`WHERE is_excluded=0`).
- Bảng riêng cho rules vì rules là per-book, có thể có nhiều, có ordering.
- Migration tự nhiên trong `_migrate()` (xem pattern hiện tại cho `voice_clip_path`).

### 3. Patch rebuild: thay thế patch list

**Lựa chọn:** Khi user POST `/books/{id}/patches/rebuild` với danh sách ranges:
1. Validate (không overlap, không chứa excluded chapter, range hợp lệ).
2. Xóa tất cả patches hiện tại của book.
3. Insert patches mới theo ranges (giữ nguyên thứ tự user submit).
4. Reset book's `final_audio_path` và `status='ready'`.

Audio cũ KHÔNG được reuse (vì chapter_index trong range có thể khác). Worker sẽ re-synth từ đầu.

**Lý do:** Đơn giản nhất, không cần reconcile patch cũ vs mới. User biết rằng submit ranges = bắt đầu lại.

**Alternative considered:** Chỉ update chapter range cho patches có cùng `patch_index`. Bỏ vì user thường thay đổi cả cấu trúc (số patch, range), giữ patch cũ sẽ rất phức tạp.

### 4. Re-process khi sửa rule: reset done patches

**Lựa chọn:** Mỗi khi `add_rule`, `update_rule`, `delete_rule` được gọi:
1. Apply mutation.
2. Set tất cả patch của book có `status='done'` về `status='pending'`, xóa `audio_path`, `error_message`.
3. Clear `book.final_audio_path` và `book.status='processing'`.

**Lý do:** Audio cũ không còn đúng với text mới. Reset cho worker pick up lại tự nhiên (claim_next_pending_patch sẽ lấy).

**Trade-off:** Patch lớn (audio dài) sẽ bị re-synth mỗi lần sửa rule. Giảm thiểu bằng cách cảnh báo user trước khi sửa, hoặc nhóm rule trước khi save (UI có nút "Save all" nếu muốn sau này).

### 5. Preview text inline: dùng `<details>` element

**Lựa chọn:** Mỗi row patch có `<details><summary>Preview text</summary>...</details>` mở rộng khi click. Trong `<details>` là một fetch đến `/patches/{id}/text` qua một lần page load (server-render luôn khi render row). Không cần JS.

**Lý do:** Đơn giản, không cần framework JS, hoạt động ngay cả khi JS disabled. Render lần đầu hơi nặng nếu user có nhiều patch nhưng có thể lazy-load sau nếu cần (qua fetch khi expand).

**Alternative considered:** Modal popup. Bỏ vì cần JS + tăng độ phức tạp không cần thiết.

### 6. Play audio: `<audio controls>` inline

**Lựa chọn:** Patch row có `<audio controls src="/patches/{id}/audio">` nếu status=done. Server endpoint trả file `.wav` qua `FileResponse` (FastAPI xử lý Range requests tự động).

**Lý do:** `<audio controls>` đã support play/pause/seek, không cần code thêm.

### 7. Apply replace rules trong worker: gọi `build_patch_text` thay vì tự concat

**Lựa chọn:** Sửa `app/worker.py` để gọi `repository.build_patch_text(conn, patch)` thay vì `get_chapters_in_range` rồi nối. Đảm bảo synthesis dùng đúng text đã replace.

**Lý do:** Single source of truth cho patch text. Nếu sau này có thêm transformation (uppercase first char, etc.), chỉ cần sửa 1 chỗ.

### 8. Worker safety với rule mutation

**Lựa chọn:** Khi user sửa rule, không cần lock worker. Worker đang xử lý patch với text cũ sẽ finish, ghi `status='done'`, nhưng ngay sau đó nếu user sửa rule, patch đó sẽ bị reset về pending (do apply rule changes).

**Lý do:** Reset tất cả `done` patches trước khi commit rule change là atomic trong transaction SQLite. Patch worker claim pending sẽ chỉ lấy patch chưa done. Race window rất nhỏ (chỉ khi user sửa rule đúng lúc worker vừa done) và audio "stale" sẽ tự bị reset bởi transaction sau.

**Trade-off:** Nếu user sửa rule 5 lần liên tiếp, patch có thể bị re-synth nhiều lần. Acceptable cho use case này.

## Risks / Trade-offs

- **Sửa rule lúc worker đang chạy**: có thể 1 patch vừa done bị reset ngay. → Mitigation: chấp nhận (audio stale sẽ bị regenerate); log warning nếu muốn.
- **Regex DoS** (user nhập regex phức tạp chạy lâu): → Mitigation: giới hạn `find` length (256 chars) và `replace` length (256 chars), timeout không cần (regex Python có cache).
- **Patch text cực lớn** (chapter dài + rules): → Mitigation: không apply cho nội dung > 1MB (skip rule + log warning).
- **Re-synth nặng khi sửa rule**: → Mitigation: confirm dialog trước khi save (UI sau này), giờ cứ reset luôn.
- **Migration trên DB cũ**: cột `is_excluded` thêm vào `chapter` có default 0. Bảng `text_replace_rule` mới với `CREATE TABLE IF NOT EXISTS`. → Không cần backfill.

## Migration Plan

1. Thêm cột `chapter.is_excluded` (default 0) + bảng `text_replace_rule` trong `_migrate()` và schema.
2. Thêm model + repository helpers.
3. Thêm API endpoints + template `patch_builder.html`.
4. Sửa worker để dùng `build_patch_text`.
5. Cập nhật `book_detail.html` (replace rules section + preview/play trong patch table).
6. Rollback: xóa cột/bảng (SQLite không hỗ trợ DROP COLUMN trước 3.35; nếu rollback cần recreate table). Code rollback an toàn vì logic mới là opt-in.

## Open Questions

- Có nên cho user "import" rules từ preset (ví dụ common TTS pronunciation fixes)? → *Defer*.
- Audio cache: khi patch bị reset, xóa file wav hay giữ lại để undo? → *Defer*: xóa luôn cho đơn giản, nếu user cần undo thì submit lại range cũ.
- Drag-and-drop reorder rules? → *Defer*: dùng `position` int cho MVP, có thể thêm drag-drop sau.
