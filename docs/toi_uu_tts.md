Nếu mục tiêu là làm cho TTS tiếng Việt **đọc ngắt nghỉ tự nhiên hơn**, bạn nên tập trung vào phần **text frontend**: chuẩn hóa văn bản → tách câu/từ → dự đoán cụm ngữ pháp → sinh nhãn ngắt nghỉ/SSML. Dưới đây là các thuật toán và mã nguồn mở phù hợp.

---

## 1. Pipeline tiền xử lý đề xuất cho TTS tiếng Việt

```text
Raw text
  │
  ├─ 1. Text normalization: số, ngày, tiền tệ, viết tắt, URL, email...
  │
  ├─ 2. Sentence segmentation: tách câu, tránh tách sai ở "TP.HCM", "GS.TS", "1.5"
  │
  ├─ 3. Word segmentation: gộp syllable thành từ: "ủy_ban nhân_dân thành_phố"
  │
  ├─ 4. POS tagging / chunking / parsing: xác định cụm danh từ, động từ, mệnh đề
  │
  ├─ 5. Punctuation / pause prediction: dự đoán chỗ nên có dấu phẩy, ngắt hơi
  │
  ├─ 6. G2P / phonemization: chuyển chữ thành âm vị, giữ thanh điệu tiếng Việt
  │
  └─ 7. Output: text có break tag / prosody token cho TTS
```

Mấu chốt nằm ở bước **3, 4, 5**. Nếu chỉ đưa text thô vào TTS, mô hình thường ngắt nghỉ theo khoảng trắng, mà trong tiếng Việt khoảng trắng đang nằm giữa các **âm tiết**, không phải giữa các **từ**.

Ví dụ:

```text
Ủy ban nhân dân thành phố Hồ Chí Minh
```

Nếu TTS coi mỗi khoảng trắng là một ranh giới tiềm năng, nó có thể đọc rời rạc. Nên tách từ/gộp từ thành:

```text
Ủy_ban nhân_dân thành_phố Hồ_Chí_Minh
```

---

## 2. Open source nên dùng cho tiếng Việt

| Bài toán | Công cụ/open source | Ghi chú |
|---|---|---|
| Tách câu | `underthesea.sent_tokenize` | Dễ dùng, phù hợp tiếng Việt |
| Tách từ tiếng Việt | `VnCoreNLP`, `Underthesea`, `pyvi`, `RDRsegmenter` | Rất quan trọng để giảm ngắt nghỉ vụn |
| POS tagging | `VnCoreNLP`, `Underthesea` | Giúp phát hiện ranh giới cụm từ |
| Chunking/parsing | `VnCoreNLP` | Có thể suy ra ranh giới mệnh đề |
| NER | `Underthesea`, `VnCoreNLP` | Tránh tách sai tên riêng, địa danh |
| G2P tiếng Việt | `PhoGPT`, `eSpeak-ng` | `PhoGPT` thường phù hợp hơn cho TTS hiện đại |
| Normalization số | `num2words`, tự viết rule | Tiếng Việt cần xử lý ngày, tiền, số điện thoại riêng |
| Normalization bằng WFST | `pynini`, `OpenFst`, `NeMo Text Processing` | Mạnh, nhưng cần tự xây grammar tiếng Việt |
| Dự đoán dấu câu/ngắt nghỉ | Fine-tune `PhoBERT` | Xem như bài toán sequence labeling |
| Forced alignment để tạo dữ liệu pause | `Montreal Forced Aligner`, `WhisperX` | Hữu ích nếu bạn có audio để train |

Một số repo đáng chú ý:

```text
Underthesea:
https://github.com/undertheseanlp/underthesea

VnCoreNLP:
https://github.com/vncorenlp/VnCoreNLP

pyvi:
https://github.com/trungtv/pyvi

RDRsegmenter:
https://github.com/datquocnguyen/RDRsegmenter

PhoBERT:
https://github.com/VinAIResearch/PhoBERT

PhoGPT:
https://github.com/VinAIResearch/PhoGPT

Montreal Forced Aligner:
https://github.com/MontrealCorpusTools/Montreal-Forced-Aligner

eSpeak-ng:
https://github.com/espeak-ng/espeak-ng
```

