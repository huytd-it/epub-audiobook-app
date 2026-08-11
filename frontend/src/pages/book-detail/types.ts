import { Book, Chapter, DriveAccount, DriveTarget, Patch, PatchExport } from "@/api";

export type PlannedPatch = {
  patch_index: number;
  chapter_start: number;
  chapter_end: number;
  chapter_no_start?: number | null;
  chapter_no_end?: number | null;
  name: string;
  chunk_count: number;
};

export type BookRecord = Book & {
  final_audio_path?: string | null;
  final_video_path?: string | null;
  voice_clip_path?: string | null;
  automation_config?: string | null;
};

export type Detail = {
  book: BookRecord;
  patches: Patch[];
  chapters: Chapter[];
  last_error: unknown;
  tts_models?: TtsModel[];
};

export type TtsModel = {
  id: string;
  name: string;
  model_id: string;
  supports_reference: boolean;
  default_voice: string | null;
  capabilities: { kind: string; voice_selection: boolean };
};

export type OnlineVoice = { id: string; label: string; language: string };
export type VoiceOption = { value: string; label: string };

export type ExportContext = {
  exports: PatchExport[];
  sync_targets: DriveTarget[];
  accounts: DriveAccount[];
};

export type UploadResult = { filename: string; status: string; detail?: string };

export type PipelineInfo = {
  stage: string;
  video_status: string;
  upload_status: string;
  upload_state: string;
  youtube_upload_id: number | null;
  last_error: string | null;
};

export type BookStatus = {
  book_status: string;
  has_final_audio: boolean;
  has_active_patches: boolean;
  pipelines: Record<string, PipelineInfo>;
  patches: { id: number; status: string; chunk_count: number; next_chunk_index: number }[];
};

export type VideoConfig = {
  backgrounds: string[];
  background_mode: string;
  image_duration_seconds: number;
  resolution: string;
  fps: number;
  image_animation: string;
  codec: string;
  audio_bitrate: string;
  quality: number;
  concurrency: number;
  intro_voice: string;
  outro_voice: string;
  crossfade_enabled: boolean;
  crossfade_seconds: number;
  ken_burns_enabled: boolean;
  progress_bar_enabled: boolean;
};

export type YouTubeConfig = {
  auto_upload: boolean;
  title_template: string;
  description: string;
  genre_tags: string;
  privacy_status: string;
  playlist: { mode: string; playlist_id: string; title_template: string; description_template: string };
};

export type YouTubeSettings = {
  config: YouTubeConfig;
  connected: boolean;
  channel_name: string | null;
  playlists: { id: string; title: string }[];
};

export type YouTubeMetadataPreview = {
  title: string;
  description: string;
  tags: string[];
  privacy_status: string;
  youtube: { mode: string; playlist_id: string };
};

export type ConfigTab = "audio" | "video" | "youtube";

// --- Chapter validation / analysis --------------------------------------------

export type Severity = "ok" | "info" | "warning" | "error";
export type TitleState = "canonical" | "fixable" | "no_name" | "unknown";
export type NumberingFlag = "gap_before" | "duplicate" | "out_of_order" | "unnumbered" | null;

export type Issue = { code: string; severity: Severity; message: string; count: number };

export type ChapterReport = {
  chapter_index: number;
  title: string;
  chapter_no: number | null;
  char_count: number;
  is_excluded: boolean;
  severity: Severity;
  is_valid: boolean;
  issues: Issue[];
  title_state: TitleState;
  suggested_title: string | null;
  numbering_flag?: NumberingFlag;
};

export type PatchReport = {
  patch_id: number;
  patch_index: number;
  chapter_start: number;
  chapter_end: number;
  chunk_count: number;
  total_chars: number;
  max_chunk_chars: number;
  oversized_chunks: number;
  empty_chunks: number;
  unspeakable_chunks: number;
  chapter_count: number;
  invalid_chapters: number[];
  severity: Severity;
  is_valid: boolean;
  issues: Issue[];
};

export type Numbering = {
  numbered_count: number;
  unnumbered_count: number;
  first_number: number | null;
  last_number: number | null;
  missing_numbers: number[];
  missing_count: number;
  duplicate_numbers: Record<string, number[]>;
  duplicate_count: number;
  out_of_order_indices: number[];
  is_continuous: boolean;
};

export type TitleCounts = { canonical: number; fixable: number; no_name: number; unknown: number };

export type ChaptersValidation = {
  book_id: number;
  max_chars: number;
  summary: {
    chapters_total: number;
    chapters_error: number;
    chapters_warning: number;
    chapters_ok: number;
    chapters_excluded: number;
    issue_totals: Record<string, number>;
  };
  numbering: Numbering;
  titles: TitleCounts;
  chapters: ChapterReport[];
};

