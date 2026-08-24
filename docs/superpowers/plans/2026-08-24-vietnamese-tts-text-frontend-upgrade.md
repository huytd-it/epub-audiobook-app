# Vietnamese TTS Text-Frontend Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Nguồn yêu cầu: `docs/toi_uu_tts.md` — tối ưu ngắt nghỉ TTS tiếng Việt. Plan này ánh xạ từng đề xuất trong tài liệu vào code hiện có của tool.

**Goal:** Nâng cấp text frontend của pipeline TTS tiếng Việt theo 3 mức trong `docs/toi_uu_tts.md`: (A) tách câu an toàn + mở rộng viết tắt + chèn cue ngắt nghỉ trong câu bằng rule, (B) tách từ/POS để dự đoán ngắt nghỉ chính xác hơn (optional dependency), (C) khung cắm mô hình PhoBERT break/punctuation prediction cho tương lai. Kết quả: giọng đọc bớt rời rạc giữa các âm tiết, ngắt nghỉ tự nhiên hơn trong lòng câu, không còn tách câu sai ở "TP.HCM", "GS.TS".

**Architecture:** Giữ nguyên kiến trúc hiện tại (chapters → normalize → chunk plan → TTS engine → merge với pause giữa chunk/chương). Thêm một tầng **break predictor** nằm giữa `normalize_text` và `split_into_tts_chunks`: nhận văn bản đã chuẩn hóa, trả về văn bản mang *cue dấu câu* (`,` `;` `...` `.`) tại vị trí nên ngắt — vì mọi engine hiện tại (VoxCPM2, OmniVoice, Confucius4, F5-ViVoice, VieNeu, ZeroTTS, Edge, gTTS) đều nhận plain text, không có SSML. Có 3 implementation cùng một interface `BreakPredictor`: rule-based (mặc định, không thêm dependency), linguistics-based (underthesea, optional), model-based (PhoBERT, stub ở Milestone C). Word segmentation được dùng nội bộ để quyết định chỗ *không* được ngắt, mặc định không thay đổi mặt chữ.

**Tech Stack:** Python 3.10–3.12, vietnormalizer (đã có), regex, pytest; optional: `underthesea` (extra mới `prosody`); tương lai: PhoBERT/transformers.

---

## Hiện trạng (đã khảo sát code)

| Giai đoạn | File hiện tại | Trạng thái | Gap so với docs/toi_uu_tts.md |
|---|---|---|---|
| Chuẩn hóa | `app/normalization.py` (`normalize_text`:235) | junk → CJK → dots → số/ngày/tiền (vietnormalizer) → đuôi file → dấu câu cuối dòng | Thiếu mở rộng viết tắt (UBND, TP.HCM… chỉ *cảnh báo* trong `text_analysis.py:71`, chưa áp dụng) |
| Tách câu | `app/chunker.py:6` `_SENTENCE_BOUNDARY_RE = (?<=[.!?…])\s+` | Regex thô | Sai ở "TP.HCM", "GS.TS", "QĐ-UBND", ngày `1. 5` — đúng lỗi mục 8.3 của tài liệu |
| Tách từ | Không có | — | Thiếu hoàn toàn bước 3 (mục 4.2): TTS đọc rời âm tiết |
| Ngắt nghỉ trong câu | Không có | Chỉ pause giữa chunk 300ms và giữa chương 1500ms tại merge (`audio_merge.py:22-23`) | Thiếu bước 4–5 (mục 3, mục 6): không có B0–B4 trong câu |
| Đầu ra cho TTS | Plain text per chunk (`repository.build_chunk_plan_from_inputs`:1146) | Engine tự ngắt theo dấu câu sẵn có | Không có cơ chế sinh break cue |

Lưu ý tích hợp:

