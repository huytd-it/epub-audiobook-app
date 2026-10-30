/** Đường dẫn kho media của một sách, bám theo layout hiện hành mà backend sinh ra
 * (app/repository.py — get_patch_audio_path/get_patch_chunk_dir/get_patch_video_path):
 *
 *   data/books/{book_id}/audio/{book_id}_{episode}.wav   (+ .timeline.json, .ass)
 *   data/books/{book_id}/audio/{book_id}_{episode}_chunks/   (metadata + chunk Text Studio)
 *   data/books/{book_id}/chapters/{chapter_no}/chunk_NNN.wav (+ chunks.json)
 *   data/books/{book_id}/videos/{book_id}_{episode}.mp4
 *
 * episode = patch_index + 1, đệm 0 cho đủ 3 chữ số — khoá theo *thứ tự patch*, không
 * phải patch_id. chapter_no = chapter_index + 1, đệm 4 chữ số (app/chunk_store.py):
 * chunk TTS sống theo chương nên build lại patch không làm mất chúng. Layout cũ (patches/{patch_id}_chunks, patch_videos/{patch_id}.mp4)
 * chỉ còn được đọc để tương thích dữ liệu tồn đọng; UI không sinh path theo nó nữa.
 *
 * Chuỗi trả về là "qualified path" của media browser, nên bắt đầu bằng alias gốc
 * `_Sách` (= data/books, xem app/routes/media_browser.py::_build_root_entries).
 */
import { Patch } from "@/api";

export const BOOKS_ROOT = "_Sách";

/** Số tập của patch trong tên file: patch_index 0 → "001". */
export const episodeOf = (patchIndex: number) => String(patchIndex + 1).padStart(3, "0");

/** Thư mục audio của sách — chứa wav/timeline/ass và các thư mục *_chunks. */
export const bookAudioDir = (bookId: number | string) => `${BOOKS_ROOT}/${bookId}/audio`;

/** Thư mục video của sách theo layout mới (không còn patch_videos/). */
export const bookVideoDir = (bookId: number | string) => `${BOOKS_ROOT}/${bookId}/videos`;

/** Kho chunk TTS theo chương của sách. */
export const bookChaptersDir = (bookId: number | string) => `${BOOKS_ROOT}/${bookId}/chapters`;

/** Thư mục chunk WAV của một chương: chapter_index 0 → "0001". */
export const chapterChunkDir = (bookId: number | string, chapterIndex: number) =>
  `${bookChaptersDir(bookId)}/${String(chapterIndex + 1).padStart(4, "0")}`;

/** Thư mục metadata TTS (+ chunk Text Studio) của một patch. */
export const patchChunkDir = (bookId: number | string, patchIndex: number) =>
  `${bookAudioDir(bookId)}/${bookId}_${episodeOf(patchIndex)}_chunks`;

/** Nơi mở media browser cho một patch: thư mục audio của sách (wav/timeline/ass của
 * patch). Chunk WAV giờ nằm theo chương — xem chapterChunkDir / tab Mục lục. */
export const patchMediaDir = (patch: Patch) => bookAudioDir(patch.book_id);
