# Chi tiết cơ chế phát hiện lỗi nội dung ebook

## 1. Mục đích và phạm vi

Tài liệu mô tả cách hệ thống phát hiện, hiển thị và hỗ trợ xử lý lỗi nội dung EPUB từ trang [`BookDetail.tsx`](../frontend/src/pages/BookDetail.tsx). Đối tượng sử dụng gồm người biên tập sách, người vận hành TTS và lập trình viên bảo trì tính năng kiểm tra.

**Trọng tâm là phát hiện lỗi:** điều kiện kích hoạt, regex, đơn vị đếm, ví dụ khớp/không khớp và các trường hợp phát hiện nhầm/bỏ sót. Đọc mục 6 để hiểu kiểm tra văn bản, mục 7 để hiểu nhận diện số/tiêu đề, mục 8 để hiểu các heuristic biên tập và mục 9 để hiểu kiểm tra patch/chunk. Các mục thao tác giao diện chỉ là tài liệu tham khảo bổ trợ.

Nội dung được đối chiếu với mã nguồn trong repository, không chỉ với giao diện trang cha. Đây là tài liệu về **cơ chế phân tích**, không phải báo cáo lỗi của một cuốn sách cụ thể: chưa có EPUB hoặc kết quả API của sách cụ thể được cung cấp để kiểm chứng.

Phạm vi:

- Chất lượng văn bản, ký tự, mã hóa và cấu trúc đoạn.
- Tiêu đề, số chương, thứ tự và nội dung trùng.
- Khoảng chương của patch và chunk thực tế gửi TTS.
- Cách sửa, chuẩn hóa, phân tích lại và kiểm tra trước sản xuất.
- Giới hạn hiện tại và đề xuất cải thiện, được phân biệt với chức năng đã có.

Không coi lỗi mạng, dịch vụ TTS, FFmpeg hay YouTube là lỗi nội dung ebook nếu chưa có bằng chứng liên hệ với văn bản đầu vào.

## 2. Các khái niệm cần phân biệt

| Khái niệm | Ý nghĩa | Ví dụ |
| --- | --- | --- |
| EPUB nguồn | Tệp sách được lưu để nhập hoặc nạp lại chương | `truyen.epub` |
| Chương | Bản ghi tiêu đề và văn bản đã được parser trích xuất | `Chương 12: Cuộc gặp` |
| `chapter_index` | Vị trí chương trong dữ liệu, bắt đầu từ 0 | Chương thứ 3 có index 2 |
| `chapter_no` | Số chương nhận diện từ tiêu đề, có thể không có | Tiêu đề `Chương 12` có số 12 |
| Patch | Nhóm chương phục vụ sản xuất audio/video | Vị trí 1–10 của mục lục |
| Chunk | Đoạn văn bản thực tế được chia để gửi TTS | Một đoạn trong patch |
| `clean_text` | Văn bản riêng của patch đã lưu qua Text Studio | Có thể khác nội dung chương |
| Issue | Kết quả kiểm tra theo mã lỗi | `mojibake`, `empty` |
| Span | Vị trí văn bản có thể tô sáng | `start`, `length`, `code` |

**Vị trí chương không đồng nghĩa với số chương.** Sách có lời tựa, phần mở đầu hoặc bắt đầu ở chương 101 vẫn có vị trí đầu tiên là 1 trên giao diện, index là 0 trong API.

## 3. Luồng phân tích tổng thể

```text
EPUB nguồn
  → parser đọc theo spine, trích tiêu đề/văn bản và lọc một số nội dung
  → lưu danh sách chương
  → phân tích toàn bộ chương
      ├─ lỗi văn bản và định dạng tiêu đề
      ├─ thiếu/trùng/sai thứ tự số chương
      └─ nội dung trùng giữa các chương
  → mở chương: phân tích chi tiết + tô sáng vị trí nghi vấn
  → sửa bản nháp → phân tích lại → lưu
  → làm mới báo cáo chương và hồ sơ sách
  → kiểm tra khoảng chương của patch
  → dựng chunk plan sau chuẩn hóa/thay thế hoặc từ clean_text
  → kiểm tra chunk → TTS → audio → video → YouTube
```

Ba lớp kiểm tra trả lời các câu hỏi khác nhau:

1. **Chương:** văn bản và tiêu đề đã lưu có bất thường không?
2. **Cấu trúc:** sách có thiếu, trùng hoặc trượt khoảng chương không?
3. **Chunk plan:** nội dung thực tế gửi TTS có rỗng, không đọc được hoặc quá dài không?

Phải kiểm tra cả ba khi chuẩn bị sản xuất. Văn bản nguồn có cảnh báo không nhất thiết khiến TTS thất bại vì bước chuẩn hóa có thể xử lý nó; ngược lại, chương có vẻ sạch vẫn có thể bị quy tắc thay thế làm rỗng.

## 4. Đọc kết quả trên BookDetail

### 4.1. Các chỉ báo đầu trang

| Chỉ báo | Nguồn dữ liệu | Cách hiểu |
| --- | --- | --- |
| “Chương không liên tục” | `report.numbering.is_continuous` | Có số thiếu, số trùng hoặc số giảm theo thứ tự đọc |
| “N tiêu đề sai định dạng” | Tổng `fixable + no_name + unknown` | Các tiêu đề không được nhận diện là chuẩn |
| Ô “Lỗi” | Số patch có `status === "failed"` | **Lỗi xử lý patch**, không phải số lỗi nội dung |
| “Lỗi xử lý gần nhất” | `data.last_error` | Thông báo của quá trình xử lý; cần đọc chi tiết để xác định nguyên nhân |
| “Audio xong” | Số patch có `status === "done"` | Audio đã hoàn thành, không chứng minh nội dung đầy đủ/đúng |

Nhấn cảnh báo số chương hoặc tiêu đề chuyển sang tab **Mục lục**.

### 4.2. Tab Mục lục

- Nút **Phân tích nội dung** tải lại báo cáo toàn bộ chương.
- Bộ lọc gồm tất cả, lỗi, cảnh báo, tiêu đề và chương bị loại trừ.
- Tìm kiếm hiện dựa trên tiêu đề hoặc vị trí chương (`chapter_index + 1`), không tìm toàn văn.
- Nhấn tiêu đề để mở chương, xem báo cáo và sửa nội dung.
- Bộ lọc lỗi/cảnh báo dùng **mức nghiêm trọng tổng hợp của chương**. Chương vừa có error vừa có warning nằm trong nhóm lỗi, không nằm trong nhóm warning.
- Chương bị loại trừ vẫn có thể xuất hiện trong thống kê lỗi nội dung; không nên hiểu tổng lỗi là chỉ đếm chương đang được đọc.

### 4.3. Hộp thoại chương

- Xem hoặc sửa tiêu đề, văn bản và trạng thái bỏ qua khi tạo audio.
- Tô sáng lỗi đỏ, cảnh báo vàng và thông tin xanh.
- Chọn một nhóm span để nhấn mạnh loại vấn đề cần xem.
- Ký tự vô hình/điều khiển được biểu diễn bằng dấu `·` để có thể nhìn thấy.
- **Phân tích lại** kiểm tra bản nháp; **không lưu** nội dung.
- **Lưu** ghi nội dung, cập nhật metadata và trả lại báo cáo mới.