- `clean_text` (patch sửa trong Text Studio) cố tình bỏ qua normalize — giữ nguyên hành vi, break predictor cũng không chạy trên `clean_text`.
- Batch Colab/Kaggle export dùng chính chunk text đã normalize (`manifest.json`), nên mọi nâng cấp ở tầng này tự động áp dụng cho bản export, không cần sửa notebook.
- `subtitle_gen.py` dùng lại `split_into_tts_chunks` chỉ để đo độ dài cue — thêm dấu câu không phá alignment (frame counts lấy từ audio thật).

## Global Constraints

- Không thêm dependency bắt buộc mới vào `dependencies`; `underthesea` phải nằm trong optional extra mới `[prosody]`, import lazy, thiếu package thì fallback về rule-based mà không raise.
- Break predictor chỉ được chèn dấu câu (`,` `;` `:` `...`), tuyệt đối không xóa/sửa từ, không đổi thứ tự, không chèn ký hiệu ngoài dấu câu phổ biến.
- Không bao giờ chèn cue ngay cạnh dấu câu đã tồn tại, không chèn trước từ đơn âm tiết đứng cuối câu, không vượt quá 1 cue mỗi ~8 âm tiết khi rule-based.
- Mọi hành vi mới phải tắt được qua `NormalizationOptions` flag; flag mặc định: `abbreviations=True`, `breaks=True` (rule-based), `word_segmentation=False`.
- Flag mới đi vào `automation_config["normalization"]` group JSON (không migration DB), kế thừa pattern `chunk_pause_ms` trong `production_defaults.py:408`.
- `clean_text` của Text Studio không bị đụng tới bởi bất kỳ bước mới nào.
- Output của `split_into_tts_chunks` không vượt `max_chars` kể cả sau khi chèn cue.
- Không đổi interface công khai của `normalize_text`, `split_into_tts_chunks`, `build_chunk_plan_from_inputs` (chỉ mở rộng).

## File Map

- Modify `app/chunker.py`: tách câu có bảo vệ mẫu (protected splitting), hook điểm ngắt phụ cho cue.
- Create `app/breaks.py`: `BreakPredictor` protocol + `RuleBasedBreakPredictor` + `predict_breaks()` façade + bảng mức B0–B3 → cue.
- Modify `app/normalization.py`: expansion viết tắt (dùng chung dict với `text_analysis`), gọi `predict_breaks` qua opts, `NormalizationOptions` thêm flag.
- Modify `app/text_analysis.py`: xuất `_ABBREVIATION_EXPANSIONS` thành API dùng chung (giữ nguyên hành vi cảnh báo).
- Modify `app/production_defaults.py`: đọc/ghi validate flag mới trong normalization group.
- Modify `app/repository.py`: gọi predictor trong `build_chunk_plan_from_inputs` và `build_patch_text` (đường preview).
- Modify `app/routes/text_studio.py` + `app/routes/books.py`: preview route nhận flag mới.
- Modify `frontend/src/...` (Text Studio normalize panel + Audio settings normalization section): checkbox Viết tắt / Ngắt nghỉ.
- Modify `pyproject.toml`: extra `[prosody]` với `underthesea`.
- Create `app/linguistic_breaks.py` (Milestone B): `UndertheseaBreakPredictor` (lazy import, fallback).
- Create `app/model_breaks.py` (Milestone C): skeleton `PhoBERTBreakPredictor` chưa bật, interface giống hệt.
- Test: create `tests/test_breaks.py`, `tests/test_protected_sentence_split.py`; modify `tests/test_normalization.py`, `tests/test_production_defaults.py`, `tests/test_chunk_manager.py`, `tests/test_text_studio.py`.

---

## Milestone A — Rule-based (không thêm dependency)

### Task 1: Tách câu có bảo vệ mẫu (fix mục 8.3)

**Files:**
- Modify: `app/chunker.py:6,21-23`
- Test: `tests/test_protected_sentence_split.py` (create)

