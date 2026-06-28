## ADDED Requirements

### Requirement: Patch image storage
Hệ thống SHALL lưu đường dẫn hình ảnh riêng cho mỗi patch thông qua cột `image_path` (TEXT, nullable) trong bảng `patch`. Giá trị `image_path` là `NULL` khi patch không có hình riêng.

#### Scenario: Patch có hình ảnh riêng
- **WHEN** patch được gán hình ảnh (image_path được set thành đường dẫn file)
- **THEN** `patch.image_path` chứa đường dẫn tuyệt đối đến file hình ảnh

#### Scenario: Patch không có hình ảnh riêng
- **WHEN** patch không được gán hình ảnh
- **THEN** `patch.image_path` là `NULL`

### Requirement: Upload hình ảnh thủ công per patch
Hệ thống SHALL cho phép người dùng upload hình ảnh trực tiếp cho từng patch trong patch builder UI. Mỗi patch row có form upload riêng (input file + nút Upload). Hình ảnh được lưu vào `data/uploads/{book_id}/patches/{patch_id}/`.

#### Scenario: Upload hình cho patch 2
- **WHEN** user chọn file `img.jpg` và click Upload trong patch 2 row
- **THEN** file được lưu, `patch.image_path` được cập nhật, thumbnail hiển thị ngay trong row

#### Scenario: Upload đè hình cũ
- **WHEN** patch 2 đã có hình và user upload hình mới
- **THEN** hình cũ bị xóa (best-effort), hình mới được lưu, thumbnail cập nhật

### Requirement: Preview hình ảnh per patch
Hệ thống SHALL hiển thị thumbnail hình ảnh hiện tại của mỗi patch ngay trong patch builder row. Nếu patch không có hình riêng, hiển thị placeholder text "default" với link preview hình default.

#### Scenario: Patch có hình riêng
- **WHEN** patch có `image_path` khác NULL
- **THEN** row hiển thị thumbnail hình ảnh (nhỏ, click để xem full size)

#### Scenario: Patch không có hình riêng
- **WHEN** patch có `image_path = NULL`
- **THEN** row hiển thị placeholder "default (book background)" với link preview

### Requirement: Xóa hình ảnh patch
Hệ thống SHALL cho phép xóa hình ảnh của patch thông qua nút "Xóa hình" trong patch builder row. Sau khi xóa, `patch.image_path` được set về `NULL`.

#### Scenario: Xóa hình patch
- **WHEN** user click "Xóa hình" cho patch 2
- **THEN** `patch.image_path` = NULL, file hình bị xóa (best-effort), UI hiển thị placeholder

### Requirement: Patch image cleanup
Hệ thống SHALL xóa file hình ảnh của patch khi book bị xóa hoặc khi patch image bị thay đổi/xóa. Cleanup phải là best-effort.

#### Scenario: Xóa book có patch images
- **WHEN** book bị xóa (DELETE CASCADE)
- **THEN** tất cả file hình ảnh trong `data/uploads/{book_id}/patches/` bị xóa (best-effort)

### Requirement: Image type per patch
Hệ thống SHALL lưu kiểu hiển thị hình ảnh cho mỗi patch thông qua cột `image_type` (TEXT, default `'static'`). Giá trị hợp lệ: `static`, `animated`. Kiểu `animated` sử dụng Ken Burns effect (zoom/pan) thay vì loop hình tĩnh.

#### Scenario: Patch với image_type = static
- **WHEN** `patch.image_type = 'static'`
- **THEN** video segment sử dụng hình tĩnh loop (như hiện tại)

#### Scenario: Patch với image_type = animated
- **WHEN** `patch.image_type = 'animated'`
- **THEN** video segment sử dụng Ken Burns effect (zoom/pan trên hình)
