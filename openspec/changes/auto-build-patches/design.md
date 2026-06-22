## Context

Hiện tại change `custom-batch-and-text-replace` đã có `rebuild_patches(conn, book_id, ranges, reset_done)` nhận một list `(start, end)` rồi validate và insert. Tuy nhiên use case phổ biến nhất - "build lại từ chapter X đến hết, skip excluded, chunk đều" - buộc user phải tự tính toán list ranges rồi nhập vào form, rất thủ công và dễ sai (đặc biệt khi số chapter lớn). Cần một auto-build mode mà user chỉ cần chỉ định start, mọi thứ còn lại tự động.

DB schema hiện đã có `chapter.is_excluded` (default 0) từ change trước, nên việc skip-excluded là query thuần. Patch range `(chapter_start, chapter_end)` cho phép range không liên tục (chỉ cần không overlap), nên có thể tạo patch có range rộng mà chapter bị exclude vẫn nằm trong khoảng - `build_patch_text` sẽ skip khi generate text.

## Goals / Non-Goals

**Goals:**
- Auto-generate patch list từ start chapter (required) đến end chapter (optional, default = max) với chunk size có thể config.
- Tự skip excluded chapters khi group chunks.
- Validate inputs với error messages rõ ràng.
- UI form đơn giản trên book detail page (3 input + 1 button).

**Non-Goals:**
- Không thay đổi schema DB.
- Không hỗ trợ negative range hoặc reverse order.
- Không thêm preset (ví dụ "build from chapter 1" hay "build from first non-excluded").
- Không thay đổi auto-group lúc upload (vẫn dùng `group_into_patches(len(chapters), patch_size)`).

## Decisions

### 1. Algorithm: group included chapter indices thành chunks

**Lựa chọn:** Trong `repository.auto_build_patches`:
1. Lấy tất cả chapter_index của book, sort ascending.
2. Lọc: `start <= chapter_index <= end` (nếu `end` là None thì bỏ check trên).
3. Lọc tiếp: `is_excluded = 0`.
4. Group vào chunks kích thước `patch_size` theo thứ tự.
5. Mỗi chunk → tuple `(first_idx, last_idx)`.
6. Gọi `rebuild_patches(conn, book_id, ranges, reset_done=True)`.

**Lý do:**
- Đơn giản, dễ test, tận dụng validation có sẵn của `rebuild_patches` (no overlap, no excluded).
- Các chapter trong cùng 1 chunk luôn liên tục trong "included list" - nên range chunk là contiguous (vd included=[0,1,2,4,5,6,8,9], patch_size=3 → chunks [0,1,2]→range(0,2), [4,5,6]→range(4,6), [8,9]→range(8,9)).

**Trade-off:** Chapter 3 và 7 bị exclude sẽ không xuất hiện trong patch ranges. `build_patch_text` sẽ generate text từ `chapter_start` đến `chapter_end` rồi filter excluded (xem `repository.build_patch_text`) - an toàn.

### 2. Defaults lấy từ book state

**Lựa chọn:** Đọc `book.patch_size` để default `patch_size`; query `MAX(chapter_index)` để default `end_chapter`.

**Lý do:** Tận dụng dữ liệu đã có, user không phải nhập lại. Khi user upload với `patch_size=5` thì auto-build cũng dùng 5.

**Alternative considered:** Hardcode default `patch_size=10`. Bỏ vì không khớp với `book.patch_size` đã user chọn lúc upload.

### 3. UI: form inline trên book_detail.html

**Lựa chọn:** Thêm section "Auto-build patches" giữa section "Patch builder" link và bảng patches. Form có 3 input + 1 submit button. Sau submit thành công, redirect về book detail (303). Khi fail (400), hiển thị error message inline.

**Lý do:** Đặt gần bảng patches nên user dễ thấy kết quả ngay. Không cần trang riêng (form quá nhỏ, không cần JS).

**Alternative considered:** Nhét vào `patch_builder.html`. Bỏ vì trang đó đã phức tạp, form auto-build thuộc use case khác (nhanh) nên cần prominent.

### 4. Validation: fail-fast với message rõ ràng

**Lựa chọn:** Validation trong `auto_build_patches` (raise `ValueError` với message cụ thể), endpoint catch và return 400. UI hiển thị error ở đầu trang.

**Lý do:** Single source of truth cho validation logic. Cả API và UI đều gặp cùng error message.

**Trade-off:** Phải catch exception ở route - acceptable, FastAPI pattern phổ biến.

### 5. Reset done patches: dùng `reset_done=True`

**Lựa chọn:** Auto-build luôn gọi `rebuild_patches(..., reset_done=True)` để xóa wav files cũ và reset patches done về pending. Book's `final_audio_path` cũng bị clear.

**Lý do:** Patch definition thay đổi → audio cũ không còn khớp → re-synth là đúng.

**Trade-off:** Mất audio cũ (đã được synthesize). User chấp nhận vì họ chủ động bấm auto-build.

## Risks / Trade-offs

- **User build nhầm range** → mất audio cũ. Mitigation: confirm dialog trước khi submit (UI tương lai, hiện tại không cần vì reset_done=True đã documented).
- **Patch lớn (>50 chapters mỗi patch)** nếu `patch_size` lớn → TTS synthesis có thể chậm/lỗi. Mitigation: default `patch_size` lấy từ `book.patch_size` thường đã nhỏ (5-15).
- **start > end (nếu user nhập cả 2)** → return 400 rõ ràng, không crash.
- **All excluded** → return 400 với message rõ ràng, không insert empty patch list.

## Migration Plan

1. Thêm `auto_build_patches` trong `repository.py` (function mới, dùng lại `rebuild_patches`).
2. Thêm route `POST /books/{book_id}/patches/auto-build` trong `routes/books.py`.
3. Thêm form HTML trong `book_detail.html`.
4. Không cần migration DB. Rollback: xóa function + route + form HTML.

## Open Questions

- Có nên show preview "X patches sẽ được tạo" trước khi submit? → *Defer*: user có thể xem trong bảng patches ngay sau submit, không cần preview trước.
- Có nên support `direction=descending`? → *Defer*: hiện tại chỉ ascending (natural reading order).
- Có nên cho phép exclude thêm chapters từ form auto-build? → *Defer*: dùng form exclude riêng trong `patch_builder.html`.