**Interfaces:**
- Produces: `_split_paragraph_into_sentences(paragraph: str) -> list[str]` — hành vi mới: không tách bên trong mẫu bảo vệ.
- Produces: `_PROTECTED_PATTERNS: list[re.Pattern]` (module-level, testable).

- [ ] **Step 1: Failing tests**

```python
def test_no_split_inside_abbreviation():
    text = "Ông làm việc tại TP.HCM. Sau đó ông về Q.1."
    parts = _split_paragraph_into_sentences(text)
    assert parts[0] == "Ông làm việc tại TP.HCM."
    assert len(parts) == 2

def test_no_split_on_titles_and_decimals():
    text = "GS.TS Nguyễn Văn A trình bày 1.5 điểm. Xong."
    assert _split_paragraph_into_sentences(text) == [
        "GS.TS Nguyễn Văn A trình bày 1.5 điểm.", "Xong.",
    ]
```

- [ ] **Step 2: Implement placeholder technique**

Trong `chunker.py`: biên dịch `_PROTECTED_PATTERNS` theo whitelist của tài liệu (`toi_uu_tts.md` mục 4.1, mở rộng thêm `QĐ-UBND`, `NĐ-CP`, URL/email đã được xử lý riêng ở tầng khác nhưng vẫn giữ mẫu phòng hờ): số thập phân `\d+[.,]\d+`, ngày `\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}`, viết tắt chức danh `(?:GS|PGS|TS|BS|ThS)\.(?:TS|BS|ThS)?`, địa danh `(?:TP|Q|P|TT|H)\.\w+`. Trước khi split: thay từng vùng khớp bằng placeholder `\x00{n}\x00`, split bằng regex cũ, rồi trả lại nội dung.

- [ ] **Step 3: Verify**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_protected_sentence_split.py tests/test_chunk_manager.py -q
```

Chạy thêm `python scripts/test_repo_and_chunker.py <epub>` nếu có epub mẫu.

### Task 2: Expansion viết tắt trong normalization (fix mục 5.3, 8.2)

**Files:**
- Modify: `app/text_analysis.py:71` (export dict), `app/normalization.py` (áp dụng)
- Test: `tests/test_normalization.py`

**Interfaces:**
- Produces: `ABBREVIATION_EXPANSIONS: dict[str, str]` public ở `text_analysis` (rename từ `_ABBREVIATION_EXPANSIONS`, cập nhật tham chiếu nội bộ).
- Produces: `expand_abbreviations(text: str) -> str` ở `normalization.py`.
- Consumes: `NormalizationOptions.abbreviations: bool = True` (field mới).

- [ ] **Step 1: Failing tests** — `"UBND thành phố xử lý vụ việc."` → chứa `"Ủy ban nhân dân thành phố..."`; `"TP.HCM"` → `"Thành phố Hồ Chí Minh"`; bật/tắt được qua `opts.abbreviations`; không đụng `"U.S.A"` dạng acronym chưa có trong dict.

- [ ] **Step 2: Implement** — di chuyển regex `_ABBREVIATION_RE` sang `text_analysis.expand_abbreviations` (match dài-trước-ngắn đã có sẵn), gọi trong `normalize_text` ngay sau `remove_dots_in_vietnamese_words`, trước `normalize_numbers` (để `"Q.1"` kịp biến thành `"Quận 1"` trước khi số hóa). Case-preserving không cần thiết — dict đã chứa dạng hoa đúng.

- [ ] **Step 3: Verify** — `pytest tests/test_normalization.py tests/test_normalization_routes.py tests/test_text_studio.py -q`

### Task 3: Break predictor rule-based + cue renderer (mục 3 Cách 1, mục 6)

**Files:**
- Create: `app/breaks.py`
- Test: `tests/test_breaks.py` (create)

**Interfaces:**
- `class BreakPredictor(Protocol): predict(tokens: list[str]) -> list[int]` — nhãn B0..B3 theo bảng mục 6 (`B1: 80–150ms tương đương`, ...).
- `class RuleBasedBreakPredictor` — rule theo tài liệu: sau trạng ngữ đầu câu; trước liên từ `nhưng, tuy nhiên, vì vậy, nên, do, vì, mà, rồi, và` (khoảng cách từ ≥ 5 âm tiết); sau cụm ≥ 4 âm tiết có dấu hai chấm/ngoặc; giới hạn tần suất 1 cue / ~8 âm tiết.
- `render_break_cues(text: str, labels: list[int]) -> str` — map: B1→`,` B2→`;` B3→`...`; bỏ qua nếu vị trí đã kề dấu câu; không chèn sau từ cuối câu.
- `insert_break_cues(text: str, predictor=None) -> str` — façade dùng bởi normalization/repository.

- [ ] **Step 1: Failing tests**

```python
def test_fronted_adverbial_gets_comma():
    assert insert_break_cues("Ngày mai chúng ta sẽ họp.") == "Ngày mai, chúng ta sẽ họp."