Sau khi lưu chương hoặc áp dụng chuẩn hóa tiêu đề, `BookDetail.onChapterSaved` tải lại báo cáo chương trước, rồi làm mới dữ liệu chính.

## 5. Mức độ nghiêm trọng và cách đếm

| Mức | Ý nghĩa | Hành động khuyến nghị |
| --- | --- | --- |
| `error` | Rủi ro lớn đối với TTS hoặc tính toàn vẹn đầu vào | Xử lý hoặc xác minh trước khi sản xuất |
| `warning` | Nội dung có thể vẫn đọc được nhưng cần biên tập | Đọc ngữ cảnh, sửa hoặc chấp nhận có lý do |
| `info` | Dấu hiệu cần chú ý, không tự động là lỗi | Rà soát nếu ảnh hưởng chất lượng đọc |
| `ok` | Không có issue trong bộ kiểm tra tương ứng | Tiếp tục kiểm tra cấu trúc/chunk và nghe thử |

Mức chương là mức cao nhất trong `report.issues`. `is_valid` chỉ yêu cầu không có `error`; warning không làm chương invalid. Các gợi ý mềm trong span không nhất thiết được thêm vào `report.issues`, vì vậy chương có thể mang mức `ok` nhưng vẫn có highlight viết tắt hoặc chính tả.

Ba loại số đếm không tương đương:

- `summary.issue_totals[code]`: số issue theo mã qua các báo cáo chương; không cộng `Issue.count`.
- `Issue.count`: số ký tự hoặc lần xuất hiện tùy quy tắc. Với lỗi không truyền count riêng, mặc định là 1.
- `span_totals[code]`: số vùng highlight sau gộp, loại chồng lấn và giới hạn kết quả.

Ví dụ: 5 ký tự CJK liền nhau trong một chương có thể tạo 1 issue với count 5, 1 span và tăng issue total thêm 1.

## 6. Danh mục lỗi văn bản từng chương

Các quy tắc dưới đây nằm trong `app/validation.py`, áp dụng trên văn bản chương đã trích xuất.

| Mã | Mức | Điều kiện hiện tại | Tác động và cách xử lý |
| --- | --- | --- | --- |
| `empty` | error | Văn bản rỗng sau `strip()` | Không có nội dung để đọc. Đối chiếu EPUB, khôi phục văn bản hoặc loại trừ nếu là trang phụ |
| `unspeakable` | error | Không rỗng nhưng không có chữ/số theo regex Unicode | Ví dụ `*** --- !!!`; TTS có thể trả audio rỗng. Xóa dòng trang trí, bổ sung nội dung hoặc loại trừ |
| `too_short` | warning | Có chữ/số nhưng dưới 120 ký tự sau trim | Có thể là bìa, chú thích hoặc chương thật rất ngắn. Đọc ngữ cảnh trước khi loại trừ |
| `cjk_residue` | error | Có ký tự trong các dải chữ Hán mà regex hỗ trợ | Có thể là phần chưa dịch. Đối chiếu bản gốc, dịch/biên tập đúng ý; không xóa mù quáng |
| `mojibake` | error | Có ký tự thay thế `�` (U+FFFD) | Ký tự đã bị mất khi giải mã. Tìm bản nguồn đúng; không thể suy ra chắc chắn chữ gốc bằng cách xóa `�` |
| `control_chars` | error | Có ký tự Unicode loại `Cc`, trừ newline/tab/carriage return | Có thể gây bất thường xử lý. Xóa ký tự điều khiển, giữ ngắt đoạn hợp lệ |
| `html_residue` | warning | Khớp regex thẻ HTML hoặc entity | TTS có thể đọc mã thô. Bóc markup hoặc giải mã entity, giữ lại nội dung hiển thị |
| `zero_width` | warning | Có ký tự trong dải U+200B–U+200F, U+2028–U+2029 hoặc U+FEFF | Kiểm tra khoảng trắng/ngắt dòng và loại ký tự không cần thiết |
| `url` | warning | Có `http://`, `https://` hoặc `www.` | TTS có thể đọc địa chỉ dài. Xóa link quảng cáo hoặc diễn đạt thành lời nếu link có ý nghĩa |
| `unsplittable_paragraph` | error | Đoạn tách bằng `\n\n` dài hơn `int(max_chars × 1.5)` và không có `. ! ? …` | Đoạn thiếu ranh giới câu dễ sinh chunk lớn. Biên tập dấu câu và ngắt đoạn đúng ý |
| `duplicate_text` | warning | Nhiều chương có cùng hash của văn bản đã trim | Nghi lặp nội dung. So sánh toàn văn và tiêu đề trước khi loại trừ hoặc khôi phục chương thiếu |

### 6.1. Đầu vào và thứ tự đánh giá

`validate_chapter_text` bắt đầu bằng:

```python
stripped = (text or "").strip()
```

Không có bước NFC/NFD, sửa chính tả, giải mã HTML hay chuẩn hóa nội dung trong hàm này. Nó kiểm tra chuỗi được truyền vào, không tự sửa chuỗi đó.

Nhóm đầu tiên dùng `if/elif/elif`: **empty → unspeakable → too_short**, nên ba mã này loại trừ nhau trong một lần kiểm tra. Các nhóm CJK, mã hóa, control, HTML, ký tự vô hình, URL, đoạn dài và tiêu đề được xét tiếp độc lập.

Ví dụ `"@@ 中文 �"` có thể đồng thời có too_short, cjk_residue và mojibake; error thắng warning khi tổng hợp mức chương. Không dừng kiểm tra ngay sau lỗi đầu tiên.

**Hai hệ quy chiếu:** issue văn bản được tính trên chuỗi đã trim, còn span được tìm lại trên văn bản thô để giữ vị trí hiển thị. Ký tự thuộc whitespace bị trim ở mép có thể biến mất khỏi bộ đếm issue nhưng vẫn xuất hiện trong span.

### 6.2. `empty` — không có nội dung sau trim

Điều kiện: `not stripped`. `None`, chuỗi rỗng hoặc chỉ có whitespace Python đều kích hoạt error; count mặc định 1. Không có span vì không tồn tại vùng nội dung cụ thể cần đánh dấu.

| Đầu vào | Kết quả |
| --- | --- |
| `""`, `" \n\t "` | empty |
| `"\u200b"` — zero-width space | Không empty; có thể unspeakable và zero_width |
| `"A"` | Không empty; có thể too_short |

Không suy ra nguyên nhân từ mã này: có thể chương nguồn rỗng, parser làm mất nội dung, hoặc thao tác biên tập xóa hết văn bản.

### 6.3. `unspeakable` — không có ký tự từ để đọc

Regex hiện tại:

```python
re.compile(r"[^\W_]", re.UNICODE)
```

Về kỹ thuật đây là **ký tự thuộc `\w` trừ `_`**, không phải bộ nhận diện tiếng Việt. Một ký tự khớp là đủ để chương vượt qua kiểm tra unspeakable.

