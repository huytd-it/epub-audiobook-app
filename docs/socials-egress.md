# Socials hub: nhiều tài khoản và định tuyến mạng (relay / proxy)

Trang **Socials** (`/socials`) gom mọi mạng xã hội vào một chỗ: mỗi mạng một tab với giao diện riêng,
cùng một thanh tài khoản dùng chung. Lớp egress (`app/egress.py`) quyết định request ra ngoài đi đường
nào. Endpoint và định tuyến được cấu hình ở trang riêng **Mạng & Server** (`/network`, API
`/network/api`). Đường dẫn cũ `/socials/api/network/*` vẫn chạy như alias cho client cũ.

## Tài khoản

| Mạng | Một tài khoản là | Cách thêm |
|---|---|---|
| YouTube | một kênh | OAuth — xem [youtube-connect.md](youtube-connect.md) |
| Facebook | một Page | dán Page ID + Page Access Token (app gọi Graph API để xác nhận trước khi lưu) |
| TikTok | một tài khoản | dán Open ID + access token |

Mọi tài khoản nằm trong bảng `social_account`. Mỗi mạng có một tài khoản **mặc định** (dấu sao), là tài
khoản được dùng khi một luồng không chọn gì; nhờ vậy các luồng tự động có từ trước chạy tiếp không cần
cấu hình lại. Nhập lại đúng Page ID / Open ID / kênh đã có là cập nhật token, không tạo tài khoản mới.

Dữ liệu của bản cũ (một tài khoản mỗi mạng) được chép sang tự động ở lần khởi động đầu tiên và trở thành
tài khoản mặc định.

Chọn tài khoản khi đăng:

- **Video sách nói**: theo kênh của sách (tab YouTube trong cấu hình sách), không thì kênh mặc định.
- **Tải thủ công** ở Socials → YouTube: kênh đang chọn trên thanh tài khoản.

## Định tuyến mạng (relay / proxy)

Hai loại điểm thoát:

| | Outbound proxy | Vercel relay |
|---|---|---|
| Là gì | proxy `http://`, `https://`, `socks5://`, `socks5h://` của bạn | Edge Function trong `relay/` do bạn tự deploy |
| Dùng cho | AI, YouTube, Facebook, TikTok, Kaggle API, Google Drive API | chỉ AI |
| Upload video | được | không — Vercel giới hạn body ~4,5 MB |

Deploy relay: xem [`relay/README.md`](../relay/README.md).

### Định tuyến theo luồng

Mỗi luồng request (*scope*) có một dòng trong bảng **Định tuyến theo luồng**:

| Scope | Gồm những gì |
|---|---|
| `ai` | sinh mô tả/thẻ YouTube, sinh nội dung và ảnh bìa (`app/ai_content.py`, `app/ai_providers.py`) |
| `youtube` | OAuth, làm mới token, mọi lời gọi YouTube Data API, upload video |
| `facebook` | Graph API và upload Reels |
| `tiktok` | Content Posting API và upload |
| `kaggle` | Kaggle API: push kernel, trạng thái, tải output, kernel log, quota GPU, upload blob (`app/kaggle_api.py`) |
| `drive` | Google Drive API của app: đổi code OAuth, refresh token, upload batch, tải kết quả, dọn thư mục sync (`app/google_drive.py`) |

Mặc định mọi scope **đi thẳng** — chưa cấu hình gì thì app chạy y như trước. TTS qua API và tải ảnh nền
(Pollinations) chưa nằm trong scope nào và luôn đi thẳng. `kaggle` và `drive` chỉ dùng được outbound
proxy, không dùng relay.

Chỉ định tuyến **ở mức scope**: cả luồng ra một IP. Không có ghim proxy riêng cho từng tài khoản
Kaggle/Drive. Cố ý không làm ghim IP để tách nhiều tài khoản Kaggle của cùng một người, vì việc đó vi
phạm ToS của Kaggle.

Giới hạn (app không định tuyến được):

- Kernel chạy **trên Kaggle** tự đồng bộ Drive (GDRIVE_CREDS) từ IP của Kaggle.
- Chế độ "thư mục local" của Google Drive for desktop do hệ điều hành xử lý.
- Bước đăng nhập Google/YouTube trong trình duyệt diễn ra trên máy chạy app. Chỉ bước đổi token,
  refresh token và các lời gọi API mới đi qua proxy.
- Upload YouTube đi máy 1 → proxy → YouTube, nên tốc độ theo link chậm hơn trong hai link và tốn băng
  thông của máy proxy.

### Server 2 làm điểm thoát mạng (LAN + Tailscale)

