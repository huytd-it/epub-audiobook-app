## 1. Database Migration

- [x] 1.1 Thêm cột `image_path` (TEXT nullable), `image_type` (TEXT default 'static') vào bảng `patch` trong `app/db.py`
- [x] 1.2 Thêm cột `video_resolution` (TEXT default '1920x1080'), `video_fps` (INTEGER default 30), `default_image_animation` (TEXT default 'none') vào bảng `book`
- [x] 1.3 Cập nhật dataclass `Patch` trong `app/models.py` — thêm `image_path`, `image_type`
- [x] 1.4 Cập nhật dataclass `Book` trong `app/models.py` — thêm `video_resolution`, `video_fps`, `default_image_animation`

## 2. Patch Image CRUD

- [x] 2.1 Tạo hàm `save_patch_image(conn, patch_id, file)` trong `app/repository.py` — lưu file vào `data/uploads/{book_id}/patches/{patch_id}/`, cập nhật `image_path`
- [x] 2.2 Tạo hàm `clear_patch_image(conn, patch_id)` — set `image_path = NULL`, xóa file cũ (best-effort)
- [x] 2.3 Tạo hàm `update_patch_image_type(conn, patch_id, image_type)` — cập nhật `image_type`
- [x] 2.4 Tạo endpoint `POST /patches/{patch_id}/image` trong `app/routes/patches.py` — upload hình cho patch
- [x] 2.5 Tạo endpoint `DELETE /patches/{patch_id}/image` trong `app/routes/patches.py` — xóa hình patch
- [x] 2.6 Tạo endpoint `POST /patches/{patch_id}/image-type` trong `app/routes/patches.py` — cập nhật image_type
- [x] 2.7 Tạo endpoint `GET /patches/{patch_id}/image` — serve hình ảnh patch (hoặc redirect default)

## 3. Per-Patch Video Generation

- [x] 3.1 Tạo hàm `generate_segment(image_path, audio_path, out_path, *, image_type, resolution, fps, audio_bitrate, crf, use_nvenc)` trong `app/video_gen.py` — tạo 1 segment video (static hoặc animated)
- [x] 3.2 Implement Ken Burns effect bằng ffmpeg zoompan filter cho animated image_type (zoom-in, zoom-out, pan-left, pan-right)
- [x] 3.3 Tạo endpoint `POST /patches/{patch_id}/generate-video` trong `app/routes/patches.py` — tạo video cho 1 patch
- [x] 3.4 Tạo endpoint `GET /patches/{patch_id}/video` — serve video preview patch
- [x] 3.5 Tạo hàm `cleanup_patch_video(patch_id)` — xóa video preview cũ khi image/audio thay đổi

## 4. Full Video Generation (Concat)

- [x] 4.1 Tạo hàm `concat_segments(segment_paths, out_path)` trong `app/video_gen.py` — ffmpeg concat demuxer
- [x] 4.2 Tạo hàm `resolve_patch_image(patch, book, default_image)` — fallback chain
- [x] 4.3 Tạo hàm `generate_full_video(patches, book, out_path, *, use_nvenc)` — orchestrate: resolve images → tạo segments → concat → cleanup
- [x] 4.4 Cập nhật `_run_video_job` trong `app/worker.py` — gọi `generate_full_video` với book settings

## 5. Video Creator Standalone Page

- [x] 5.1 Tạo `app/routes/video.py` — route GET `/video` (hiển thị form), POST `/video/generate` (generate video)
- [x] 5.2 Tạo template `app/templates/video_creator.html` — form: upload audio, upload image, ffmpeg settings (resolution, fps, codec, audio_bitrate, image_type, crf), nút Generate, video preview area
- [x] 5.3 Tạo hàm `generate_standalone_video(audio_path, image_path, out_path, *, resolution, fps, codec, audio_bitrate, image_type, crf)` trong `app/video_gen.py`
- [x] 5.4 Tạo endpoint serve output video `GET /video/output/{filename}` — serve file từ `data/tmp/video_creator/`
- [x] 5.5 Tạo cleanup logic cho `data/tmp/video_creator/` — xóa file cũ hơn 1 giờ khi server start
- [x] 5.6 Cập nhật `app/main.py` — register router mới
- [x] 5.7 Cập nhật `templates/base.html` — thêm nav link "Video Creator"

## 6. Patch Builder UI

- [x] 6.1 Cập nhật `templates/patch_builder.html` — mỗi patch row có: thumbnail preview, upload form, nút xóa hình, dropdown image_type, nút Generate Video, video preview player
- [x] 6.2 Thêm CSS cho patch image thumbnail, video preview inline, upload form compact
- [x] 6.3 Cập nhật `templates/book_detail.html` — thêm link "Video Settings" (book-level config), cập nhật video section

## 7. Worker & Auto-enqueue

- [x] 7.1 Cập nhật điều kiện auto-enqueue trong `_merge_final_audio` — check cả patch images
- [x] 7.2 Cập nhật startup backfill trong `app/main.py` — điều kiện backfill check patch images
- [x] 7.3 Cập nhật `_run_video_job` sử dụng book settings (resolution, fps, animation)

## 8. Cleanup & Edge Cases

- [x] 8.1 Cập nhật book deletion — xóa thư mục `patches/` và `patch_videos/` khi xóa book
- [x] 8.2 Xử lý khi rebuild patches — reset image_path/image_type = NULL
- [x] 8.3 Validation: chỉ chấp nhận jpg/png/webp khi upload image, wav/mp3/m4a/ogg khi upload audio
- [x] 8.4 Cleanup video preview khi patch audio được regenerate

## 9. Tests

- [x] 9.1 Viết test cho upload/delete patch image endpoints
- [x] 9.2 Viết test cho `resolve_patch_image` — fallback chain
- [x] 9.3 Viết test cho `generate_segment` — static và animated (mock ffmpeg)
- [x] 9.4 Viết test cho per-patch video generation endpoint
- [x] 9.5 Viết test cho Video Creator standalone page (upload + generate + preview)
- [x] 9.6 Viết test cho auto-enqueue với điều kiện mới