| Đầu vào | Kết quả của riêng bộ kiểm tra này |
| --- | --- |
| `"*** --- !!!"`, `"___"`, `"😀"` | unspeakable |
| `"123"`, `"A"`, `"中文"` | Không unspeakable |
| Một chuỗi dài chứa toàn ký hiệu nhưng có thêm `1` | Không unspeakable |

**Bỏ sót:** có chữ/số không bảo đảm có câu đọc được hay nội dung có ý nghĩa. **Phát hiện nhầm theo ngữ cảnh:** chương chỉ chứa emoji/ký hiệu có thể là dụng ý của tác giả, dù không phù hợp đầu vào TTS hiện tại.

### 6.4. `too_short` — cảnh báo độ dài

Điều kiện: `len(stripped) < 120`, chỉ xét sau khi đã vượt empty và unspeakable. `len` ở backend là số code point Python, không phải số byte, từ hoặc ký tự hiển thị theo grapheme.

- 119 ký tự có chữ/số: warning.
- 120 ký tự: không có too_short.
- 119 dấu `!`: unspeakable, không có too_short.
- Count của issue vẫn là 1; số ký tự nằm trong thông báo và `char_count`.

**False positive:** thơ, đoạn chuyển cảnh, lời đề từ ngắn hợp lệ. **False negative:** văn bản rác dài trên ngưỡng hoặc thiếu nửa chương nhưng vẫn trên 120 ký tự.

### 6.5. `cjk_residue` — chữ Hán còn sót

Regex dạng escape tương đương mã nguồn:

```python
r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]"
```

Mỗi ký tự khớp tăng count 1. Span gộp ký tự liền nhau thành vùng:

```text
A中文B漢
 → issue count = 3
 → 2 span: 中文 và 漢
```

- Bắt được chữ Hán cơ bản, Extension A và dải Compatibility Ideographs.
- Không bao phủ toàn bộ chữ Hán mở rộng ngoài BMP, Kana Nhật và Hangul Hàn.
- Không phân biệt phần chưa dịch với tên riêng, trích dẫn ngoại ngữ hoặc ví dụ có chủ ý.
- Parser/normalizer có thể đã loại các ký tự này trước đó, nên không có issue không chứng minh nguồn EPUB không có CJK.

### 6.6. `mojibake` — ký tự thay thế U+FFFD

Mẫu tìm chỉ là `�`. Count là số lần xuất hiện; các ký tự liền nhau được gộp span.

| Văn bản | Phát hiện |
| --- | --- |
| `"Tôi �ọc sách"` | Có mojibake |
| `"���"` | count 3, một vùng span |
| `"TÃ´i"` | Không được nhận diện là mojibake chỉ dựa vào regex U+FFFD |

Mã này chỉ chứng minh có ký tự thay thế, không xác định encoding gốc hay tự khôi phục chữ đã mất. Nếu chính tác phẩm viết ký hiệu `�` để minh họa lỗi mã hóa, vẫn bị đánh dấu.

### 6.7. `control_chars` — ký tự điều khiển

Điều kiện cho từng ký tự:

```python
unicodedata.category(ch) == "Cc" and ch not in "\n\t\r"
```

- Bắt NUL U+0000, BEL U+0007 và các ký tự điều khiển khác thuộc Cc.
- Cho phép LF, TAB, CR.
- Không bao phủ mọi ký tự Unicode vô hình: nhiều ký tự thuộc Cf hoặc nhóm khác.
- Count là số ký tự; span gộp các control liền nhau.

Do issue dùng `.strip()`, một số control được Python coi là whitespace ở đầu/cuối có thể bị bỏ trước khi đếm; span thô vẫn có thể tìm thấy chúng. NUL không tự được loại bởi trim thông thường.

### 6.8. `html_residue` — thẻ và entity còn nguyên

Hai regex độc lập:

```python
tag    = r"<[a-zA-Z/][^>]{0,80}>"
entity = r"&(?:[a-zA-Z]{2,10}|#\d{2,5});"
```

Issue count = số match thẻ + số match entity, không phải số ký tự HTML. Mỗi match tạo một span trước bước loại chồng lấn.

| Ví dụ | Kết quả |
| --- | --- |
| `<b>Xin chào</b>` | 2 match thẻ |
| `&nbsp;`, `&amp;`, `&#65;` | Match entity |
| `&#x41;` | Không match entity hex |
| `&a;` | Không match vì tên dưới 2 chữ |
| `<!-- ghi chú -->`, `<!DOCTYPE html>` | Không match mẫu thẻ vì ký tự đầu sau `<` không hợp lệ |
| `<TênNhânVật>` | Có thể match dù không phải HTML thực |

Regex không phân tích DOM hoặc xác minh entity thực sự tồn tại. Entity tên bất kỳ 2–10 chữ vẫn có thể bị đánh dấu; thẻ quá dài so với mẫu có thể bị bỏ sót. Nội dung văn học dùng ngoặc nhọn cũng có nguy cơ false positive.

### 6.9. `zero_width` — dải ký tự vô hình được chọn

Mẫu hiện tại tương đương:

```python
r"[\u200b-\u200f\u2028\u2029\ufeff]"
```

Bao gồm zero-width space, ZWNJ, ZWJ, dấu hướng văn bản, line/paragraph separator và BOM. Count là số ký tự; ký tự liền nhau được gộp span.

**Không phải tất cả đều là rác:** ZWJ có thể tham gia emoji ghép, ZWNJ có ý nghĩa trong một số ngôn ngữ, dấu hướng có thể phục vụ văn bản hai chiều. Bộ kiểm tra không xét ngữ cảnh này. Không bắt mọi ký tự vô hình như soft hyphen U+00AD hoặc word joiner U+2060.

### 6.10. `url` — mẫu đường dẫn đơn giản

```python
r"https?://\S+|www\.\S+"
```

Regex phân biệt hoa/thường, lấy chuỗi đến whitespace đầu tiên và không xác minh hostname.

- `https://example.com`: khớp.
- `www.example.com`: khớp.
- `HTTPS://example.com`: không khớp nhánh protocol hiện tại.
- `example.com`, email hoặc `ftp://example.com`: không khớp.
- `https://example.com).`: có thể lấy cả dấu `).` vào span.

Count là số match, không phải số website duy nhất. Một link lặp 3 lần được đếm 3. Warning chỉ cho biết có mẫu link, chưa chứng minh link là quảng cáo hay TTS sẽ đọc sai.

### 6.11. `unsplittable_paragraph` — đoạn dài không có dấu kết câu

Thuật toán:

```python
hard_limit = int(max_chars * 1.5)
paragraphs = stripped.split("\n\n")
bad = [p for p in paragraphs if len(p) > hard_limit
       and not re.search(r"[.!?…]", p)]
```