---

## 3. Thuật toán ngắt nghỉ: từ đơn giản đến nâng cao

### Cách 1: Rule-based dựa trên dấu câu

Đơn giản, chạy nhanh, phù hợp baseline.

Ánh xạ dấu câu thành mức ngắt:

```python
BREAK_MAP = {
    ".": "major",
    "!": "major",
    "?": "major",
    ",": "minor",
    ";": "minor",
    ":": "minor",
    "-": "extra_minor",
    "—": "extra_minor",
}
```

Ví dụ output SSML:

```xml
<s>
Hôm nay,
<break time="200ms"/>
do trời mưa,
<break time="180ms"/>
nên đường rất đông.
<break time="450ms"/>
</s>
```

Ưu điểm:

- Dễ triển khai.
- Không cần dữ liệu huấn luyện.
- Phù hợp nếu text đầu vào đã có dấu câu tốt.

Nhược điểm:

- Nếu text thiếu dấu câu, kết quả kém.
- Không hiểu ngữ pháp/ngữ nghĩa.
- Dễ ngắt sai trong câu dài, câu phức.

---

### Cách 2: Tách từ + chunking để tạo cụm ngữ pháp

Thuật toán:

1. Dùng `VnCoreNLP` hoặc `Underthesea` để tách từ.
2. Gắn POS tag.
3. Xác định cụm danh từ, cụm động từ, cụm giới từ.
4. Cho phép ngắt nhẹ giữa các cụm, không ngắt bên trong cụm.

Ví dụ:

```text
[UBND TP.HCM] [đã ban hành] [quyết định số 123/QĐ-UBND].
```

Có thể sinh:

```text
Ủy_ban nhân_dân thành_phố Hồ_Chí_Minh <break_minor>
đã ban_hành <break_minor>
quyết_định số một_hai_ba ...
```

Ưu điểm:

- Giảm đọc rời rạc.
- Ngắt nghỉ theo cụm tự nhiên hơn.
- Không cần dữ liệu audio.

Nhược điểm:

- Phụ thuộc chất lượng word segmentation và POS tagging.
- Cần xử lý tên riêng, viết tắt, tổ chức.

---

### Cách 3: Sequence labeling để dự đoán điểm ngắt nghỉ

Đây là hướng tốt nếu bạn muốn chất lượng cao hơn.

Mô hình hóa bài toán:

```text
Input tokens:  Hôm nay do trời mưa nên đường rất đông
Break labels:      0    1    0   0   1    0   0   0   1
```

Trong đó:

```text
0 = không ngắt
1 = ngắt ngắn
2 = ngắt vừa
3 = ngắt dài / hết câu
```

Thuật toán có thể dùng:

- CRF
- BiLSTM + CRF
- PhoBERT + classification/CRF
- XLM-R nếu cần mô hình đa ngôn ngữ

Đặc trưng hữu ích:

- Dấu câu hiện có.
- POS tag.
- Từ loại trước/sau.
- Độ dài cụm.
- Khoảng cách phụ thuộc cú pháp.
- Liên từ: `và`, `nhưng`, `tuy nhiên`, `vì vậy`, `nên`, `do`, `khi`, `sau khi`.
- Vị trí đầu/cuối mệnh đề.
- Có phải tên riêng, số, ngày tháng không.

Ưu điểm:

- Ngắt nghỉ tự nhiên hơn rule-based.
- Học được phong cách đọc từ dữ liệu thật.
- Có thể dự đoán cả chỗ thiếu dấu câu.

Nhược điểm:

- Cần dữ liệu gán nhãn pause hoặc ít nhất dữ liệu văn bản có dấu câu tốt.
- Tốn công huấn luyện hơn.

---

### Cách 4: Dự đoán dấu câu trước, rồi suy ra ngắt nghỉ

