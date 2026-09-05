# Hướng dẫn dùng tính năng tự động hoá Kaggle (TTS qua Kaggle Kernels API)

Tính năng này thay thế hoàn toàn Google Drive cho mục tiêu Kaggle: app tự đẩy batch
lên Kaggle bằng API (không cần mở kaggle.com, không cần tạo notebook thủ công), tự
theo dõi tiến độ, tự tải kết quả về và import — tương tự "Export vào Drive" nhưng
không cần Drive, không cần chạy tay notebook.

> ⚠️ **Chưa test với tài khoản Kaggle thật.** Toàn bộ phần push/poll/import được viết
> khớp với mã nguồn chính thức của Kaggle (`kaggle-cli`, `kaggle-sdk-python`), nhưng
> chưa có ai chạy thử với API key thật. Trước khi giao cho việc thật, làm thử **một
> batch nhỏ (1 patch, vài chunk)** trước — xem mục "Trước khi dùng thật" bên dưới.

## Bước 1 — Lấy Kaggle username + API key

1. Đăng nhập [kaggle.com](https://www.kaggle.com), vào **Settings** (bấm avatar góc
   phải → Settings).
2. Cuộn tới mục **API**, bấm **Create New Token**. Trình duyệt tải về file
   `kaggle.json`.
3. Mở file đó ra — có 2 trường:
   ```json
   {"username": "ten_dang_nhap_kaggle", "key": "abcdef0123456789..."}
   ```
   `username` và `key` chính là 2 thứ cần điền ở Bước 2.

Mỗi tài khoản Kaggle miễn phí có khoảng **30 giờ GPU/tuần**. Nếu cần xử lý nhiều hơn,
tạo thêm tài khoản Kaggle khác và thêm cả vào app (xem "Nhiều tài khoản" bên dưới) —
app sẽ tự xoay vòng.

## Bước 2 — Thêm tài khoản Kaggle vào app

1. Mở trang **`/drive`** ("Google Drive & Đồng bộ dữ liệu").
2. Chuyển sang tab **Kaggle** (icon đồng hồ đo, cạnh tab "OAuth clients").
3. Bấm **Thêm tài khoản Kaggle**, điền:
   - **Tên gợi nhớ** — tự đặt, chỉ để phân biệt (vd: "tài khoản chính").
   - **Kaggle username** — trường `username` trong `kaggle.json`.
   - **Kaggle API key** — trường `key` trong `kaggle.json`.
4. Bấm **Lưu**. Tài khoản mới hiện trạng thái **Sẵn sàng**, còn ~30h GPU tuần này.

Mỗi dòng tài khoản có 3 nút: **Sửa** (đổi tên/username, để trống ô API key nếu không
đổi key), **Bật/Tắt** (tắt tạm một tài khoản mà không xoá — app sẽ bỏ qua nó khi chọn
tài khoản để chạy job), và nút xoá (chỉ xoá được khi tài khoản không đang chạy job nào).

## Bước 3 — Chạy TTS qua Kaggle cho một cuốn sách

1. Mở trang chi tiết sách, vào bảng **Patches**.
2. Chọn các patch cần tổng hợp giọng đọc (không chọn gì = lấy toàn bộ patch đang sẵn
   sàng, giống các nút export khác).
3. Ở khối **Export Colab / Kaggle**, chọn **TTS model**, **Voice**, `max_chars` như
   bình thường.
4. Ở cột thứ 4 ("**Kaggle (tự động)**"), bấm **Chạy trên Kaggle**.
   - Nút này bị mờ nếu chưa có tài khoản Kaggle nào (quay lại Bước 2), hoặc chưa chọn
     patch nào sẵn sàng.
5. App báo "Đã đưa N patch vào hàng đợi Kaggle" — nghĩa là đã tạo xong 1 job, chưa
   chạy xong ngay (Kaggle cần thời gian queue + chạy GPU).

## Bước 4 — Theo dõi tiến độ

Mở trang **Queue** — job loại `kaggle_tts` hiện log chi tiết từng bước: đang tạo
dataset, đang push kernel, trạng thái Kaggle trả về (queued/running/complete/error),
đang import patch nào. Khi patch tổng hợp xong, patch tự chuyển sang trạng thái
**done** trong bảng Patches như mọi đường TTS khác — không cần thao tác import thủ
công.