- Count = số đoạn thỏa cả hai điều kiện.
- Thông báo nêu độ dài đoạn lỗi lớn nhất.
- Span đánh dấu toàn đoạn, không chỉ phần vượt ngưỡng.
- Với max_chars 400: 600 không khớp, 601 không dấu kết câu thì khớp.
- Một dấu chấm bất kỳ làm cả đoạn không khớp, kể cả dấu chấm trong số thập phân, viết tắt hay URL.
- Dấu `;`, `:`, `。`, `！`, `？` không được coi là dấu kết câu trong regex này.
- `\n` đơn, hoặc `\r\n\r\n` chưa được chuẩn hóa sang LF, không tương đương delimiter `\n\n`.

**Bỏ sót quan trọng:** đoạn rất dài chứa một dấu chấm ở đầu nhưng phần còn lại không chia được. **False positive:** thơ, danh sách hoặc chuỗi dữ liệu dài không dùng dấu câu thông thường. Đây là cảnh báo nguy cơ theo heuristic; chỉ kiểm tra chunk plan mới đo được độ dài chunk thực.

### 6.12. `duplicate_text` — nhóm chương có hash văn bản giống nhau

Sau khi kiểm tra từng chương, `validate_chapters` nhóm theo:

```python
hash((chapter.text or "").strip())
```

Mọi nhóm có hơn một chương khiến **tất cả chương trong nhóm** nhận duplicate_text, count mặc định 1. Tiêu đề không tham gia khóa; chương bị loại trừ vẫn được xét.

| Cặp văn bản | Có thể gắn duplicate_text? |
| --- | --- |
| `"Xin chào"` và `" Xin chào\n"` | Có |
| Cùng văn bản nhưng khác tiêu đề | Có |
| `"Xin  chào"` và `"Xin chào"` | Không chỉ vì giống nội dung; whitespace bên trong khác |
| Cùng câu nhưng khác hoa/thường, dấu câu hoặc NFC/NFD | Không có chuẩn hóa để gom chúng |
| Hai chương rỗng | Có |

Không kiểm tra lại bằng equality sau hash, nên lý thuyết có rủi ro va chạm hash. Không phát hiện chương gần trùng, lặp vài đoạn hoặc paraphrase. Hai chương cố ý dùng cùng văn bản cũng bị cảnh báo.

Kiểm tra **một chương riêng lẻ** bằng `validate_chapter_text` không phát hiện duplicate_text vì không có các chương khác để so sánh. Muốn xác nhận trùng phải chạy báo cáo toàn chương.

### 6.13. Ví dụ kết quả và cách đọc bằng chứng

Với văn bản thô `"A中文B漢"`, bỏ qua issue tiêu đề để minh họa:

```json
{
  "code": "cjk_residue",
  "severity": "error",
  "count": 3
}
```

Span tương ứng có `start=1, length=2` cho `中文`, và `start=4, length=1` cho `漢` theo chỉ số Python. Đây là hai vùng chứa ba ký tự lỗi; không phải ba chương lỗi.

Một kết luận phát hiện nên luôn ghi: **mã lỗi + nội dung match + vị trí/chương + ngưỡng/config + phạm vi đã phân tích**. Không kết luận “thiếu nội dung”, “sai bản dịch” hoặc “TTS chắc chắn thất bại” chỉ từ một regex.

## 7. Tiêu đề và số chương

### 7.1. Trạng thái tiêu đề

**Thuật toán nhận diện:** thử regex canonical trước; nếu không khớp, thử hai mẫu số chương sau theo thứ tự:

```python
r"^\s*(?:chương|chuong|chapter|hồi|hoi|phần|phan|quyển|quyen|tập|tap)\s*[:\-–—]?\s*(\d{1,5})\b"
# Mẫu trên dùng re.IGNORECASE.
r"^\s*(\d{1,5})\s*(?:[.:\-–—]|$)"
```

Sau prefix khớp, phần còn lại được strip và bỏ các dấu `: - – — .` cùng whitespace ở đầu để lấy tên. Có số nhưng không còn tên → no_name; không tìm được số → unknown; có số và tên nhưng không canonical → fixable.

Canonical dùng mẫu phân biệt hoa/thường:

```python
r"^Chương\s+(\d{1,5})(?::\s+|\s+(?![:\-–—]))(\S.*?)\s*$"
```

**Các biên cần lưu ý:** không nhận số viết bằng chữ hoặc La Mã qua các mẫu này; `12 Tên` không khớp mẫu số đứng riêng vì thiếu delimiter; `Phần 1`, `Quyển 1`, `Tập 1` đều có thể được diễn giải như số chương dù thực tế là cấp phân chia khác. Regex không buộc số phải lớn hơn 0: số 0 hoặc số có zero đầu được chấp nhận và chuyển sang int. `\d` của Python cũng không chỉ giới hạn ASCII `0–9`.

| Trạng thái | Ví dụ | Cách xử lý |
| --- | --- | --- |
| `canonical` | `Chương 12: Cuộc gặp` hoặc `Chương 12 Cuộc gặp` | Được regex hiện tại chấp nhận; không cần sửa tự động |
| `fixable` | `Chapter 12: Cuộc gặp` | Có số và tên, có thể chuyển thành `Chương 12: Cuộc gặp` |
| `no_name` | `Chương 12` | Bổ sung tên thủ công từ nguồn; hệ thống không tự đặt tên |
| `unknown` | `Lời tựa` | Không nhận diện được số; có thể là phần phụ hợp lệ, không tự động chứng minh sai |

Regex chuẩn phân biệt hoa/thường và chấp nhận số từ 1 đến 5 chữ số. Các dạng không được coi là canonical vẫn có thể được bộ tách số/tên nhận diện là fixable.

Các mã issue:

- `no_chapter_number`: warning cho tiêu đề unknown nếu chương chưa bị loại trừ.
- `title_missing_name`: warning cho no_name nếu chương chưa bị loại trừ.
- `title_not_canonical`: warning cho fixable, kèm `suggested_title`; vẫn có thể xuất hiện trên chương bị loại trừ.

**Chuẩn hóa tiêu đề** có bước xem trước. Backend chỉ áp dụng cho chương còn ở trạng thái fixable tại thời điểm ghi và tự tính lại tiêu đề đề xuất. Các chương thiếu tên/không nhận diện số cần sửa thủ công; chuẩn hóa tiêu đề không sửa nội dung thân chương.

### 7.2. Phát hiện thiếu, trùng và sai thứ tự

Hệ thống xét chương có số và không bị loại trừ:

| Hiện tượng | Ví dụ | Cách diễn giải |
| --- | --- | --- |
| Thiếu số | `1, 2, 4` | Thiếu số 3 trong khoảng min–max hiện có |
| Trùng số | `1, 2, 2, 3` | Một số xuất hiện ở nhiều vị trí; kiểm tra cả tiêu đề và nội dung |
| Sai thứ tự | `1, 3, 2, 4` | Số chương giảm giữa hai chương liên tiếp được xét |

`is_continuous` chỉ đúng khi không thiếu, không trùng và không giảm số.

Giới hạn quan trọng:

- Dãy `101, 102, 103` được coi là liên tục; không yêu cầu bắt đầu từ 1.
- Không biết tổng chương chuẩn nên không phát hiện chắc chắn thiếu phần đầu/cuối ngoài min–max.
- Không có chương nhận diện số vẫn có thể cho `is_continuous = true`; cần kiểm tra tiêu đề riêng.
- `unnumbered_count` được tính bằng tổng báo cáo trừ số chương được xét, nên có thể bao gồm chương đã loại trừ dù chúng có số.
- `duplicate_count` là số **giá trị số chương bị trùng**, không phải số bản ghi dư.
- Danh sách số thiếu và vị trí sai thứ tự trả tối đa 200 phần tử; trường count vẫn phản ánh tổng tương ứng.

Không đổi số tiêu đề chỉ để làm mất cảnh báo khi chưa xác minh thứ tự/nội dung thực tế.

## 8. Highlight và gợi ý biên tập mềm

### 8.1. Cơ chế của từng heuristic

**Rác (`junk`):** tìm `@{2,}`, `#{2,}`, `\*{2,}`, `~{3,}` và chuỗi chữ Hán trong U+4E00–U+9FFF/U+3400–U+4DBF. Một `@`, một `*` hoặc hai `~` không khớp nhóm tương ứng. Không phân biệt cú pháp Markdown có chủ ý với rác. Vùng CJK có thể đồng thời khớp junk và cjk_residue; sau dedupe, error CJK được ưu tiên.

**Viết tắt (`abbreviation`):** ba tầng theo thứ tự:

1. Bảng viết tắt biết trước, sắp mẫu dài trước mẫu ngắn; không phân biệt hoa/thường. Ví dụ TP.HCM, UBND, HĐND, THPT, TNHH, NXB, GS., TS., v.v.
2. Acronym dạng chấm: `(?<![\w.])(?:[A-ZĐ]\.){2,}[A-ZĐ]?(?![\w])`.
3. Acronym hoa 2–6 chữ ASCII: `(?<![\w.])[A-Z]{2,6}(?![\w])`.

Mỗi tầng chiếm vùng match; tầng sau không phát sinh cảnh báo nếu vùng giao nhau đã được chiếm. Acronym chung chỉ gồm chữ La Mã `IVXLCDM` sau bỏ dấu chấm thì được bỏ qua, ví dụ `XIV`.

Bảng biết trước dùng biên trái `(?<![\w.])` và biên phải `(?![\w])`. Vì vậy `Q.1` có thể không được cảnh báo như viết tắt: sau `Q.` là chữ số thuộc `\w`. **Regex mở rộng viết tắt trong normalization khác regex cảnh báo** và có xử lý ngữ cảnh bổ sung; không thấy warning không có nghĩa không bị normalization thay đổi. Các từ hoa hợp lệ như `ANH` cũng có thể bị nhận nhầm là acronym.

**Chính tả nghi vấn (`spell_vi`):** token được lấy bằng `[a-zA-ZÀ-ỹ]+(?:\.[a-zA-ZÀ-ỹ]+)*`, bỏ token dưới 3 ký tự. Một token có bất kỳ mẫu sau sẽ tạo một cảnh báo cho **toàn token**, không chỉ substring:

```python
r"([aeiou])\1{2,}"            # một nguyên âm ASCII lặp ít nhất 3 lần
r"[qx][bcdfghjklmnpqrstvwxz]" # q hoặc x đứng trước chữ thuộc nhóm phụ âm
r"[bcdfghjklmnpqrstvwxz]{4,}" # ít nhất 4 chữ trong nhóm liên tiếp
```

Các mẫu dùng IGNORECASE. `aaa`, `qx`, `bcdf` là substring đáng nghi; nhưng token `qx` riêng bị bỏ vì dài dưới 3, trong khi `qxa` được xét. Không tra từ điển tiếng Việt, không kiểm tra ngữ pháp, không gợi ý từ sửa (`suggestion` rỗng). Tên riêng/tiếng nước ngoài và kéo dài âm cảm thán có thể bị báo nhầm; lỗi phổ biến như nhầm s/x hay dấu thanh có thể bị bỏ sót.

**Nhãn hiệu ứng (`effect_marker`):** regex nhận nhãn ngoặc vuông thuộc các mẫu cho sẵn, ví dụ `[tiếng cười]`, `[âm thanh ...]`, `[sound effect: ...]`, `[cry]`, `[scream]`, `[laugh]`, `[whisper]`. Không phải mọi `[...]` đều là hiệu ứng. Đây là nhận diện cú pháp/keyword, không xác minh có tệp hiệu ứng tương ứng; suggestion hiện giữ nguyên match.

**Mô tả âm thanh (`sound_desc`):** danh sách regex keyword tiếng Việt/Anh, gồm khóc, hét, rên, cười, thì thầm, cảm thán, va chạm, tiếng động vật/nature và nói lắp dạng lặp từ với dấu phẩy/dấu chấm lửng. Ví dụ `khóc nức nở`, `hét lên`, `thở dài`, `mỉm cười`, `rầm`, `gâu gâu`, `trời ơi`, `scream`, `bang`.

Các match cùng `(start, end)` được khử trùng trong nhóm sound_desc; match giao nhau nhưng khác biên chưa bị loại ở bước này. Không hiểu ngữ nghĩa: `cạch`, `pop` hoặc `mỉm cười` có thể là văn bản bình thường, không phải lỗi đọc. Việc dùng `\s*` trong nhiều mẫu cho phép khớp cả khi thiếu khoảng trắng hoặc có newline giữa từ.

### 8.2. Mức độ và phạm vi trả kết quả

Ngoài các lỗi cứng có vị trí trong thân văn bản, `analyze_chapter_spans` bổ sung kết quả từ `app/text_analysis.py`:

| Mã span | Mức | Ví dụ/phạm vi | Khuyến nghị |
| --- | --- | --- | --- |
| `junk` | warning | `@@`, `##`, `**`, `~~~` và mẫu chữ Hán | Kiểm tra dòng trang trí/rác; giữ lại ký hiệu có ý nghĩa |
| `abbreviation` | warning | Viết tắt trong bảng có sẵn, acronym hoa 2–6 chữ hoặc dạng chấm | Chọn cách đọc đúng ngữ cảnh; kiểm tra preview sau mở rộng |
| `spell_vi` | info | Mẫu chữ lặp hoặc tổ hợp chữ đáng nghi | Chỉ là heuristic, không phải từ điển chính tả đầy đủ |
| `effect_marker` | info | Nhãn như `[laugh]`, `[tiếng cười]` | Quyết định đọc thành lời hay dùng cơ chế hiệu ứng |
| `sound_desc` | info | Cụm mô tả âm thanh khớp mẫu tiếng Việt/Anh | Không tự động xóa văn phong kể chuyện |

Tô sáng không có nghĩa hệ thống đã sửa văn bản. Tính năng kiểm tra chính tả tiếng Anh hiện không phát sinh cảnh báo trong hàm tương ứng.

Giới hạn highlight:

