## Why

Hiện tại video generation chỉ dùng 1 hình nền tĩnh cho toàn bộ audiobook, không có cách nào gán hình riêng cho từng patch, và không có trang tạo video độc lập. Cần:
1. Cho phép gán hình ảnh **thủ công** cho từng patch (upload + preview ngay trong row)
2. Mỗi patch có nút **Generate Video** riêng để tạo video preview ngay lập tức
3. Trang **Video Creator** độc lập — upload file âm thanh + hình ảnh + cấu hình ffmpeg → output .mp4

## What Changes

- Thêm cột `image_path`, `image_type` (static/animated) vào bảng `patch`
- Thêm cột video settings vào bảng `book` (`video_resolution`, `video_fps`, `default_image_animation`)
- Patch builder UI: mỗi row có upload hình + thumbnail preview + nút Generate Video + video preview
- Trang Video Creator mới (`/video`) — standalone tool: upload audio + image + ffmpeg settings → mp4
- `video_gen.py`: hỗ trợ animated image (Ken Burns effect) và static image
- Endpoint tạo video cho từng patch riêng lẻ (preview)
- Endpoint tạo video hoàn chỉnh từ tất cả patches

## Capabilities

### New Capabilities
- `per-patch-images`: Gán hình ảnh thủ công cho từng patch — upload, preview thumbnail, xóa, cập nhật ngay trong patch builder row
- `per-patch-video-preview`: Tạo và preview video cho từng patch riêng lẻ ngay trong patch builder
- `video-creator`: Trang Video Creator độc lập — upload audio + image + cấu hình ffmpeg (codec, resolution, FPS, animation, audio bitrate) → generate .mp4

### Modified Capabilities
- `book-video-job`: Thay đổi auto-enqueue condition; cập nhật video generation sử dụng settings mới

## Impact

- **Database**: Migration thêm columns vào `patch` và `book`
- **Code**: `video_gen.py` (rewrite + animated support), `worker.py`, `repository.py`, `routes/patches.py`, `routes/video.py` (mới)
- **UI**: `patch_builder.html` (hàng mới per patch), `video_creator.html` (mới), `book_detail.html`, `base.html` (nav link)
- **Dependencies**: ffmpeg (đã có)