Nếu đầu vào không có dấu câu, ví dụ:

```text
hôm nay do trời mưa nên đường rất đông
```

Bạn có thể huấn luyện mô hình khôi phục dấu câu:

```text
Input:  hôm nay do trời mưa nên đường rất đông
Output: Hôm nay, do trời mưa nên đường rất đông.
```

Có thể dùng:

- PhoBERT token classification.
- BiLSTM-CRF.
- Seq2seq nhỏ nếu muốn linh hoạt hơn.

Nhãn gợi ý:

```text
O        : không thêm dấu
COMMA    : thêm dấu phẩy
PERIOD   : thêm dấu chấm
QUESTION : thêm dấu hỏi
EXCLAIM  : thêm dấu than
COLON    : thêm dấu hai chấm
```

Sau khi có dấu câu, bạn ánh xạ sang break như cách 1.

Đây là hướng rất đáng làm nếu nguồn text là:

- Phụ đề ASR.
- Chat không dấu.
- Text OCR.
- Input từ voice command đã được ASR hóa.

---

## 4. Open source cụ thể cho từng tầng

### 4.1. Tách câu tiếng Việt

Dùng:

```python
from underthesea import sent_tokenize

text = "UBND TP.HCM đã ban hành quyết định. Ngày 1/3/2024, có hiệu lực."
sentences = sent_tokenize(text)
```

Lưu ý: cần xử lý thêm các trường hợp không được tách câu:

```text
TP.HCM
GS.TS
PGS.TS
QĐ-UBND
1.5
1/3/2024
```

Có thể dùng regex whitelist:

```python
DO_NOT_SPLIT_PATTERNS = [
    r"\d+[.,]\d+",
    r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}",
    r"(?:TP|HN|HCM)\.[A-Za-zÀ-ỹ]+",
    r"(?:GS|PGS|TS|BS|ThS)\.(?:TS|BS|ThS)?",
]
```

---

### 4.2. Tách từ / word segmentation

Đây là phần rất quan trọng cho tiếng Việt.

Có thể dùng:

```python
from underthesea import word_sent_seg

text = "Ủy ban nhân dân thành phố Hồ Chí Minh"
words = word_sent_seg(text)
print(words)
```

Kết quả dạng:

```text
['Ủy_ban', 'nhân_dân', 'thành_phố', 'Hồ_Chí_Minh']
```

Khi đưa vào TTS, bạn nên giữ các từ ghép như một đơn vị, không để mô hình hiểu nhầm là phải ngắt giữa các âm tiết.

Ví dụ:

```text
Ủy_ban nhân_dân thành_phố Hồ_Chí_Minh
```

Hoặc chuyển thành token đặc biệt:

```text
Ủy ban nhân dân thành phố Hồ Chí Minh
```

Nếu dùng phoneme:

```text
[Ủy_ban] [nhân_dân] [thành_phố] [Hồ_Chí_Minh]
```

Không nên:

```text
Ủy <break> ban <break> nhân <break> dân
```

---

### 4.3. POS tagging và chunking

Dùng:

- `VnCoreNLP`
- `Underthesea`

POS tag giúp biết ranh giới cụm:

```text
N   : danh từ
V   : động từ
A   : tính từ
P   : giới từ
C   : liên từ
R   : phó từ
```

Ví dụ heuristic:

```text
Sau cụm danh từ dài -> có thể ngắt nhẹ
Trước liên từ "nhưng", "tuy nhiên" -> có thể ngắt
Sau trạng ngữ đầu câu -> có thể ngắt
Giữa các mệnh đề -> ngắt vừa
```

Ví dụ:

```text
Ngày mai, trong buổi họp giao ban, chúng ta sẽ thống nhất kế hoạch.
```

Break hợp lý:

```text
Ngày mai,<break>
trong buổi họp giao ban,<break>
chúng ta sẽ thống nhất kế hoạch.
```

---

## 5. Tiền xử lý văn bản: normalization