Cài gost trên server 2 bằng script trong [`deploy/egress-server/`](../deploy/egress-server/README.md),
rồi thêm **2 endpoint** proxy trỏ tới cùng server 2: `socks5h://user:pass@<IP-LAN>:PORT` và
`socks5h://user:pass@<100.x>:PORT`. Cả hai ra cùng một IP public, nên đặt `youtube`, `kaggle`, `drive` =
*Qua proxy*, chọn cả hai, *Xoay vòng* là an toàn. Bấm **Kiểm tra** trên từng endpoint: hai IP thoát phải
giống nhau.

Trang Mạng cũng **cảnh báo** kênh đang ghim một proxy lệch với định tuyến của luồng (endpoint ghim không
nằm trong danh sách của luồng, hoặc luồng đang đi thẳng nên ghim bị bỏ qua). Bỏ ghim ở Socials.

Cách chọn endpoint:

- **Xoay vòng mỗi request** (mặc định của `ai`): lần lượt qua các endpoint đang sẵn sàng.
- **Cố định theo tài khoản** (mặc định của mạng xã hội): mỗi tài khoản luôn ra cùng một endpoint.
- **Ghim trên tài khoản**: ô *Outbound proxy* trên thanh tài khoản chỉ định đích danh một proxy cho tài
  khoản đó, thắng cả policy của scope.

### Khi endpoint hỏng

- Lỗi kết nối, proxy đòi xác thực (407), provider giới hạn tốc độ (429) hoặc chặn theo vùng (451, hoặc
  thông báo kiểu "User location is not supported") làm endpoint đó **tạm nghỉ** — 30 giây, gấp đôi sau mỗi
  lần lỗi liên tiếp, tối đa 30 phút — và request được thử lại qua endpoint kế tiếp, tối đa 3 endpoint.
- Lỗi thật của provider (key sai, request sai) được trả về nguyên vẹn, không phạt endpoint.
- Tài khoản mạng xã hội **không** đổi sang IP khác khi endpoint của nó tạm lỗi: lần thử đó thất bại và
  job tự thử lại sau.
- Scope đã đặt proxy/relay mà không còn endpoint nào dùng được thì **báo lỗi**, không tự đi thẳng. Tài
  khoản ghim một proxy đã tắt/xoá cũng báo lỗi cho tới khi bạn ghim lại hoặc bỏ ghim.
- **Kiểm tra TCP trước khi chọn**: trước khi dùng một proxy, app mở thử TCP (~2s) tới host:port của nó.
  Cổng chết thì endpoint đó tạm nghỉ và app chọn cái kế (failover LAN ↔ Tailscale), không phải chờ
  timeout của request. Kiểm tra này chỉ chứng minh cổng mở. Lỗi xác thực (407) vẫn xử lý ở tầng request
  như trên.
- **Job được hoãn, không thất bại**: khi không vào được proxy nào (lỗi `EgressUnavailable`, kể cả proxy
  rớt giữa chừng một lần upload), job bị hoãn 2 phút, gấp đôi mỗi lần, tối đa 10 phút. Lần hoãn
  **không** tính vào số lần thử (`attempt_count` được hoàn lại), và job chờ vô hạn tới khi server sống
  lại. Muốn dừng thì bấm Huỷ. Hàng đợi hiện lý do "Server egress không vào được – hoãn tới …".
- Kernel Kaggle **đã push** thì không bị hoãn: handler ở lại, đợi rồi poll/tải output lại, để không push
  kernel thứ hai trong khi kernel cũ còn chạy.
- Upload YouTube rớt giữa chừng vì proxy: job được hoãn và lần sau **upload lại từ đầu** (chưa resume
  qua các lần chạy job).

Kiểm tra một endpoint sẽ gọi `api.ipify.org` qua endpoint và hiện IP thoát. Bật lại một endpoint đang tắt
sẽ xoá bộ đếm lỗi của nó.

### Bảo mật

- Mật khẩu proxy, secret của relay và token tài khoản lưu trong database cục bộ (`data/app.db`) và không
  bao giờ được trả lại giao diện: URL proxy hiển thị dạng `http://user:***@host:port`. Khi sửa endpoint,
  để trống URL/secret nghĩa là giữ giá trị cũ.
- Bản xuất ở trang **Dữ liệu** có chứa các bảng `social_account` và `egress_endpoint` — coi file xuất như
  một file bí mật.
- API key của provider AI đi qua relay trong header. Chỉ dùng relay do chính bạn deploy.

### Lưu ý về điều khoản dịch vụ

Dùng proxy/relay để gọi một provider từ vùng họ không phục vụ, hoặc vận hành nhiều tài khoản mạng xã hội
qua nhiều IP, có thể vi phạm điều khoản của provider/nền tảng đó và dẫn tới khoá key hoặc tài khoản. App
giữ nguyên nhịp gọi và giới hạn quota sẵn có, và mặc định cố định một IP cho mỗi tài khoản; việc dùng tính
năng này có phù hợp với điều khoản bạn đã chấp nhận hay không là trách nhiệm của bạn.