def test_conjunction_gets_pause():
    out = insert_break_cues("Trời mưa rất to nhưng chúng tôi vẫn đi học đầy đủ.")
    assert "to," in out or "to;" in out

def test_no_double_punctuation():
    assert insert_break_cues("Đã có dấu phẩy, ở đây.") == "Đã có dấu phẩy, ở đây."

def test_idempotent():
    once = insert_break_cues("Câu dài có nhiều âm tiết để thử heuristic ngắt nghỉ.")
    assert insert_break_cues(once) == once
```

- [ ] **Step 2: Implement** — tokenizer nhẹ: tách theo khoảng trắng giữ dấu câu; đếm âm tiết = số token tiếng Việt (syllable). Rule đánh label, render chèn cue, chạy 2 lần để đảm bảo idempotent (lần 2 phát hiện cue cũ là dấu câu nên bỏ qua).

- [ ] **Step 3: Verify** — `pytest tests/test_breaks.py -q`

### Task 4: Cắm predictor vào pipeline + cấu hình + UI

**Files:**
- Modify: `app/normalization.py:235-256` (gọi `insert_break_cues` khi `opts.breaks`), `app/production_defaults.py:413-427`, `app/repository.py:1112-1121,1155-1178`, `app/routes/text_studio.py:229-235`, `app/routes/books.py:1139-1145`
- Modify: `frontend/src` Text Studio normalize panel + settings normalization section
- Test: `tests/test_normalization.py`, `tests/test_production_defaults.py`, `tests/test_text_studio.py`, `tests/test_chunk_manager.py`

**Interfaces:**
- `NormalizationOptions` thêm: `abbreviations: bool = True`, `breaks: bool = True` (Task 2 đã thêm abbreviations).
- `get_effective_normalization_config` trả thêm 2 key; sách cũ không có key → default như trên (không migration).
- Preview routes nhận form field `abbreviations`/`breaks`.

- [ ] **Step 1: Failing tests** — `build_chunk_plan_from_inputs` với chapter chứa `"Trời mưa nhưng vẫn đến lớp."` tạo chunk text mang dấu phẩy sau mệnh đề đầu khi `breaks=True`; `breaks=False` giữ nguyên; config group roundtrip qua `set_.../get_effective_normalization_config`.
- [ ] **Step 2: Implement backend plumbing** (thứ tự: production_defaults → repository → routes). Đảm bảo `split_into_tts_chunks` chạy SAU khi chèn cue và cue không làm chunk vượt `max_chars` (nếu vượt, greedy packing tự tách — chấp nhận).
- [ ] **Step 3: UI** — 2 checkbox mới (mặc định bật cho `abbreviations`, `breaks`; `word_segmentation` chỉ hiện khi Milestone B xong). Build SPA: `npm run build`.
- [ ] **Step 4: Verify toàn tuyến**

```bash
./.venv/Scripts/python.exe -m pytest tests/test_normalization.py tests/test_production_defaults.py tests/test_chunk_manager.py tests/test_text_studio.py tests/test_normalization_routes.py -q
```

Smoke: upload epub mẫu → build patch → kiểm tra `manifest.json` chunk có cue.

---

## Milestone B — Ngôn ngữ học (optional, sau khi A ổn)

### Task 5: Extra `prosody` + UndertheseaBreakPredictor (mục 4.2, 4.3)

**Files:**
- Modify: `pyproject.toml` (extra `prosody = ["underthesea>=6.8"]`)
- Create: `app/linguistic_breaks.py`
- Modify: `app/breaks.py` (`get_predictor(opts)` factory: chọn theo khả dụng + opts)
- Test: `tests/test_breaks.py` (skip-if-not-installed)

**Interfaces:**
- `UnderthereaBreakPredictor.predict` dùng `underthesea.pos_tag` + `word_sent`: ranh giới cụm từ (token có `_`) là nơi cấm ngắt; sau NP dài/Ngữ trạng thái → B1/B2; ưu tiên hơn rule-based khi installed.
- Factory `get_predictor(word_segmentation: bool)`: `False` → rule; `True` + thiếu package → log warning 1 lần, fallback rule.

- [ ] **Step 1:** failing test với monkeypatched `pos_tag` (không yêu cầu package thật khi CI chưa cài).
- [ ] **Step 2:** implement lazy import trong try/except; cache instance module-level.
- [ ] **Step 3:** bật checkbox `word_segmentation` trong UI (disable + tooltip nếu chưa cài extra). Verify: `pip install -e ".[prosody]"` rồi chạy lại smoke Task 4.

### Task 6: A/B so sánh chất lượng (thủ công, có script)

**Files:**
- Create: `scripts/compare_break_strategies.py` — nhập đoạn văn, in ra 3 phiên bản (raw / rule / underthesea) + số cue, phục vụ nghe thử A/B.

- [ ] **Step 1:** script chạy offline không cần DB. **Step 2:** ghi kết quả nghe thử vào `docs/toi_uu_tts.md` phần cuối (bảng nhỏ: câu / nhận xét).

---

## Milestone C — Model-based (khung sẵn, không bật)

### Task 7: Skeleton PhoBERT predictor (mục 3 Cách 3, mục 7)

**Files:**
- Create: `app/model_breaks.py`
- Test: `tests/test_breaks.py::test_factory_falls_back_without_model`

**Interfaces:**
- `PhoBERTBreakPredictor(BreakPredictor)` — lazy load từ `vinai/phobert-base` + head phân loại 4 lớp; weights tìm trong `data/models/break-phobert/`, thiếu → `ModelNotAvailableError` → factory fallback.
- Không thêm `transformers` vào pyproject ở giai đoạn này; docstring ghi rõ cách huấn luyện (fine-tune token classification, nhãn từ punctuation restoration như mục 7.2) và nguồn dữ liệu (WhisperX/MFA forced alignment — mục 7.1) khi có audio thật.

- [ ] **Step 1:** test factory fallback không cần tải model. **Step 2:** implement class + wiring vào factory phía sau flag env `BREAK_MODEL_DIR`. Dừng ở đây — huấn luyện thật là plan riêng.

---

## Verification cuối plan

```bash
./.venv/Scripts/python.exe -m pytest tests/test_breaks.py tests/test_protected_sentence_split.py tests/test_normalization.py tests/test_production_defaults.py tests/test_chunk_manager.py tests/test_text_studio.py tests/test_subtitle_gen.py -q
npm run build
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload   # smoke: normalize-preview + generate 1 patch
```

Tiêu chí chấp nhận: không test nào fail; epub mẫu sinh audio có ngắt nghe tự nhiên hơn tại mệnh đề (nghe thử A/B Task 6); batch export manifest mang text đã có cue; tắt hết flag mới → output byte-for-byte giống nhánh main.
