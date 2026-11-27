# Quản lý tài khoản Google Drive cho batch export

App chỉ xuất package Colab/Kaggle theo **batch**. Người dùng chọn một hoặc nhiều patch trên trang sách,
mở modal **Export**, chọn model TTS và đích Drive, rồi tạo một package chứa
`batch_manifest.json`, các thư mục patch và batch notebook.

Package chỉ gồm **text + clip voice reference**: text của từng chunk nằm ngay trong `manifest.json` của
patch, còn ảnh nền và nhạc nền **không** được đóng gói — video vẫn render tại app từ file WAV import về.
Nhờ vậy mỗi patch thêm vào batch chỉ tốn thêm một file JSON nhỏ, sync lên Drive nhanh hơn hẳn.

Cơ chế Drive: app copy package batch vào một **folder cục bộ**
trên máy chạy server; folder đó phải được đồng bộ lên đúng tài khoản Google Drive bằng cách nào đó.
App không quan tâm folder được đồng bộ bằng cách nào — chỉ cần đúng đường dẫn.

## Quy trình batch export

1. Mở trang chi tiết sách và chuyển tới bảng **Patches**.
2. Chọn các patch cần xử lý. Có thể chọn duy nhất một patch; package tạo ra vẫn là package batch.
3. Bấm **Export** để mở modal TTS.
4. Chọn model, voice hoặc ngôn ngữ, `max_chars` và hiệu ứng nếu cần.
5. Chọn một trong các đích:
   - **Download selected (.zip)** để tải package và tự đưa lên Colab/Kaggle.
   - **Export vào Drive Desktop** để copy vào sync target cục bộ.
   - **Export lên Drive (Kaggle)** để upload qua Drive API.
6. Mở `colab_kaggle_batch_tts_template.ipynb`, đặt đúng cờ `IS_KAGGLE`, rồi chạy các cell theo thứ tự.
7. Notebook tạo WAV và timeline trong thư mục `result`. Quay lại app để import kết quả rồi render video.

Khi chỉ cần xử lý một patch, hãy chọn đúng patch đó trong bảng rồi dùng batch export như trên.

Hiện có 10 tài khoản (`codex1`..`codex10@g.lsts.edu.vn`), chia làm 2 nhóm vì **Google Drive for Desktop
chỉ cho đăng nhập tối đa 4 tài khoản cùng lúc** (giới hạn cứng của chính ứng dụng, không cấu hình được).

## Nhóm 1 — codex1-4: Google Drive for Desktop (mount ổ đĩa)

| Sync target | Email | Ổ đĩa | Folder |
|---|---|---|---|
| codex1 | codex1@g.lsts.edu.vn | H: | `H:\My Drive\EPUB Audiobook Exports` |
| codex2 | codex2@g.lsts.edu.vn | G: | `G:\My Drive\EPUB Audiobook Exports` |
| codex3 | codex3@g.lsts.edu.vn | I: | `I:\My Drive\EPUB Audiobook Exports` |
| codex4 | codex4@g.lsts.edu.vn | J: | `J:\My Drive\EPUB Audiobook Exports` |

Các tài khoản này đăng nhập trực tiếp trong Google Drive for Desktop (icon khay hệ thống → Preferences →
Add another account). App export thẳng vào folder trên ổ đĩa đã mount — Drive for Desktop tự đồng bộ lên
cloud ngay, **không cần script gì thêm**.

Muốn đổi 1 trong 4 tài khoản này: sign out tài khoản cũ trong Drive for Desktop trước (vì đã đụng trần 4),
đăng nhập tài khoản mới, rồi sửa lại `account_email` + `folder_path` của sync target tương ứng ở trang `/drive`.

## Nhóm 2 — codex5-10: rclone (không mount ổ đĩa)

| Sync target | Email | Remote rclone | Folder cục bộ (staging) |
|---|---|---|---|
| codex5 | codex5@g.lsts.edu.vn | `codex5:` | `D:\RcloneDriveStaging\codex5` |
| codex6 | codex6@g.lsts.edu.vn | `codex6:` | `D:\RcloneDriveStaging\codex6` |
| codex7 | codex7@g.lsts.edu.vn | `codex7:` | `D:\RcloneDriveStaging\codex7` |
| codex8 | codex8@g.lsts.edu.vn | `codex8:` | `D:\RcloneDriveStaging\codex8` |
| codex9 | codex9@g.lsts.edu.vn | `codex9:` | `D:\RcloneDriveStaging\codex9` |
| codex10 | codex10@g.lsts.edu.vn | `codex10:` | `D:\RcloneDriveStaging\codex10` |

App export vào folder staging cục bộ (giống hệt cách nó ghi vào ổ Drive Desktop-mounted). Folder này **không**
tự động lên cloud — phải đẩy lên bằng rclone.

**Cách 1 — trong UI (khuyến nghị):** ở trang `/drive`, điền cột **rclone remote** cho từng sync target codex5-10
(đích đầy đủ dạng `remote:path`, ví dụ `codex5:EPUB Audiobook Exports`). Khi có remote, dòng đó hiện nút **Sync**;
ngoài ra có nút **Sync all folders** đẩy tất cả target có remote một lượt. Backend chạy `rclone copy` (xem
`_run_rclone_copy` trong `app/routes/drive.py`) — **luôn `copy`, không bao giờ `sync`** (xem luật an toàn bên dưới).

