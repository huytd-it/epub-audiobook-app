## Why

Khi parse EPUB, nhiều file chứa một "chapter 0" thực chất là trang mục lục (table of contents) - chỉ liệt kê tiêu đề và số trang, không có nội dung kể chuyện. Hiện tại chapter này bị giữ lại trong DB và được xử lý như một chapter thật, gây tốn thời gian TTS và tạo ra audio thừa trong file cuối. Đồng thời, người dùng chưa có cách nào để nhanh chóng xem nội dung text của nhiều chapter cùng lúc để kiểm tra chất lượng parse trước khi chạy TTS.

## What Changes

- **Skip TOC chapter khi parse EPUB**: Tự động phát hiện và loại bỏ chapter đầu tiên nếu nó chỉ chứa danh sách liên kết mục lục (nhiều dòng ngắn, cấu trúc giống index). Áp dụng như một bộ lọc sau khi parse, trước khi lưu vào DB. Log lại số chapter bị skip để người dùng biết.
- **Batch preview chapter trong UI**: Thêm trang/chức năng cho phép chọn nhiều chapter (checkbox hoặc range) và hiển thị text của tất cả cùng lúc, với đánh dấu từng chapter để dễ đọc. Hỗ trợ cả xem preview trước khi TTS (text gốc) và xem nội dung text sau khi đã chunk.
- **API endpoint mới**: `GET /books/{book_id}/chapters/preview?ids=...` trả về danh sách chapter (id, index, title, char_count, text preview) để render trong UI.
- **Nút "Preview" trên book detail page**: Mở giao diện batch preview mặc định chọn tất cả chapter (trừ chapter mục lục đã bị skip).

## Capabilities

### New Capabilities
- `chapter-toc-filter`: Logic phát hiện và bỏ qua chapter mục lục (TOC) trong quá trình parse EPUB.
- `chapter-batch-preview`: Giao diện và API cho phép xem trước nội dung text của nhiều chapter cùng lúc trong UI.

### Modified Capabilities
- (none - chưa có spec nào tồn tại trong `openspec/specs/`)

## Impact

- **Code**: 
  - `app/epub_parser.py`: thêm hàm `_is_toc_chapter()` và áp dụng filter trong `parse_epub()`
  - `app/repository.py`: có thể thêm helper `get_chapters_by_ids()`
  - `app/routes/books.py`: thêm endpoint `/chapters/preview`
  - `app/templates/`: thêm `chapter_preview.html`, cập nhật `book_detail.html` thêm nút/nhúng preview
  - `app/static/style.css`: CSS cho preview layout
- **DB**: không thay đổi schema (chỉ lọc trước khi insert)
- **Backward compat**: Sách đã upload trước đây giữ nguyên (chapters trong DB không bị xóa). Logic skip chỉ áp dụng cho lần upload mới. Có thể bổ sung cờ `skip_existing_books` nếu cần áp dụng lại.
- **Tests**: cần test script mới trong `scripts/` để verify TOC detection trên nhiều EPUB mẫu.
