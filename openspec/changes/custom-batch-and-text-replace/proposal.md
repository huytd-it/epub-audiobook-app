## Why

Hiện tại patches được auto-group theo `patch_size` cố định và user không có cách nào chỉnh sửa. Sách thường có chapter đầu/cuối đặc biệt (lời nói đầu, lời cảm ơn, quảng cáo, chú thích) mà user muốn loại bỏ hoàn toàn, hoặc muốn gộp thành một patch riêng. Đồng thời, TTS engine thường phát âm sai tên riêng, thuật ngữ, hoặc từ viết tắt (ví dụ "AI" đọc thành "a-i", "API" thành "a-p-i") và hiện tại user phải sửa trong file EPUB gốc rồi upload lại. Cuối cùng, sau khi patch xong, user phải mở trang chi tiết patch riêng mới nghe được audio - không có cách nghe nhanh ngay từ bảng tổng quan.

## What Changes

- **Chọn chapter và tạo batch tuỳ chỉnh**: Thêm UI cho phép user đánh dấu từng chapter là "excluded" (không xử lý) hoặc tạo các patch với chapter range tuỳ ý (không bị ràng buộc bởi `patch_size`). Mặc định vẫn suggest auto-group theo `patch_size` để user chỉnh.
- **Find/Replace rules với UI động**: Thêm UI quản lý danh sách cặp (find, replace, is_regex) trên trang book detail. Khi user sửa rule, các patch đã có audio sẽ bị reset về `pending` để re-process với text mới.
- **Preview text + Play audio trong bảng patch**: Mỗi dòng patch trong bảng có 2 nút: "Preview text" (mở modal/section hiện text gộp của các chapter trong patch, dùng batch preview UI có sẵn) và "Play" (phát audio .wav nếu patch đã ở trạng thái `done`; disabled nếu chưa).
- **API endpoints mới**: CRUD cho chapter exclude, patch definition, replace rules; endpoint lấy text của patch (gộp các chapter theo apply find/replace).
- **DB schema mới**: Bảng `text_replace_rule` (book_id, find, replace, is_regex, position) và cột `is_excluded` trên bảng `chapter`. Bảng `patch` giữ nguyên cấu trúc nhưng `chapter_start`/`chapter_end` có thể do user định nghĩa tự do (không bắt buộc tuần tự, không overlap).

## Capabilities

### New Capabilities
- `chapter-selection`: Cho phép user đánh dấu chapter exclude và tự định nghĩa patch (chapter range) thay vì auto-group.
- `text-replace-rules`: Quản lý danh sách find/replace rules per-book với UI động; tự động re-process patch khi rule thay đổi.
- `patch-preview-actions`: Nút "Preview text" và "Play audio" inline trong bảng patch trên book detail page.

### Modified Capabilities
- (none - chưa có spec nào tồn tại trong `openspec/specs/`)

## Impact

- **DB schema**:
  - Thêm bảng `text_replace_rule`: `(id, book_id, find TEXT, replace TEXT, is_regex INTEGER, position INTEGER)` với FK tới `book` ON DELETE CASCADE.
  - Thêm cột `chapter.is_excluded INTEGER NOT NULL DEFAULT 0` (migration trong `_migrate()`).
- **Code**:
  - `app/db.py`: thêm schema cho bảng mới + migration cho `chapter.is_excluded`.
  - `app/models.py`: thêm dataclass `TextReplaceRule`.
  - `app/repository.py`: thêm helpers cho chapter exclude, replace rule CRUD, patch definition rebuild, `get_patch_text()` (gộp text các chapter + apply replace rules).
  - `app/routes/books.py`: thêm CRUD endpoints cho exclude/replace, endpoint rebuild patches, endpoint lấy patch text.
  - `app/templates/`: thêm `patch_builder.html` (chapter list với checkbox exclude + form tạo patch range), cập nhật `book_detail.html` (thêm section replace rules, nút preview/play trong bảng patch).
  - `app/static/style.css`: CSS cho modal preview, replace rule list.
  - `app/worker.py`: cần biết về replace rules khi synthesize (apply rules trước khi chunk).
  - `app/tts_engine.py`: không thay đổi (logic replace ở worker).
- **Backward compat**: Sách đã upload giữ nguyên (mặc định `is_excluded=0`, không có replace rule). Cột mới có default nên migration tự nhiên. Patches cũ vẫn hợp lệ với patch definition tự do.
- **Tests**: cần test cho chapter exclude (DB), replace rule CRUD, apply-replace, patch text generation.
