## ADDED Requirements

### Requirement: Video Creator standalone page
Hệ thống SHALL cung cấp trang Video Creator tại `/video` — trang độc lập, không phụ thuộc vào book/patch workflow. Trang cho phép upload file âm thanh + hình ảnh + cấu hình ffmpeg settings → generate .mp4.

#### Scenario: Truy cập Video Creator
- **WHEN** user click link "Video Creator" từ nav bar
- **THEN** hiển thị trang với form upload audio, upload image, ffmpeg settings, nút Generate

#### Scenario: Tạo video thành công
- **WHEN** user upload audio `sample.wav`, image `bg.jpg`, chọn resolution `1920x1080`, FPS `30`, image type `zoom-in`, click Generate
- **THEN** hệ thống gọi ffmpeg, hiển thị video preview + nút Download khi hoàn tất

#### Scenario: Generate khi thiếu file
- **WHEN** user chưa upload audio hoặc image (và không chọn default) và click Generate
- **THEN** hiển thị lỗi "Please upload both audio and image files"

### Requirement: Video Creator form fields
Hệ thống SHALL cung cấp các trường cấu hình ffmpeg trên trang Video Creator:
- **Audio file**: upload bắt buộc (wav, mp3, m4a, ogg)
- **Image file**: upload tùy chọn (jpg, png, webp) — nếu không upload, dùng default background
- **Resolution**: preset `1920x1080`, `1280x720`, `854x480`
- **FPS**: `24`, `30`, `60`
- **Video codec**: `libx264` (default), `h264_nvenc`
- **Audio bitrate**: `128k`, `192k`, `256k`, `320k`
- **Image type**: `none` (static), `zoom-in`, `zoom-out`, `pan-left`, `pan-right`
- **CRF (quality)**: range 18-28, default 23

#### Scenario: Config đầy đủ
- **WHEN** user chọn tất cả các trường và click Generate
- **THEN** ffmpeg được gọi với đúng các tham số đã chọn

#### Scenario: Config tối thiểu (default)
- **WHEN** user chỉ upload audio + image và click Generate (không thay đổi settings)
- **THEN** ffmpeg dùng default: 1920x1080, 30fps, libx264, 192k audio, static image, CRF 23

### Requirement: Video Creator output và preview
Hệ thống SHALL hiển thị video preview ngay trên trang sau khi generate thành công. Video player có controls (play/pause/seek). Nút Download cho phép tải file .mp4.

#### Scenario: Preview video
- **WHEN** video generate thành công
- **THEN** trang hiển thị `<video>` element với controls, nút Download, và nút "Tạo video khác"

#### Scenario: Tạo video khác
- **WHEN** user click "Tạo video khác"
- **THEN** form reset về trạng thái ban đầu, video preview bị ẩn

### Requirement: Video Creator temporary storage
Hệ thống SHALL lưu output video tạm trong `data/tmp/video_creator/`. File được tự động xóa sau khi server restart hoặc sau 1 giờ.

#### Scenario: File tạm được tạo
- **WHEN** video generate thành công
- **THEN** file mp4 được lưu vào `data/tmp/video_creator/{uuid}.mp4`, URL preview trả về đường dẫn relative

#### Scenario: Cleanup file tạm
- **WHEN** server restart HOẶC file tồn tại quá 1 giờ
- **THEN** file tạm bị xóa (best-effort)

### Requirement: Video Creator navigation
Hệ thống SHALL hiển thị link "Video Creator" trong nav bar (base.html), luôn visible bất kể có book nào hay không.

#### Scenario: Nav bar có link Video Creator
- **WHEN** user truy cập bất kỳ trang nào
- **THEN** nav bar hiển thị link "Video Creator" bên cạnh "Books" và "Logs"

## MODIFIED Requirements

### Requirement: Auto-enqueue video on book finalization
Hệ thống SHALL, sau khi `final_audio_path` được set, tự động enqueue `book_job` type `video` nếu book có ít nhất một trong: `background_image_path` không null HOẶC có ít nhất một patch với `image_path` không null. Enqueue PHẢI idempotent.

#### Scenario: Book có background image và patch images
- **WHEN** worker finalizes audio cho book có `background_image_path` và 2 patch có `image_path`
- **THEN** `book_job` type `video` được enqueue

#### Scenario: Book chỉ có patch images
- **WHEN** worker finalizes audio cho book có `background_image_path IS NULL` nhưng có patch với `image_path`
- **THEN** `book_job` type `video` được enqueue

#### Scenario: Book không có hình nào
- **WHEN** worker finalizes audio cho book không có bất kỳ hình ảnh nào
- **THEN** không enqueue `book_job`

#### Scenario: Refinalize đã có video job
- **WHEN** book đã có `book_job` type `video` trạng thái `done`
- **THEN** không enqueue job mới