Đây là phần ảnh hưởng rất lớn đến việc TTS đọc có tự nhiên không.

### 5.1. Số

Không nên đưa trực tiếp:

```text
123456
```

Nên chuyển thành:

```text
một trăm hai mươi ba nghìn bốn trăm năm mươi sáu
```

Có thể dùng:

```bash
pip install num2words
```

```python
from num2words import num2words

print(num2words(123, lang="vi"))
# một trăm hai mươi ba
```

Tuy nhiên, cần tự xử lý thêm:

- Số thập phân: `1,5` → `một phẩy năm`
- Số tiền: `1.500.000đ` → `một triệu năm trăm nghìn đồng`
- Phần trăm: `15%` → `mười lăm phần trăm`
- Số điện thoại: đọc từng nhóm hoặc theo quy tắc riêng
- Năm: `2024` → `hai nghìn không trăm hai mươi bốn`
- Ngày: `1/3/2024` → `ngày một tháng ba năm hai nghìn không trăm hai mươi bốn`

---

### 5.2. Ngày tháng

Cần phân biệt:

```text
1/3      # ngày 1 tháng 3? hay một phần ba?
1/3/2024 # ngày tháng năm
```

Quy tắc gợi ý:

```python
date_pattern = r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b"
```

Nếu match 3 thành phần và có năm, ưu tiên đọc là ngày tháng năm.

Nếu chỉ có `1/2`, `1/3`, cần xem ngữ cảnh:

```text
1/2 số dân      # một phần hai
họp ngày 1/2    # ngày một tháng hai
```

Nếu khó, nên ưu tiên đọc theo dạng ngày khi có từ khóa:

```text
ngày, tháng, năm, hẹn, lịch, họp, hạn
```

---

### 5.3. Viết tắt

Cần có từ điển riêng. Ví dụ:

```json
{
  "UBND": "ủy ban nhân dân",
  "TP.HCM": "thành phố Hồ Chí Minh",
  "QĐ": "quyết định",
  "NĐ-CP": "nghị định chính phủ",
  "GS.TS": "giáo sư tiến sĩ",
  "PGS.TS": "phó giáo sư tiến sĩ",
  "Q.": "quận",
  "P.": "phường"
}
```

Nhưng cần thận trọng:

```text
Q.1   -> Quận 1
Q.    -> có thể là chữ Q
QĐ    -> quyết định
QĐ-UBND -> quyết định ủy ban nhân dân
```

Bạn nên xây dựng một bộ abbreviation dictionary theo domain:

- Pháp luật: `QĐ`, `NĐ`, `TT`, `UBND`, `HĐND`
- Y tế: `BS`, `TS`, `ThS`, `PGS.TS`
- Giáo dục: `GV`, `HS`, `THPT`, `ĐHQG`
- Địa danh: `TP.`, `Q.`, `P.`, `TT.`, `H.`

---

### 5.4. URL, email, số điện thoại

Không nên để TTS đọc nguyên dạng:

```text
https://example.com
```

Tùy ứng dụng:

```text
https://example.com -> example chấm com
```

Email:

```text
support@example.com -> support a còng example chấm com
```

Số điện thoại:

```text
0987654321 -> không chín tám bảy sáu năm bốn ba hai một
```

Hoặc:

```text
0987 654 321 -> không chín tám bảy, sáu năm bốn, ba hai một
```

---

## 6. Sinh SSML hoặc break token

Nếu TTS hỗ trợ SSML:

```xml
<speak>
  <s>
    Hôm nay,
    <break time="200ms"/>
    do trời mưa,
    <break time="180ms"/>
    nên đường rất đông.
    <break time="450ms"/>
  </s>
</speak>
```

Nếu TTS tự huấn luyện, bạn có thể đưa break token:

```text
Hôm nay [B1] do trời mưa [B1] nên đường rất đông [B3]
```

Hoặc:

```text
Hôm nay <break_1> do trời mưa <break_1> nên đường rất đông <break_3>
```

Các mức break gợi ý:

```text
B0: không ngắt
B1: ngắt rất ngắn, giữa các cụm nhỏ
B2: ngắt ngắn, sau dấu phẩy, giữa mệnh đề
B3: ngắt vừa, kết thúc câu
B4: ngắt dài, chuyển đoạn
```

Thời gian gợi ý ban đầu:

```text
B1: 80–150 ms
B2: 150–250 ms
B3: 300–500 ms
B4: 600–900 ms
```

Bạn nên tune theo giọng và tốc độ TTS thực tế.

---

## 7. Nếu có dữ liệu audio: huấn luyện mô hình pause prediction

Đây là cách tốt nhất nếu bạn muốn TTS đọc ngắt nghỉ tự nhiên theo phong cách người thật.

### 7.1. Tạo nhãn pause từ audio

Các bước:

```text
Audio + transcript
  │
  ├─ Forced alignment để lấy thời gian từng từ/âm tiết
  │
  ├─ Tính khoảng lặng giữa các từ
  │
  └─ Gán nhãn break
```

Có thể dùng:

- `Montreal Forced Aligner`
- `WhisperX`
- CTC segmentation
- Kaldi/ESPnet alignment pipeline

Ví dụ heuristic gán nhãn:

```text
Khoảng lặng < 80 ms       -> B0
80–180 ms                  -> B1
180–350 ms                 -> B2
> 350 ms                   -> B3
```

Lưu ý: ngưỡng cần điều chỉnh theo dữ liệu thật.

---

### 7.2. Huấn luyện mô hình break prediction

Input:

```text
Hôm nay do trời mưa nên đường rất đông
```

Label:

```text
Hôm nay      B1
do           B0
trời         B0
mưa          B1
nên          B0
đường        B0
rất          B0
đông         B3
```

Mô hình gợi ý:

- `BiLSTM-CRF`
- `PhoBERT + linear classification`
- `PhoBERT + CRF`
- `XLM-R` nếu có dữ liệu đa ngôn ngữ

Nếu dữ liệu ít:

- Dùng rule-based làm baseline.
- Fine-tune PhoBERT trên dữ liệu có dấu câu.
- Dùng CRF để bắt buộc nhãn hợp lý theo chuỗi.

---

## 8. Một số lỗi tiếng Việt thường gặp khi làm TTS

### 8.1. Ngắt nghỉ giữa các âm tiết trong một từ

Sai:

```text
ủy ban nhân dân
```

Nếu TTS coi mỗi khoảng trắng là break, nó có thể đọc rất rời.

Nên:

```text
ủy_ban nhân_dân
```

---

### 8.2. Đọc sai viết tắt

Ví dụ:

```text
TP.HCM
```

Nếu không normalize, có thể đọc thành:

```text
tê pê hát xê mờ
```

Nhưng thường mong muốn:

```text
thành phố Hồ Chí Minh
```

Bạn cần dictionary.

---

### 8.3. Tách câu sai ở dấu chấm trong tên riêng

Ví dụ:

```text
Ông làm việc tại TP.HCM.
```

Nếu sentence splitter tách sau `TP`, câu sẽ vỡ.

Cần bảo vệ:

```text
TP.HCM
Q.1
P.5
GS.TS
PGS.TS
```

---

### 8.4. Ngày tháng và phân số

Ví dụ:

```text
1/2
```

Có thể là:

```text
một phần hai
```

hoặc:

```text
ngày một tháng hai
```

Cần ngữ cảnh.

---

### 8.5. Số lớn

Ví dụ:

```text
1.000.000
```

Nên đọc:

```text
một triệu
```

Không nên:

```text
một chấm không trăm nghìn
```

---

## 9. Pipeline mẫu bằng Python

Dưới đây là ví dụ dạng pseudo-code:

```python
from underthesea import sent_tokenize, word_sent_seg, pos_tag

def normalize_text(text):
    # TODO:
    # - thay abbreviation
    # - chuyển số, ngày, tiền tệ
    # - xử lý email, URL, số điện thoại
    return text

def add_break_tags(sentence):
    words = word_sent_seg(sentence)
    tags = pos_tag(sentence)

    output = []

    for word, tag in zip(words, tags):
        output.append(word.replace("_", " "))

        # rule đơn giản
        if word in [",", ";", ":"]:
            output.append('<break time="180ms"/>')
        elif word in [".", "!", "?"]:
            output.append('<break time="450ms"/>')
        elif tag in ["C"] and word in ["nhưng", "tuy nhiên", "vì vậy", "nên", "do"]:
            output.append('<break time="120ms"/>')

    return " ".join(output)

def preprocess_for_tts(text):
    text = normalize_text(text)
    sentences = sent_tokenize(text)
    processed = []

    for s in sentences:
        processed.append(add_break_tags(s))

    return "<speak>" + " ".join(processed) + "</speak>"
```

Đây chỉ là baseline. Để tốt hơn, bạn nên thay `add_break_tags` bằng mô hình dự đoán break.

---

## 10. Gợi ý cấu hình theo mức độ

### Mức 1: Nhanh, đơn giản

Dùng:

```text
Regex normalization
+ Underthesea sentence segmentation
+ Underthesea word segmentation
+ Rule-based break theo dấu câu
```

Phù hợp:

- Demo.
- Text đầu vào đã có dấu câu tương đối tốt.
- Không cần huấn luyện.

---

### Mức 2: Cân bằng chất lượng và độ phức tạp

Dùng:

```text
VnCoreNLP hoặc Underthesea
+ POS tagging
+ Chunking heuristic
+ Abbreviation dictionary
+ num2words
+ SSML break tag
```

Phù hợp:

- Chatbot.
- IVR.
- Đọc báo, đọc văn bản hành chính.
- Ứng dụng cần chất lượng khá.

---

### Mức 3: Chất lượng cao

Dùng:

```text
Custom normalization
+ VnCoreNLP/Underthesea
+ PhoBERT punctuation restoration
+ PhoBERT/BiLSTM-CRF break prediction
+ PhoGPT hoặc G2P riêng
+ TTS model hỗ trợ prosody token: VITS, FastSpeech2, ESPnet...
```

Phù hợp:

- Audiobook.
- Trợ lý ảo.
- TTS chất lượng cao.
- Cần giọng đọc tự nhiên, ít máy móc.

---

## 11. Nếu bạn đang huấn luyện TTS tiếng Việt

Nếu bạn tự train TTS, hãy đưa break/prosody vào ngay từ dữ liệu huấn luyện.

Ví dụ:

```text
Hôm nay [B1] do trời mưa [B1] nên đường rất đông [B3]
```

Hoặc phoneme:

```text
h o m [B0] n a y [B1] d o [B0] t r oj [B0] m w a [B1] ...
```

Các mô hình có thể áp dụng:

- `VITS`
- `FastSpeech2`
- `GlowTTS`
- `ESPnet-TTS`
- `Coqui TTS`

Bạn có thể thêm:

- `break_token`
- `prosody_embedding`
- `duration predictor`
- `pause embedding`

Nếu mô hình không hỗ trợ break token, bạn có thể thêm ký tự đặc biệt vào vocabulary:

```text
[B0]
[B1]
[B2]
[B3]
```

Sau đó huấn luyện để mô hình học cách dừng tương ứng.

---

## 12. Đề xuất thực tế

Nếu bạn cần giải pháp thực dụng ngay:

```text
Underthesea
+ custom normalization
+ dictionary cho viết tắt
+ rule-based break
+ SSML
```

Nếu bạn muốn chất lượng cao hơn:

```text
VnCoreNLP/Underthesea
+ PhoBERT punctuation restoration
+ PhoBERT break prediction
+ PhoGPT
+ VITS/FastSpeech2 có prosody token
```

Nếu bạn có audio thật:

```text
Forced alignment
+ pause labeling
+ train break prediction model
```

Đây là cách tốt nhất để TTS ngắt nghỉ theo đúng phong cách người Việt đọc thật.