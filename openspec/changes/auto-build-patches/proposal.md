## Why

Change `custom-batch-and-text-replace` đã có sẵn cơ chế `rebuild_patches` cho phép user tự định nghĩa ranges tuỳ ý, nhưng form hiện tại bắt buộc nhập cả `start` và `end` cho từng range - khá tốn công cho use case phổ biến nhất: "build lại từ chapter X đến hết, với kích thước patch mặc định, tự động bỏ qua những chapter đã exclude". User cần một nút "auto-build" đơn giản chỉ yêu cầu 1 input (start), patch list được generate tự động với logic skip-excluded tích hợp sẵn.

## What Changes

- **Auto-build endpoint**: Thêm `POST /books/{book_id}/patches/auto-build` nhận `start_chapter` (required), `end_chapter` (optional, mặc định = max chapter của book), `patch_size` (optional, mặc định = book.patch_size). Trả về JSON danh sách patch mới.
- **Auto-chunk algorithm**: Repository function `auto_build_patches(conn, book_id, start_chapter, end_chapter, patch_size)` lấy các chapter KHÔNG bị exclude trong khoảng [start, end], group thành các chunk liên tiếp kích thước `patch_size`, mỗi chunk tạo thành 1 patch với `chapter_start` = chapter đầu tiên, `chapter_end` = chapter cuối cùng của chunk (có thể không liên tục nếu giữa chunk có chapter bị exclude - thực tế là liên tục vì skip excluded).
- **Reset done patches**: Auto-build reset tất cả `done` patches về `pending` (giống `rebuild_patches` với `reset_done=True`).
- **UI form**: Thêm 1 form nhỏ "Auto-build patches" trên book_detail.html với 3 input: start_chapter, end_chapter (optional, placeholder="to end"), patch_size (default=10, sẵn từ book.patch_size). Submit gọi endpoint, redirect về book detail.
- **Validation**: start_chapter phải >= 0, end_chapter (nếu có) phải >= start, patch_size phải >= 1. Trả 400 với message rõ ràng khi invalid.
- **Edge cases**: 
  - Toàn bộ khoảng [start, end] bị exclude → 400 "no included chapters in range"
  - start > max chapter → 400 "start_chapter out of bounds"
  - end < start → 400 "end must be >= start"

## Capabilities

### New Capabilities
- `auto-build-patches`: Auto-generate patch list từ start chapter đến end chapter (hoặc đến hết), chunk theo patch_size, tự skip excluded chapters.

### Modified Capabilities
- (none - không thay đổi requirement của capability đã có)

## Impact

- **Code**:
  - `app/repository.py`: thêm `auto_build_patches(conn, book_id, start_chapter, end_chapter, patch_size) -> list[Patch]`. Có thể tái sử dụng `rebuild_patches` ở cuối sau khi build danh sách ranges.
  - `app/routes/books.py`: thêm `POST /books/{book_id}/patches/auto-build` (form handler, redirect về book detail).
  - `app/templates/book_detail.html`: thêm form "Auto-build patches" ngay sau section "Patch builder" link, trước table patches.
- **DB**: không thay đổi schema.
- **Backward compat**: Sách cũ vẫn dùng auto-group theo `patch_size` lúc upload (xem `create_book`). Auto-build chỉ chạy khi user submit form.
- **Tests**: cần test cho thuật toán skip-excluded, edge cases (empty range, out of bounds), end-to-end qua form submit.