1. Các ký tự liền nhau của một số lỗi được gộp thành một vùng.
2. Khi chồng lấn, ưu tiên mức nghiêm trọng cao hơn; cùng mức ưu tiên vùng dài hơn. Span bị chồng lấn có thể bị bỏ toàn bộ, không tách phần còn lại.
3. API mặc định trả tối đa 800 span sau loại chồng lấn.
4. Thành phần hiển thị chỉ render tối đa 120.000 đơn vị chuỗi JavaScript và thông báo nếu cắt bớt.
5. Lỗi toàn chương như empty, tiêu đề sai hay trùng nội dung không nhất thiết có span trong thân văn bản.

Do đó “không có highlight” không đồng nghĩa “không có issue”, và số vùng thấy trên màn hình không phải tổng mọi bất thường.

## 9. Kiểm tra patch và chunk thực tế

### 9.1. Khoảng chương của patch

| Mã | Mức | Ý nghĩa |
| --- | --- | --- |
| `range_inverted` | error | Chỉ số bắt đầu lớn hơn chỉ số kết thúc |
| `range_out_of_bounds` | error | Có chỉ số trong khoảng không tồn tại |
| `range_gap` | error | Hở chương giữa hai patch liên tiếp |
| `range_overlap` | error | Chồng lấn khoảng của patch trước |
| `range_not_from_start` | warning | Patch đầu không bắt đầu tại index 0 |
| `range_size_drift` | warning | Patch không phải cuối có kích thước khác kích thước phổ biến của các patch trước cuối |
| `chapter_no_desync` | error | Số chương neo đã lưu khác min/max số chương tại khoảng hiện tại |
| `chapter_no_gap` | warning | Có số chương thiếu bên trong một patch |
| `chapter_no_missing` | warning | Có chương tại khoảng không nhận diện được số |

Kiểm tra khoảng dựa trên dữ liệu chương/patch, không phải độ giống nhau của văn bản. Khoảng min/max số đúng không bảo đảm thứ tự bên trong đúng; vẫn cần báo cáo số chương toàn sách. Hàm kiểm tra khoảng cũng không có mã riêng để phát hiện phần đuôi sách chưa được patch cuối bao phủ.

Kích thước patch khác nhau có thể là chủ ý. Không sửa hàng loạt chỉ vì `range_size_drift` nếu cách phân tập hiện tại đã được xác nhận.

### 9.2. Chunk plan

| Mã | Mức | Ý nghĩa và xử lý |
| --- | --- | --- |
| `no_chunks` | error | Không sinh được chunk: kiểm tra chương loại trừ, văn bản rỗng và quy tắc thay thế |
| `empty_chunks` | error | Có chunk rỗng sau xử lý: kiểm tra đầu vào và quy tắc chuẩn hóa |
| `unspeakable_chunks` | error | Có chunk chỉ chứa ký hiệu: loại phần không đọc được hoặc biên tập lại |
| `oversized_chunks` | error | Chunk dài hơn `int(max_chars × 1.5)`: kiểm tra câu dài, dấu câu và giới hạn model |
| `invalid_chapters` | error | Patch bao phủ chương chưa loại trừ có mức error: sửa chương liên quan |

Chunk builder hiện lọc nhiều đoạn không có chữ/số trước khi tạo plan, nên một số tình huống rỗng chỉ còn thể hiện qua `no_chunks` thay vì `empty_chunks`.

Nếu patch có `clean_text`, chunk plan sử dụng văn bản riêng đó. Sửa chương không bảo đảm thay đổi văn bản TTS của patch này. Báo cáo `invalid_chapters` vẫn có thể cảnh báo theo chương nguồn dù plan dùng override; cần đối chiếu cả hai nguồn thay vì coi chúng là một.

## 10. Chuẩn hóa và tác động của việc sửa

### 10.1. Các tùy chọn hiển thị trên BookDetail

| Tùy chọn | Vai trò |
| --- | --- |
| `numbers` | Chuyển số/ngày/giờ/tiền tệ/đơn vị sang cách đọc |
| `junk` | Làm sạch token rác theo pipeline |
| `spellcheck` | Xử lý dấu chấm chen trong từ tiếng Việt; không phải tự sửa mọi lỗi chính tả |
| `dictionary` | Bật xử lý qua normalizer/từ điển |
| `transliteration` | Bật xử lý phiên âm qua normalizer |
| `abbreviations` | Mở rộng viết tắt trước khi xử lý số |
| `breaks` | Chèn cue ngắt nghỉ ở cuối pipeline |

Các giá trị mặc định ở state frontend không chứng minh cấu hình hiệu lực của mọi sách. Khi có dữ liệu, trang cập nhật theo các cờ của sách; riêng abbreviations và breaks thiếu trường thì mặc định bật.

Pipeline backend còn có các bước khác, bao gồm loại CJK và dòng không đọc được. Hai bước này trong `normalize_text` được chạy không phụ thuộc các công tắc trên. Cần xem preview để tránh vô tình mất nội dung ngoại ngữ có chủ ý.

### 10.2. Sửa chương không đồng nghĩa dựng lại audio

Khi lưu chương, backend cập nhật metadata và tính lại số chunk cho các patch liên quan đủ điều kiện. Quá trình này:

- Bỏ qua tính lại plan của patch có `clean_text`.
- Bỏ qua patch đang `processing` vì worker sở hữu quá trình tổng hợp.
- Không tự chứng minh audio/video đã dựng phản ánh văn bản mới.

Sau sửa, cần kiểm tra và chủ động tạo lại đầu ra liên quan. Nếu đang tổng hợp, tránh biên tập đồng thời; chờ job hoàn tất hoặc thực hiện thao tác dừng phù hợp trước khi sửa. Không xem `chunk_count` mới là bằng chứng audio cũ đã được thay thế.

## 11. Quy trình biên tập khuyến nghị

### Bước 1 — Giữ bản nguồn và ghi nhận hiện trạng

- Giữ bản EPUB gốc và bản sao dữ liệu/chỉnh sửa quan trọng.
- Ghi ID sách, tên file, số chương và số patch.
- Kiểm tra có job audio/video đang chạy hay không.
- Ghi cấu hình `max_chars`, model và normalization để có thể tái hiện kết quả.

### Bước 2 — Phân tích toàn bộ Mục lục

- Bấm **Phân tích nội dung**.
- Ghi số chương error/warning, tiêu đề chưa chuẩn và tình trạng số chương.
- Xem lần lượt lỗi, cảnh báo, tiêu đề và chương loại trừ.
- Không dùng ô “Lỗi” ở đầu trang thay cho báo cáo này.

### Bước 3 — Sửa theo ưu tiên

1. Khôi phục chương thiếu/nội dung rỗng hoặc lỗi mã hóa.
2. Xác minh chương trùng, thứ tự sai và khoảng patch trượt.
3. Sửa ký tự lỗi, markup còn sót và đoạn không chia được.
4. Chuẩn hóa tiêu đề có thể tự sửa; bổ sung tên/số thủ công khi cần.
5. Rà soát viết tắt, chính tả nghi vấn, URL và hiệu ứng.

Với từng chương: mở → đọc ngữ cảnh → sửa → **Phân tích lại** → kiểm tra kết quả → **Lưu**. Nếu đánh dấu loại trừ, ghi rõ lý do; không loại chương thật chỉ để làm sạch thống kê.