**Cách 2 — script CLI (fallback):**

```powershell
# Đẩy cả 6 tài khoản
.\scripts\rclone_push_drives.ps1

# Chỉ đẩy 1 tài khoản
.\scripts\rclone_push_drives.ps1 codex7
```

Chạy sau khi batch export xong (thủ công), hoặc tự thêm Windows Task Scheduler nếu muốn tự động theo chu kỳ.

### ⚠️ Luật an toàn quan trọng nhất: KHÔNG BAO GIỜ dùng `rclone sync` để đẩy lên các remote này

Script `rclone_push_drives.ps1` dùng `rclone copy`, **không phải** `rclone sync`. Đây không phải lựa chọn tuỳ ý:

- `rclone sync <local> <remote>` làm cho remote **giống hệt** local — nghĩa là nó **xoá** mọi thứ trên remote
  không có trong folder local.
- Ngày 2026-07-21, chạy thử với folder staging codex5 đang trống đã **xoá mất 173 file export thật** đã có sẵn
  trên tài khoản đó từ trước (không phải do script này tạo ra). Phục hồi được nhờ Google Drive Trash
  (`rclone backend untrash "codex5:EPUB Audiobook Exports"`), nhưng lần sau có thể không may mắn vậy.
- Vì mỗi lần batch export, app tạo folder tên riêng có timestamp (xem `app/drive_export.py`), không bao giờ cần
  xoá gì ở phía remote — `copy` là đủ và an toàn tuyệt đối cho mục đích này.

Nếu cần viết thêm script/thao tác rclone khác nhắm vào các remote này, luôn dùng `copy`, không dùng `sync`,
trừ khi bạn chắc chắn 100% folder đích không chứa gì cần giữ.

## Thêm tài khoản mới (vượt quá 10)

1. `rclone config create <tenmoi> drive scope=drive config_is_local=true` — mở trình duyệt để đăng nhập.
2. Tạo folder staging: `mkdir D:\RcloneDriveStaging\<tenmoi>`.
3. Thêm sync target trong DB (hoặc qua UI `/drive`):
   ```python
   import sqlite3
   from datetime import datetime, timezone
   conn = sqlite3.connect("data/app.db")
   now = datetime.now(timezone.utc).isoformat()
   conn.execute(
       "INSERT INTO drive_sync_target (name, account_email, folder_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
       ("<tenmoi>", "<email>", r"D:\RcloneDriveStaging\<tenmoi>", now, now),
   )
   conn.commit()
   ```
4. Thêm remote đó vào `scripts/rclone_push_drives.ps1` (mảng `$targets`).

## Đi qua server 2

Đặt luồng `drive` = *Qua proxy* ở trang **Mạng & Server** (`/network`) thì Google Drive API của
app (đổi code OAuth, refresh token, upload batch, tải kết quả, dọn thư mục sync) ra Internet qua IP
của server 2 — xem [socials-egress.md](socials-egress.md) và
[`deploy/egress-server/`](../deploy/egress-server/README.md). Upload/download dùng chung http của
service nên cũng đi qua proxy.

Không đi qua server 2:

- Bước đăng nhập Google trong trình duyệt (diễn ra trên máy này).
- Nhóm 1 (Google Drive for Desktop, thư mục local): hệ điều hành tự đồng bộ.
- Nhóm 2 (rclone): rclone là tiến trình riêng, không dùng lớp egress của app.
- Kernel trên Kaggle dùng GDRIVE_CREDS từ IP của Kaggle. Refresh token dùng đồng thời từ nhiều kernel
  và từ server 2 là bình thường. Nếu Google thu hồi token (`invalid_grant`), log ghi rõ
  `event=drive.refresh_token_revoked`, và mọi kernel dùng Drive đó cùng mất sync cho tới khi kết nối
  lại đúng email.

## Lưu ý khác

- rclone hiện dùng client_id dùng chung mặc định của chính rclone (thấy warning "This remote uses rclone's
  shared Google Drive client_id... will stop working during 2026" mỗi lần chạy). Trước khi nó ngừng hoạt động,
  cần tạo Client ID/Secret riêng trên Google Cloud Console — xem
  https://rclone.org/drive/#making-your-own-client-id. **Cách nhanh nhất:** dán Client ID/Secret vào panel
  **"rclone OAuth client"** ở trang `/drive` (lưu trong `app_state`, key `rclone.drive_client_id` /
  `rclone.drive_client_secret`); khi bấm Sync trong UI, app tự truyền `--drive-client-id`/`--drive-client-secret`
  nên warning biến mất, **không cần re-auth từng remote**. Script `rclone_push_drives.ps1` (fallback) không đọc
  giá trị này — muốn tắt warning khi chạy script thì `rclone config update <remote> client_id ... client_secret ...`
  hoặc đặt env `RCLONE_DRIVE_CLIENT_ID`/`RCLONE_DRIVE_CLIENT_SECRET`.
- Kiểm tra danh sách remote: `rclone listremotes`. Kiểm tra dung lượng/quota: `rclone about <remote>:`.
- Nếu vô tình xoá nhầm gì trên remote: kiểm tra trash trước khi hoảng — `rclone lsf <remote>: --drive-trashed-only -R`,
  rồi `rclone backend untrash "<remote>:<path>"` để khôi phục hàng loạt.
