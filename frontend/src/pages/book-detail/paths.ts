/** Đường dẫn kho media của một sách, bám theo layout hiện hành mà backend sinh ra
 * (app/repository.py — get_patch_audio_path/get_patch_chunk_dir/get_patch_video_path):
 *
 *   data/books/{book_id}/audio/{book_id}_{episode}.wav   (+ .timeline.json, .ass)
 *   data/books/{book_id}/audio/{book_id}_{episode}_chunks/
 *   data/books/{book_id}/videos/{book_id}_{episode}.mp4
 *
 * episode = patch_index + 1, đệm 0 cho đủ 3 chữ số — khoá theo *thứ tự patch*, không
 * phải patch_id. Layout cũ (patches/{patch_id}_chunks, patch_videos/{patch_id}.mp4)
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

/** Thư mục chunk WAV của một patch. */
export const patchChunkDir = (bookId: number | string, patchIndex: number) =>
  `${bookAudioDir(bookId)}/${bookId}_${episodeOf(patchIndex)}_chunks`;

/** Nơi mở media browser cho một patch: thư mục chunk khi patch đã có audio, ngược
 * lại lùi về thư mục audio của sách — thư mục chunk chưa tồn tại thì browse trả 404. */
export const patchMediaDir = (patch: Patch) =>
  patch.audio_path || patch.status === "done"
    ? patchChunkDir(patch.book_id, patch.patch_index)
    : bookAudioDir(patch.book_id);