### Bước 4 — Kiểm tra cấu trúc và văn bản đọc

- Phân tích lại toàn bộ sau khi lưu để kiểm tra trùng nội dung và số chương liên chương.
- Kiểm tra patch gap/overlap/desync.
- Kiểm tra normalization preview và các quy tắc thay thế.
- Nếu có `clean_text`, kiểm tra văn bản riêng của patch.
- Kiểm tra chunk plan với giới hạn phù hợp cấu hình TTS dự kiến.

### Bước 5 — Sản xuất lại và nghe thử

- Tạo lại audio của patch bị ảnh hưởng, rồi dựng lại video nếu cần.
- Nghe đầu/cuối chương, chỗ chuyển chương và các đoạn vừa sửa.
- Kiểm tra không đọc lặp tiêu đề, không mất câu, không đọc URL/rác ngoài ý muốn.
- Chỉ bật nối tiếp video/YouTube sau khi nội dung và cách đọc được xác nhận.

`runBatch("audio")` ở trang cha không tự lọc patch dựa trên báo cáo lỗi nội dung. Không nên hiểu việc nút TTS cho phép bấm là nội dung đã vượt qua kiểm tra chất lượng.

## 12. Các thao tác phá hủy dữ liệu cần tránh nhầm

| Thao tác trên trang | Dữ liệu bị mất theo thông báo UI | Dữ liệu được giữ theo thông báo UI |
| --- | --- | --- |
| “Xóa & nạp lại mục lục” | Chỉnh sửa nội dung/tiêu đề, trạng thái loại trừ, patch và audio/video patch | EPUB nguồn, cấu hình sản xuất, branding và thumbnail |
| “Xóa EPUB và patch” | EPUB, mục lục, patch và audio/video patch | Hồ sơ cấu hình sản xuất, branding, overlay và artwork thumbnail |
| Upload EPUB mới khi chưa có nguồn | Nạp lại mục lục từ file mới vào hồ sơ hiện tại | Tên đã chỉnh, cấu hình và thumbnail hiện tại |

**Không dùng nạp lại toàn bộ để sửa một vài lỗi nhỏ.** Thao tác này không giữ bản biên tập chương đã sửa. Nó cũng không có nghĩa xóa video đã đăng trên YouTube.

Repository còn có API reimport dạng preview/merge riêng. Không đánh đồng nó với nút **Xóa & nạp lại mục lục** gọi `/chapters/reimport` trong `BookDetail`.

## 13. API phục vụ kiểm tra

Các đường dẫn là tương đối; thay `{book_id}`, `{chapter_index}`, `{patch_id}` bằng giá trị thực. Chỉ số chương trong API bắt đầu từ 0.

| Method | Endpoint | Chức năng |
| --- | --- | --- |
| GET | `/books/{book_id}/chapters/validation` | Báo cáo tất cả chương, summary, numbering và thống kê tiêu đề |
| GET | `/books/{book_id}/chapters/{chapter_index}?analyze=1` | Chi tiết chương, report, spans và patch liên quan |
| POST | `/books/{book_id}/chapters/{chapter_index}/analyze` | Phân tích title/text bản nháp, không ghi |
| PUT | `/books/{book_id}/chapters/{chapter_index}` | Lưu title/text/is_excluded và tính lại dữ liệu liên quan |
| GET | `/books/{book_id}/chapters/title-normalize/preview` | Xem các tiêu đề fixable và các tiêu đề cần sửa thủ công |
| POST | `/books/{book_id}/chapters/title-normalize` | Áp dụng cho `chapter_indices` được chọn |
| GET | `/books/{book_id}/validation` | Phân tích chương và chunk plan của tất cả patch |
| GET | `/books/{book_id}/patches/{patch_id}/validation` | Phân tích plan một patch, trả chunk lỗi |
| GET | `/books/{book_id}/patches/ranges` | Kiểm tra khoảng chương của các patch |

Ví dụ payload bản nháp:

```json
{
  "title": "Chương 12: Cuộc gặp",
  "text": "Nội dung chương đang biên tập."
}
```

Ví dụ payload lưu:

```json
{
  "title": "Chương 12: Cuộc gặp",
  "text": "Nội dung chương đã được xác minh.",
  "is_excluded": false
}
```

Title khi lưu bị giới hạn tối đa 400 ký tự. API phân tích bản nháp lấy trạng thái loại trừ từ chương đang lưu, không nhận trạng thái `draftExcluded`; sau khi đổi checkbox cần lưu để báo cáo phản ánh trạng thái mới.

Endpoint validation toàn chương/toàn sách có thể nhận `max_chars`; hook `useChapterValidation` hiện không gửi tham số này nên dùng `settings.tts_max_chars` của backend. API chi tiết/phân tích/lưu chương cũng dùng giá trị backend đó. Validation một patch ưu tiên tham số, rồi `patch.max_chars`, rồi cấu hình backend. Vì vậy các màn hình có thể dùng ngưỡng khác nhau nếu cấu hình sách hoặc patch khác mặc định.

## 14. Giới hạn, rủi ro và đề xuất cải thiện

### 14.1. Giới hạn từ khâu nhập EPUB

Parser kiểm tra nội dung **sau trích xuất**, không bảo đảm giữ nguyên mọi nội dung của file:

- Đọc theo spine, không theo thứ tự manifest.
- Dùng h1/h2/h3 để chia chương; sách dùng heading để chia mục có thể bị chia thành nhiều chương.
- Với nhiều heading, phần trước heading đầu không được đưa vào các phần chương trong hàm chia.
- Bỏ phần ngắn dưới 50 ký tự; ngưỡng này khác warning dưới 120 của validator.
- Có heuristic loại phần đầu giống mục lục; chương thơ/hội thoại nhiều dòng ngắn có thể bị nhận nhầm. Có guard giữ lại nếu việc lọc sẽ làm danh sách chương rỗng.
- Loại script/style và một số ký tự CJK khi trích xuất.
- Có xử lý manifest tham chiếu file không tồn tại; nếu thiếu file nội dung thực, validator không thể khôi phục chương từ đó.

Hệ quả: không có `cjk_residue` hoặc `too_short` không chứng minh EPUB nguồn không chứa các phần này; chúng có thể đã bị lọc trước. Cần đối chiếu số chương và nội dung với bản nguồn, đặc biệt đầu/cuối sách.

### 14.2. Độ mới của báo cáo và bản nháp

- `useChapterValidation` chạy khi mở/đổi ID và khi reload, không polling mỗi 5 giây như dữ liệu chính.
- Lỗi tải báo cáo bị catch im lặng; có thể còn báo cáo cũ hoặc chưa có báo cáo. Không nên diễn giải trường hợp đó thành “không có lỗi”.
- Hook không có cơ chế bỏ kết quả request cũ; thao tác đổi sách/reload nhanh có nguy cơ response đến lệch thứ tự.
- Khi sửa bản nháp sau một lần phân tích, vị trí span cũ có thể không còn khớp. Hãy phân tích lại trước khi tin highlight.