Nếu một batch có nhiều patch và một tài khoản không đủ giờ GPU để xong hết, job **tự
xoay sang tài khoản Kaggle khác** (nếu có) và tiếp tục — không cần làm gì thêm, chỉ
cần đã thêm nhiều tài khoản ở Bước 2.

## Cách hoạt động (tóm tắt)

- Text các chunk + clip giọng mẫu được đóng gói thành một **Kaggle Dataset riêng tư**
  (vì Kaggle không cho đính file trực tiếp vào notebook, chỉ nhận qua Dataset).
- Notebook `colab_kaggle_batch_tts_template.ipynb` được đẩy lên như một **Kernel**
  riêng tư, gắn với dataset ở trên, chạy GPU, tự tải model rồi tổng hợp.
- App poll trạng thái kernel mỗi ~30 giây (`KAGGLE_POLL_INTERVAL_SECONDS`).
- Khi xong (hoặc khi phiên Kaggle bị ngắt giữa chừng), app tải file kết quả về và
  import những patch đã có, giữ nguyên patch chưa xong để lượt sau tiếp tục.
- Quota GPU mỗi tài khoản được **ước tính nội bộ** (app tự cộng dồn thời gian mỗi lần
  chạy, không lấy số liệu thật từ Kaggle vì Kaggle không cung cấp API cho việc này) —
  xem cột "còn ~Xh GPU tuần này" ở trang `/drive`.

## Nhiều tài khoản

Không giới hạn số tài khoản Kaggle có thể thêm. Mỗi tài khoản chỉ chạy **một job
`kaggle_tts` tại một thời điểm** (số job kaggle_tts chạy song song = số tài khoản chưa
tắt). Thêm càng nhiều tài khoản, càng nhiều batch có thể chạy song song và càng nhiều
giờ GPU/tuần khả dụng tổng cộng.

## Trước khi dùng thật — nên biết

Phần dưới đây là các điểm **chưa xác minh được với tài khoản Kaggle thật** (chỉ đối
chiếu mã nguồn chính thức của Kaggle, xem
`docs/superpowers/specs/2026-09-05-kaggle-api-tts-automation-design.md` để biết chi
tiết):

- **Hủy job không thực sự dừng kernel trên Kaggle.** Bấm Cancel ở trang Queue sẽ dừng
  app theo dõi job đó ngay, nhưng kernel vẫn có thể tiếp tục chạy trên Kaggle tới khi
  tự hết giờ (Kaggle không cho biết session ID cần thiết để gọi API hủy thật). Nếu
  cần dừng hẳn, vào kaggle.com/code, tìm kernel `epub-tts-batch-...` và dừng tay.
- **Mỗi lượt chạy tạo một Dataset mới** (`epub-tts-data-...`) thay vì cập nhật một
  dataset cũ — dataset cũ không tự xoá. Nếu chạy nhiều batch, thỉnh thoảng vào
  kaggle.com/<username>/datasets dọn bớt các dataset `epub-tts-data-*` cũ.
- **License dataset đang để cứng `CC0-1.0`** — chưa xác nhận Kaggle có chấp nhận giá
  trị này cho dataset riêng tư hay không.
- Khuyến nghị: chạy thử 1 patch ngắn trước, xem job có báo lỗi ở bước nào (tạo
  dataset / push kernel / poll trạng thái / tải kết quả) rồi báo lại để vá đúng chỗ.

## Xử lý sự cố

- **Job kaggle_tts đứng ở "pending" rất lâu, không chạy:** kiểm tra trang `/drive`
  tab Kaggle — nếu mọi tài khoản đều "Hết quota tuần này", job đang tự đợi tới lúc
  quota hồi phục (không cần làm gì, hoặc thêm tài khoản mới nếu cần gấp).
- **Job báo lỗi ngay (failed) thay vì thử lại:** xem log job ở trang Queue — lỗi do
  thiếu clip giọng mẫu (với model cần clone giọng) hoặc patch không còn tồn tại sẽ
  dừng ngay, không tự thử lại; sửa xong thì bấm export lại từ đầu.
- **Không xoá được tài khoản Kaggle:** tài khoản đang được một job dùng — đợi job đó
  xong (hoặc hủy job đó ở trang Queue) rồi xoá.
