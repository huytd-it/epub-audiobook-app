## Context

Hiện tại `app/epub_parser.py` đọc spine theo thứ tự, với mỗi spine document dùng `_split_by_headings()` để tách theo `<h1>/<h2>/<h3>` rồi lọc những phần dưới `_MIN_CHAPTER_CHARS = 50` ký tự. Kết quả trả về danh sách `ParsedChapter` được lưu thẳng vào DB (xem `app/repository.create_book`, dòng 56-63) trước khi chia thành patch. Người dùng hiện không có UI để xem text gốc của từng chapter - chỉ thấy danh sách patch với range chapter và trạng thái. Sau khi upload, nếu parse nhầm (ví dụ chapter 0 là trang mục lục, hoặc một số heading bị tách sai), user phải xóa sách và upload lại.

## Goals / Non-Goals

**Goals:**
- Tự động phát hiện và bỏ qua chapter đầu tiên nếu đó là mục lục (TOC).
- Cung cấp API + UI cho phép xem text gốc của nhiều chapter cùng lúc để user verify chất lượng parse.
- Không thay đổi schema DB, không ảnh hưởng sách đã upload.

**Non-Goals:**
- Không backfill sách cũ (chỉ áp dụng khi upload mới).
- Không cố detect TOC ở vị trí khác ngoài đầu sách.
- Không xây editor cho chapter (chỉ read-only preview).
- Không thêm dependency mới (chỉ dùng stdlib + deps hiện có).

## Decisions

### 1. Heuristic cho TOC detection

**Lựa chọn:** Dùng kết hợp 2 tín hiệu đơn giản trên chapter đầu tiên:
- **Mean line length thấp**: chia text thành các dòng, lấy mean(line.strip()). Nếu < 40 ký tự/dòng → nghi ngờ TOC.
- **Tỉ lệ non-whitespace ngắn**: nếu > 70% số dòng non-blank có độ dài < 50 ký tự → nghi ngờ TOC.

**Lý do:** TOC thường là danh sách ngắn gọn (title chapter + số trang hoặc link), khác hẳt với prose có paragraph dài. Hai tín hiệu này cheap, deterministic, không cần thêm dependency.

**Alternatives considered:**
- *Đếm số `<a href>` internal links*: cũng hiệu quả nhưng phụ thuộc cách mỗi EPUB encode link, dễ miss khi TOC dùng text thuần.
- *Dùng thư viện NLP để phát hiện*: quá nặng, không phù hợp project.
- *Hardcode "chapter 0" → skip*: sai cho nhiều sách, quá brittle.

### 2. Vị trí áp dụng filter

**Lựa chọn:** Filter trong `parse_epub()` (pure function), ngay sau khi build `chapters: list[ParsedChapter]` nhưng trước khi return. Caller (`routes/books.py`) không cần biết.

**Lý do:** Tận dụng cấu trúc dataclass `ParsedChapter` đã có, không leak logic TOC vào route. Mặc định bật; cho phép tắt qua `parse_epub(path, skip_toc=False)` để giữ backward-compat cho tests.

**Trade-off:** Caller không có quyền "review" chapter bị skip. Giảm thiểu bằng log INFO + (sau này) hiển thị thông báo trên UI upload page.

### 3. API design cho batch preview

**Lựa chọn:** `GET /books/{book_id}/chapters/preview?ids=0,2,4&preview_chars=500` trả về `application/json`. Truncated text (theo `preview_chars`); UI render full text nhưng fetch từ endpoint khác (xem decision 4).

**Lý do:** 
- GET dễ cache, dễ share URL, không cần CSRF.
- Query string `ids` thân thiện với bookmark/curl.
- `preview_chars` mặc định 500 đủ để user scan nhanh; tăng được nếu cần.

**Alternative considered:** `POST /books/{id}/preview` với JSON body. Bỏ vì GET đơn giản hơn cho use-case này (chỉ đọc).

### 4. UI: trang riêng hay inline?

**Lựa chọn:** Trang riêng `GET /books/{book_id}/chapters/preview-ui` render bằng server-side Jinja. Form submit đến chính URL đó với query `?ids=...&range_start=...&range_end=...`. Không cần thêm JS framework.

**Lý do:** Phù hợp với phong cách project hiện tại (server-rendered Jinja, meta refresh cho progress, form submit). Thêm JS chỉ khi cần (ví dụ: chọn checkbox dồn lại thành range trước khi submit - làm sau nếu cần).

**Alternative considered:** Inline section trong `book_detail.html` với anchor. Bỏ vì preview text có thể rất dài, làm page detail nặng và khó navigate.

### 5. Storage và lưu lượng text

**Lựa chọn:** API trả truncated text (`preview_chars`); UI trang riêng fetch FULL text qua endpoint khác (`GET /books/{book_id}/chapters/{chapter_index}/text`) trả raw text. UI chỉ render full text cho chapter đã chọn (thường < 20).

**Lý do:** Tách concerns: API `preview` cho danh sách scan nhanh, endpoint per-chapter cho xem chi tiết. Tránh phải truyền full text của cả cuốn sách qua 1 request (có thể vài MB).

## Risks / Trade-offs

- **False positive** (real chapter bị nhận nhầm là TOC) → Mitigation: guard "nếu filter làm rỗng list thì trả về list gốc"; log số chapter bị skip để user đối chiếu.
- **False negative** (TOC không bị detect, ví dụ TOC dài với description ngắn) → Mitigation: cung cấp batch preview UI giúp user thấy ngay và quyết định re-upload với flag tắt TOC filter.
- **Long preview page** (cuốn sách 500 chapter, mỗi chapter 5k chars) → Mitigation: cap `preview_chars` cho danh sách scan, paginate/chunk riêng cho full text.
- **No backfill** cho sách cũ → Mitigation: documented trong proposal; có thể thêm `scripts/backfill_toc_filter.py` sau nếu user cần.

## Migration Plan

1. Implement TOC filter trong `epub_parser.py` (off by default nội bộ, on by default qua wrapper).
2. Thêm route + template cho preview UI.
3. Thêm nút "Preview chapters" trong `book_detail.html`.
4. Smoke test với 1 EPUB có TOC và 1 EPUB không có TOC.
5. Không cần migration DB. Sách cũ giữ nguyên; nếu user muốn áp dụng, xóa + upload lại.
6. Rollback: revert các thay đổi trong `epub_parser.py` + xóa route/template mới. Không ảnh hưởng data.

## Open Questions

- Có nên lưu `skipped_toc_chapter` vào bảng `book` để hiển thị "1 chapter skipped" trên UI book detail? → *Defer*: bắt đầu với log-only, thêm field DB nếu user yêu cầu.
- Có nên cho user chọn chapter để **xóa** (mark as excluded) khỏi patch generation? → *Defer*: tách thành change riêng nếu cần.