export type Span = { start: number; length: number; code: string; severity: Severity; label: string; excerpt: string };

export type ChapterPatchSummary = {
  patch_id: number;
  patch_index: number;
  name: string;
  status: string;
  has_clean_text: boolean;
  chunk_count: number;
};

export type ChapterDetail = {
  id: number;
  chapter_index: number;
  title: string;
  text: string;
  char_count: number;
  is_excluded: boolean;
  chapter_no: number | null;
  title_state: TitleState;
  suggested_title: string | null;
  max_chars: number;
  report: ChapterReport;
  spans: Span[];
  span_totals: Record<string, number>;
  patches: ChapterPatchSummary[];
};

export type ChapterSaveResult = {
  ok: boolean;
  title: string;
  text: string;
  char_count: number;
  is_excluded: boolean;
  chapter_no: number | null;
  title_state: TitleState;
  suggested_title: string | null;
  report: ChapterReport;
  spans: Span[];
  span_totals: Record<string, number>;
  patches: ChapterPatchSummary[];
  patches_recomputed: { patch_id: number; patch_index: number; chunk_count: number }[];
};

export type ChapterAnalyzeResult = {
  report: ChapterReport;
  spans: Span[];
  span_totals: Record<string, number>;
  title_state: TitleState;
  suggested_title: string | null;
};

export type ReimportPlan = {
  existing_count: number;
  parsed_count: number;
  matched_count: number;
  changed: { chapter_index: number; chapter_no: number | null; title: string; old_char_count: number; new_char_count: number }[];
  added: { chapter_no: number | null; title: string; char_count: number }[];
  removed: { chapter_index: number; title: string }[];
  next_chapter_index: number;
};

export type ExtendPlan = {
  patches: { patch_index: number; chapter_start: number; chapter_end: number; name: string; chunk_count: number }[];
  uncovered_chapters: number;
};

// --- Patch range + text quality ------------------------------------------------

export type PatchRangeReport = {
  patch_id: number;
  patch_index: number;
  name: string;
  status: string;
  chapter_start: number;
  chapter_end: number;
  chapter_count: number;
  stored_no_start: number | null;
  stored_no_end: number | null;
  actual_no_start: number | null;
  actual_no_end: number | null;
  unnumbered_count: number;
  severity: Severity;
  is_valid: boolean;
  issues: Issue[];
};

export type PatchRangesReport = {
  book_id: number;
  summary: {
    patches_total: number;
    patches_error: number;
    patches_warning: number;
    patches_ok: number;
    needs_resync: number;
    issue_totals: Record<string, number>;
  };
  patches: PatchRangeReport[];
};

export type TextWarning = {
  kind: string;
  position: number;
  length: number;
  original: string;
  suggestion: string;
  context: string;
  context_offset: number;
};

export type PatchTextCheck = {
  patch_id: number;
  patch_index: number;
  name: string;
  chars: number;
  totals: Record<string, number>;
  total: number;
  items: TextWarning[];
};

export type PatchTextCheckSummary = {
  book_id: number;
  patches: { patch_id: number; patch_index: number; totals: Record<string, number>; total: number }[];
};

export type PlannedRangeCheck = {
  planned: number;
  issues: Issue[];
  has_error: boolean;
};

export type TitleNormalizeItem = { chapter_index: number; current: string; suggested: string; chapter_no: number | null };
export type TitleNormalizeSkipped = { chapter_index: number; title: string; reason: TitleState };
export type TitleNormalizePreview = {
  total: number;
  fixable: number;
  skipped: number;
  items: TitleNormalizeItem[];
  skipped_items: TitleNormalizeSkipped[];
};

/** Thiết lập TTS dùng chung cho export, tạo audio và hộp thoại cấu hình. */
export type AudioSettings = {
  modelId: string;
  voiceId: string;
  maxChars: string;
  withEffects: boolean;
};

export function errorText(error: unknown) {
  return error instanceof Error ? error.message : "Đã xảy ra lỗi không xác định.";
}

/** POST một FormData rồi lưu response về máy dưới dạng file. */
export async function downloadForm(url: string, form: FormData) {
  const response = await fetch(url, { method: "POST", body: form });
  if (!response.ok) {
    try {
      const body = await response.json();
      throw new Error(body.detail?.message || body.detail || `Lỗi ${response.status}`);
    } catch (error) {
      if (error instanceof Error) throw error;
      throw new Error(`Lỗi ${response.status}`);
    }
  }
  const header = response.headers.get("content-disposition") || "";
  const match = header.match(/filename="?([^";]+)"?/i);
  const anchor = document.createElement("a");
  anchor.href = URL.createObjectURL(await response.blob());
  anchor.download = match?.[1] || "patch-export.zip";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(anchor.href);
}