Đề xuất: hiển thị lỗi tải, timestamp/phiên bản nội dung và trạng thái “cần phân tích lại”; dùng request token hoặc AbortController để chống kết quả cũ.

### 14.3. Offset Unicode

Backend tính `start`/`length` bằng chỉ số chuỗi Python; frontend cắt chuỗi bằng `slice` JavaScript. Hai hệ thống khác nhau với ký tự ngoài BMP, ví dụ emoji. Văn bản có emoji trước vùng lỗi có nguy cơ tô sáng lệch.

Đề xuất: chuẩn hóa offset sang UTF-16 tại API hoặc chuyển offset trên frontend, bổ sung test có emoji trước vùng CJK/HTML/URL. Đây là rủi ro suy ra từ cách đánh chỉ số; tài liệu này chưa chạy kiểm thử UI để xác nhận một trường hợp cụ thể.

### 14.4. Chất lượng nội dung vượt ngoài bộ quy tắc

Hệ thống chưa chứng minh được:

- Đúng bản dịch, đúng ý nghĩa, không mất câu/đoạn.
- Chính tả/ngữ pháp đầy đủ và nhất quán tên riêng.
- Chương gần trùng hoặc bị lặp một phần.
- Tổng chương đúng theo ấn bản gốc.
- Cách đọc TTS tự nhiên và đúng mọi từ ngoại ngữ.

Đề xuất: thêm nguồn mục lục chuẩn để đối chiếu, checksum/toàn văn khi xác nhận trùng, phát hiện gần trùng, lịch sử chỉnh sửa và báo cáo so sánh trước/sau normalization. Những mục này là **đề xuất**, không phải chức năng đã được xác nhận hiện có.

## 15. Ma trận kiểm thử và tiêu chí nghiệm thu

Đây là bộ tình huống kiểm thử khuyến nghị; không phải kết quả test đã chạy trong quá trình viết tài liệu.

| Tình huống | Kết quả mong đợi theo quy tắc hiện tại |
| --- | --- |
| Văn bản chỉ khoảng trắng | `empty`, error |
| Văn bản `*** !!!` | `unspeakable`, error |
| Chương có chữ/số dưới 120 ký tự | `too_short`, warning |
| Có `�` | `mojibake`, error và span tương ứng |
| Có chữ Hán trong văn bản đã lưu | `cjk_residue`, error |
| Có HTML/entity/link còn sót | warning tương ứng |
| Đoạn 601 ký tự, không dấu kết câu, max_chars 400 | `unsplittable_paragraph`, error |
| Đoạn 600 ký tự với cùng điều kiện | Không phát sinh mã trên chỉ vì độ dài |
| Tiêu đề `Chương 12: Cuộc gặp` | canonical |
| Tiêu đề `Chapter 12: Cuộc gặp` | fixable, có suggested_title |
| Tiêu đề `Chương 12` | no_name; cần bổ sung thủ công |
| Số chương `1, 2, 4` | missing_count 1, missing_numbers có 3 |
| Số chương `1, 2, 2, 3` | duplicate_count 1 |
| Số chương `1, 3, 2` | Có out_of_order, is_continuous false |
| Hai chương cùng văn bản đã trim | `duplicate_text` trên các chương liên quan |
| Patch có clean_text rồi sửa chương nguồn | Kiểm tra plan vẫn dùng văn bản override |
| Chương bị loại trừ | Không đọc trong plan chương thông thường; kiểm tra thống kê vẫn có thể báo issue |
| Quy tắc thay thế làm rỗng toàn bộ patch | Plan có thể không có chunk, `no_chunks` |
| Emoji trước vùng lỗi | Kiểm tra UI không tô lệch; nếu lệch cần sửa quy đổi offset |
| Request phân tích thất bại | Cần nhận diện báo cáo không khả dụng/cũ, không kết luận sạch |

Checklist nghiệm thu cho một cuốn sách:

- [ ] Đã đối chiếu số lượng và thứ tự chương với nguồn đáng tin cậy.
- [ ] Không còn error chưa được giải thích trong các chương cần đọc.
- [ ] Mọi warning được sửa hoặc chấp nhận có lý do.
- [ ] Các tiêu đề chưa chuẩn và phần phụ được xử lý rõ ràng.
- [ ] Không có thiếu/trùng nội dung ngoài chủ ý của tác phẩm.
- [ ] Khoảng patch không hở/chồng lấn/trượt ngoài chủ ý.
- [ ] Đã kiểm tra văn bản sau normalization và clean_text nếu có.
- [ ] Chunk plan dùng đúng giới hạn model và không có lỗi nghiêm trọng.
- [ ] Audio/video liên quan đã được tạo lại sau chỉnh sửa cần thiết.
- [ ] Đã nghe thử trước khi xuất bản.

## 16. Nguồn mã và kiểm thử tham khảo

| Tệp | Vai trò |
| --- | --- |
| [`frontend/src/pages/BookDetail.tsx`](../frontend/src/pages/BookDetail.tsx) | Điều phối trạng thái, cảnh báo, thao tác batch và làm mới sau sửa |
| [`frontend/src/pages/book-detail/useBookDetail.ts`](../frontend/src/pages/book-detail/useBookDetail.ts) | Hook tải dữ liệu và báo cáo chương |
| [`frontend/src/pages/book-detail/ChaptersPanel.tsx`](../frontend/src/pages/book-detail/ChaptersPanel.tsx) | Danh sách, bộ lọc và thao tác phân tích |
| [`frontend/src/pages/book-detail/ChapterDialog.tsx`](../frontend/src/pages/book-detail/ChapterDialog.tsx) | Xem/sửa/phân tích bản nháp/lưu chương |
| [`frontend/src/pages/book-detail/HighlightedText.tsx`](../frontend/src/pages/book-detail/HighlightedText.tsx) | Render span và ký tự vô hình |
| [`frontend/src/pages/book-detail/types.ts`](../frontend/src/pages/book-detail/types.ts) | Kiểu report, issue, numbering và span |
| [`app/epub_parser.py`](../app/epub_parser.py) | Trích xuất EPUB, chia chương và lọc đầu vào |
| [`app/validation.py`](../app/validation.py) | Quy tắc lỗi chương, số chương, khoảng patch, chunk và highlight |
| [`app/text_analysis.py`](../app/text_analysis.py) | Phát hiện mềm: rác, viết tắt, chính tả nghi vấn và âm thanh |
| [`app/routes/validation.py`](../app/routes/validation.py) | API kiểm tra, sửa chương và chuẩn hóa tiêu đề |
| [`app/normalization.py`](../app/normalization.py) | Pipeline chuẩn hóa trước TTS |
| [`app/repository.py`](../app/repository.py) | Lưu chương và xây dựng văn bản/chunk plan |
| [`tests/test_validation.py`](../tests/test_validation.py) | Các test liên quan validation và metadata |
| [`tests/test_toc_filter.py`](../tests/test_toc_filter.py) | Test heuristic lọc mục lục |
| [`tests/test_normalization.py`](../tests/test_normalization.py) | Test pipeline chuẩn hóa |

Tài liệu liên quan: [`toi_uu_tts.md`](toi_uu_tts.md).
