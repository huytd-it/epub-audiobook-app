## Context

Hiện tại hệ thống tạo video bằng cách loop một hình nền tĩnh duy nhất kết hợp với file audio hoàn chỉnh qua ffmpeg (`video_gen.generate_video()`). Không có cách nào gán hình riêng cho từng patch, không có preview video per patch, và không có trang tạo video độc lập.

Pipeline hiện tại: EPUB → Chapters → Patches (TTS audio) → Merge final audio → Video (1 hình + audio).

Thay đổi này bổ sung:
1. Gán hình thủ công cho từng patch ngay trong patch builder
2. Tạo/preview video per patch
3. Trang Video Creator độc lập — upload audio + image + ffmpeg config → mp4

## Goals / Non-Goals

**Goals:**
- Gán hình ảnh thủ công cho từng patch: upload ngay trong row, preview thumbnail, xóa
- Nút "Generate Video" per patch → tạo video 1 patch → preview video inline
- Trang Video Creator standalone: upload audio + image + config ffmpeg → generate mp4
- Tạo video hoàn chỉnh (concat tất cả patches) với settings đã cấu hình
- Fallback: patch không có hình → dùng book background → default

**Non-Goals:**
- Không hỗ trợ upload video clip cho patch
- Không thay đổi logic TTS audio synthesis
- Không hỗ trợ transition effects phức tạp giữa các segment (chỉ cut)
- Không lưu trữ video đã tạo từ Video Creator vào book (độc lập)

## Decisions

### 1. Gán hình thủ công per patch (không auto-assign theo thứ tự)

**Lựa chọn**: Mỗi patch row trong patch builder có form upload riêng. User upload hình trực tiếp cho patch đó, preview ngay.

**Lý do**: 
- User muốn kiểm soát chính xác hình nào cho patch nào
- Upload + preview ngay trong row → UX trực quan hơn

### 2. Per-patch video preview (không phải concat ngay)

**Lựa chọn**: Nút "Generate Video" per patch tạo video 1 patch duy nhất. User preview ngay trong row. Video hoàn chỉnh (concat) tạo riêng qua nút "Generate Full Video".

**Lý do**:
- User muốn xem kết quả từng patch trước khi tạo full video
- Video 1 patch nhanh (vài giây) → preview ngay

### 3. Animated image: Ken Burns effect

**Lựa chọn**: Hỗ trợ 2 kiểu hình: static (loop như cũ) và animated (Ken Burns effect — slow zoom + pan).

**Animation types**:
- `none`: Static image (như hiện tại)
- `zoom-in`: Slow zoom vào tâm
- `zoom-out`: Slow zoom ra từ tâm
- `pan-left`: Pan từ phải sang trái
- `pan-right`: Pan từ trái sang phải

**Thực hiện**: ffmpeg `zoompan` filter, không cần thêm dependency.

### 4. Video Creator page — standalone

**Lựa chọn**: Trang `/video` độc lập, không phụ thuộc vào book/patch. Upload audio + image + cấu hình ffmpeg → generate mp4.

**Lý do**:
- User muốn tạo video nhanh từ bất kỳ audio + image nào, không cần tạo book
- Hữu ích cho testing, preview nhanh, hoặc tạo video ngoài workflow EPUB

**Các trường ffmpeg config**:
- Resolution: preset `1920x1080`, `1280x720`, `854x480` hoặc custom
- FPS: `24`, `30`, `60`
- Video codec: `libx264`, `h264_nvenc`
- Audio bitrate: `128k`, `192k`, `256k`, `320k`
- Image type: `static`, `zoom-in`, `zoom-out`, `pan-left`, `pan-right`
- CRF (quality): slider 18-28, default 23

**UX flow**:
1. User truy cập `/video`
2. Upload file âm thanh (wav, mp3, m4a)
3. Upload hình ảnh (jpg, png, webp) — hoặc dùng default
4. Chọn/cấu hình ffmpeg settings
5. Click "Generate Video" → loading indicator
6. Hiển thị video preview + nút Download
7. Có nút "Tạo video khác" để reset form

**Lưu trữ**: File output tạm trong `data/tmp/video_creator/`, auto-cleanup sau 1 giờ.

### 5. Concat video: ffmpeg concat demuxer

**Lựa chọn**: Tạo từng segment video rồi dùng ffmpeg concat demuxer.

**Lý do**: Tận dụng ffmpeg đã có, mỗi segment xử lý độc lập.

### 6. Video Settings lưu trong book

**Lựa chọn**: Lưu video settings vào bảng `book` (columns mới). Mỗi book có settings riêng cho full video generation.

## Risks / Trade-offs

- **[Risk] Animated image chậm hơn static** → Mitigation: Ken Burns chỉ là ffmpeg filter, tăng thời gian encode nhưng không đáng kể
- **[Risk] Per-patch preview tạo nhiều file tạm** → Mitigation: Cleanup sau khi preview, hoặc giữ file nếu user muốn download
- **[Risk] Video Creator file tạm chiếm storage** → Mitigation: Auto-cleanup sau 1 giờ, hoặc cleanup khi server restart
- **[Risk] Resolution khác nhau giữa các patch** → Mitigation: Normalize tất cả segment về cùng resolution trước khi concat

## Migration Plan

1. DB migration: thêm columns vào `patch` và `book`
2. Tạo route mới `routes/video.py` cho Video Creator
3. Cập nhật `patch_builder.html` — thêm image upload + preview + generate video per row
4. Tạo template `video_creator.html`
5. Rewrite `video_gen.py` — thêm animated image support + per-patch generation + standalone creator
6. Cập nhật `worker.py` — sử dụng settings mới khi tạo full video
