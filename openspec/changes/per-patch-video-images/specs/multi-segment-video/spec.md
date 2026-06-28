## ADDED Requirements

### Requirement: Generate video per patch
Hệ thống SHALL cho phép tạo video cho từng patch riêng lẻ thông qua nút "Generate Video" trong patch builder row. Video được tạo từ hình ảnh + audio của patch đó, lưu vào `data/books/{book_id}/patch_videos/{patch_id}.mp4`.

#### Scenario: Tạo video cho patch có hình riêng
- **WHEN** user click "Generate Video" cho patch 2 (có image_path và audio_path)
- **THEN** hệ thống tạo video từ hình patch 2 + audio patch 2, lưu output, hiển thị video preview trong row

#### Scenario: Tạo video cho patch không có hình riêng
- **WHEN** user click "Generate Video" cho patch 3 (không có image_path, có audio_path)
- **THEN** hệ thống dùng hình fallback (book background hoặc default), tạo video, hiển thị preview

#### Scenario: Generate video khi chưa có audio
- **WHEN** user click "Generate Video" cho patch chưa có audio (status != done)
- **THEN** hệ thống hiển thị lỗi "Patch audio not ready"

### Requirement: Video preview per patch
Hệ thống SHALL hiển thị video player ngay trong patch builder row sau khi video được tạo thành công. Video player cho phép play/pause và download.

#### Scenario: Patch đã có video preview
- **WHEN** patch có file video preview tồn tại
- **THEN** row hiển thị `<video>` element với controls, có nút download

#### Scenario: Patch chưa có video preview
- **WHEN** patch chưa tạo video preview
- **THEN** row không hiển thị video player, chỉ có nút "Generate Video"

### Requirement: Segment video format
Mỗi segment video SHALL được tạo bằng ffmpeg với cấu hình: image (static loop hoặc animated Ken Burns) + audio, codec video libx264 (hoặc h264_nvenc), codec audio AAC 192k, pixel format yuv420p. Resolution và FPS theo book settings.

#### Scenario: Tạo segment với static image
- **WHEN** `patch.image_type = 'static'` hoặc NULL
- **THEN** ffmpeg chạy với `-loop 1 -i image.jpg -i audio.wav`

#### Scenario: Tạo segment với animated image (Ken Burns)
- **WHEN** `patch.image_type = 'animated'`
- **THEN** ffmpeg chạy với zoompan filter (zoom-in/out hoặc pan-left/right theo book settings)

#### Scenario: Segment với NVENC
- **WHEN** `settings.use_nvenc = True`
- **THEN** segment dùng codec `h264_nvenc`

### Requirement: Full video generation (concat all patches)
Hệ thống SHALL tạo video hoàn chỉnh bằng cách concat tất cả segment video của các patch. Sử dụng ffmpeg concat demuxer. Các segment phải có cùng resolution.

#### Scenario: Concat 3 patch segments
- **WHEN** book có 3 patch, mỗi patch đã có segment video
- **THEN** hệ thống concat thành video hoàn chỉnh, lưu vào `data/books/{book_id}/video_{job_id}.mp4`

#### Scenario: Segment có resolution khác nhau
- **WHEN** các segment có resolution khác nhau
- **THEN** tất cả segment được scale về resolution theo book settings trước khi concat

### Requirement: Fallback image chain
Hệ thống SHALL sử dụng chuỗi fallback: `patch.image_path` → `book.background_image_path` → `settings.default_background_image`. Nếu cả 3 đều không có, FAIL với error rõ ràng.

#### Scenario: Fallback chain đầy đủ
- **WHEN** `patch.image_path IS NULL`, `book.background_image_path = '/path/to/bg.jpg'`
- **THEN** segment dùng `/path/to/bg.jpg`

#### Scenario: Không có hình nào
- **WHEN** cả 3 nguồn đều NULL/không tồn tại
- **THEN** FAIL với error "no background image available"

### Requirement: Segment cleanup
Hệ thống SHALL xóa segment video tạm sau khi concat thành công. Nếu concat thất bại, giữ nguyên files để debug.

#### Scenario: Cleanup sau concat thành công
- **WHEN** concat video hoàn tất
- **THEN** tất cả segment tạm bị xóa, chỉ giữ video output cuối

### Requirement: Per-patch video cleanup
Hệ thống SHALL xóa video preview của patch khi patch image bị thay đổi/xóa hoặc khi patch audio được regenerate. Video preview cũ bị xóa (best-effort).

#### Scenario: Thay đổi hình patch
- **WHEN** user upload hình mới cho patch
- **THEN** video preview cũ bị xóa (best-effort), user cần generate lại
