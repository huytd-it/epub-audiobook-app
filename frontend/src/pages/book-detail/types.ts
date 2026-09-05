import { Book, Chapter, DriveAccount, DriveTarget, KaggleAccount, Patch, PatchExport } from "@/api";

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
  capabilities: { kind: string; runtime?: "local" | "api"; provider?: string; voice_selection: boolean };
  configured?: boolean;
  config_hint?: string;
  /** Giọng có sẵn trong chính model (ZeroTTS). Rỗng với model clone theo audio mẫu
   * và với backend cloud — hai loại đó lấy giọng từ nơi khác. */
  voices?: OnlineVoice[];
  options_schema?: TtsOptionField[];
};

export type TtsOptionField = {
  key: string;
  label: string;
  type: "number" | "select";
  default: string | number;
  min?: number;
  max?: number;
  step?: number;
  choices?: { value: string; label: string }[];
};

export type OnlineVoice = { id: string; label: string; language: string };
export type VoiceOption = { value: string; label: string };

/** Giọng preset của các model cast cố định (VieNeu, ZeroTTS) đưa sang cho model clone
 *  (VoxCPM, OmniVoice) dùng làm audio mẫu: app tự đọc một câu bằng giọng preset rồi lấy
 *  file đó làm reference. `value` phải đúng dạng "preset:<engine>:<voice>" mà backend
 *  (app/tts_engine.parse_preset_voice) hiểu — filename trong thư viện không bao giờ có
 *  dạng này nên hai nguồn giọng đứng chung dropdown được. */
export function presetVoiceOptions(models: TtsModel[]): VoiceOption[] {
  return models
    .filter((model) => !model.supports_reference && (model.voices?.length || 0) > 0)
    .flatMap((model) =>
      (model.voices || []).map((voice) => ({
        value: `preset:${model.id}:${voice.id}`,
        label: `${model.name} · ${voice.label || voice.id}`,
      }))
    );
}

export type ExportContext = {
  exports: PatchExport[];
  sync_targets: DriveTarget[];
  accounts: DriveAccount[];
  kaggle_accounts: KaggleAccount[];
};

export type UploadResult = import("@/api").UploadResult;

/** Trạng thái pipeline patch từ /books/{id}/status. Các trường bổ sung của bản
 * backend mới (attempts, next_retry_at, video_path...) được để optional để trang
 * vẫn hoạt động với mọi phiên bản API. */
export type PipelineInfo = {
  stage: string;
  thumbnail_status: string;
  video_status: string;
  upload_status: string;
  playlist_status: string;
  thumbnail_path: string | null;
  youtube_upload_id: number | null;
  youtube_video_id?: string | null;
  last_error: string | null;
  upload_state: string;
  can_force_new: boolean;
  attempt_count?: number;
  video_path?: string | null;
  video_id?: number | null;
  next_retry_at?: string | null;
};

/** Giai đoạn pipeline bị chặn (cần thao tác của người dùng thay vì chờ worker). */
export const BLOCKED_STAGES = [
  "auth_required",
  "waiting_for_audio",
  "waiting_for_media",
  "retry_wait",
];

export function stageBlockedReason(stage: string | undefined): string | null {
  if (!stage) return null;
  if (stage === "auth_required") return "Cần kết nối lại YouTube";
  if (stage === "waiting_for_audio") return "Đang chờ audio của patch";
  if (stage === "waiting_for_media") return "Đang chờ media nền hợp lệ";
  if (stage === "retry_wait") return "Đang chờ thời điểm thử lại";
  return null;
}

/** Nhãn hiển thị cho publish_status trả về từ inbox / upload kết quả. */
export const PUBLISH_STATUS_LABEL: Record<string, string> = {
  queued: "Đã đưa vào hàng đợi YouTube",
  pending: "Chờ pipeline đăng",
  skipped_already_published: "Đã đăng trước đó — bỏ qua",
  skipped_youtube_not_ready: "YouTube chưa sẵn sàng (auto-upload bật)",
  skipped_auto_upload_disabled: "Đã nhận audio — auto-upload đang tắt",
  blocked_active_pipeline: "Bị chặn: patch đang dựng hoặc upload video",
  enqueue_failed: "Đã nhận audio — lỗi khi đưa vào hàng đợi đăng",
};

export type BookStatus = {
  book_status: string;
  has_final_audio: boolean;
  has_active_patches: boolean;
  pipelines: Record<string, PipelineInfo>;
  patches: { id: number; status: string; chunk_count: number; next_chunk_index: number }[];
};

export type GameplaySelectionConfig = {
  selection_mode: "single" | "rotation";
  game_id: "snake_arena" | "brick_stack" | "tank_duel" | "brick_breaker" | "star_defender" | "pixel_dash" | "pacman_maze" | "chicken_shooter" | "spaceship_voyager" | "flappy_bird" | "gold_miner" | "aurora_veil" | "plasma_tide" | "ripple_pond" | "lumen_bloom" | "silk_current" | "starfall_warp";
  game_ids: ("snake_arena" | "brick_stack" | "tank_duel" | "brick_breaker" | "star_defender" | "pixel_dash" | "pacman_maze" | "chicken_shooter" | "spaceship_voyager" | "flappy_bird" | "gold_miner" | "aurora_veil" | "plasma_tide" | "ripple_pond" | "lumen_bloom" | "silk_current" | "starfall_warp")[];
  preset: "calm";
};

export type VideoConfig = {
  background_type: "media" | "gameplay";
  backgrounds: string[];
  background_mode: "sequential" | "random";
  gameplay: GameplaySelectionConfig;
  image_duration_seconds: number;
  /** Nhạc nền chỉ chèn vào khoảng lặng của giọng đọc thay vì lặp suốt patch. */
  music_gap_only: boolean;
  /** Khoảng lặng tối thiểu (ms) mới được chèn nhạc. */
  music_gap_min_ms: number;
  /** Fade in/out (ms) ở hai đầu mỗi đoạn nhạc chèn vào. */
  music_gap_fade_ms: number;
  resolution: string;
  fps: number;
  image_animation: string;
  fit_mode: "auto" | "contain" | "cover" | "blur";
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
  waveform_enabled: boolean;
  waveform_style: "line" | "cline" | "p2p" | "point";
  waveform_color: string;
  waveform_position: "top" | "center" | "bottom";
  waveform_height: number;
  waveform_opacity: number;
  waveform_layout: "horizontal" | "vertical" | "circular";
  waveform_background_color: string;
  waveform_background_opacity: number;
  subtitle_enabled: boolean;
  subtitle_font_size: number;
  subtitle_color: string;
  subtitle_position: "top" | "center" | "bottom";
};

/** Khối nội dung mở rộng nối vào cuối description (bản quyền, miễn trừ AI,
 * nguồn truyện, fair use). Dòng nào chứa placeholder chưa điền sẽ bị bỏ. */
export type DescriptionExtra = {
  enabled: boolean;
  contact_email: string;
  story_title: string;
  story_source_name: string;
  story_source_url: string;
  fair_use_url: string;
  template: string;
};

export const DESCRIPTION_EXTRA_FIELDS: { key: keyof DescriptionExtra & string; label: string; hint: string }[] = [
  { key: "contact_email", label: "Email liên hệ bản quyền", hint: "{contact_email}" },
  { key: "story_title", label: "Tên truyện", hint: "{story_title}" },
  { key: "story_source_name", label: "Nguồn truyện", hint: "{story_source_name}" },
  { key: "story_source_url", label: "Link nguồn truyện", hint: "{story_source_url}" },
  { key: "fair_use_url", label: "Link chính sách Fair Use", hint: "{fair_use_url}" },
];

export type YouTubeConfig = {
  auto_upload: boolean;
  title_template: string;
  description: string;
  genre_tags: string;
  privacy_status: string;
  timeline_enabled: boolean;
  description_extra: DescriptionExtra;
  playlist: { mode: string; playlist_id: string; title_template: string; description_template: string };
  podcast: PodcastConfig;
  auto_ai_labels: boolean;
  playlist_sort_mode: "manual" | "natural" | "episode";
  auto_sort_episode: boolean;
};

/** Cài đặt podcast: playlist của sách được YouTube đánh dấu là podcast và nhận
 * ảnh bìa vuông 1:1 cắt ra từ chính thumbnail. */
export type PodcastConfig = {
  enabled: boolean;
  upload_cover: boolean;
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

export type ConfigTab = "audio" | "normalization" | "video" | "youtube" | "branding";

export type ProductionGroup = ConfigTab;
export type ProductionMode = "inherit" | "custom";
export type ProductionSettings = {
  schema_version: number;
  updated_at: string | null;
  defaults: {
    audio: {
      model_id: string;
      voice_id: string;
      max_chars: number;
      with_effects: boolean;
      tts_options: Record<string, string | number>;
      /** Khoảng lặng (ms) chèn giữa hai chunk liền nhau. */
      chunk_pause_ms: number;
      /** Khoảng lặng (ms) chèn trước chunk mở đầu một chương mới. */
      chapter_pause_ms: number;
    };
    normalization: NormalizationSettings;
    video: VideoConfig;
    youtube: YouTubeConfig;
    branding: BrandingConfig;
  };
  book_id?: number;
  modes?: Record<ProductionGroup, ProductionMode>;
  effective?: ProductionSettings["defaults"];
};

export type NormalizationSettings = {
  numbers: boolean;
  junk: boolean;
  spellcheck: boolean;
  dictionary: boolean;
  transliteration: boolean;
  abbreviations: boolean;
  breaks: boolean;
};

export type BrandingPosition = "top-left" | "top-right" | "bottom-left" | "bottom-right" | "center";

export type BrandingWatermark = {
  enabled: boolean;
  text: string;
  position: BrandingPosition;
  font_size: number;
  text_color: string;
  opacity: number;
  margin: number;
  shadow_enabled: boolean;
  shadow_color: string;
};

export type BrandingLogo = {
  enabled: boolean;
  path: string;
  position: BrandingPosition;
  size: number;
  opacity: number;
  margin: number;
};

export type BrandingTargets = {
  thumbnail: boolean;
  podcast: boolean;
  video: boolean;
};

export type BrandingConfig = {
  watermark: BrandingWatermark;
  logo: BrandingLogo;
  targets: BrandingTargets;
};

export type OverlayShadow = {
  enabled: boolean;
  color: string;
  offset: number;
};

export type OverlayBox = {
  enabled: boolean;
  color: string;
  opacity: number;
  padding_x: number;
  padding_y: number;
  radius: number;
};

export type OverlayLayer = {
  text: string;
  position: "top" | "center" | "bottom";
  alignment: "left" | "center" | "right";
  font_size: number;
  font_path: string;
  text_transform: "none" | "uppercase" | "lowercase" | "titlecase";
  line_spacing: number;
  max_width: number;
  stroke_width: number;
  stroke_color: string;
  text_color: string;
  margin: number;
  offset_x: number;
  offset_y: number;
  shadow: OverlayShadow;
  box: OverlayBox;
};

export type OverlayConfig = {
  text: string;
  position: "top" | "center" | "bottom";
  alignment: "left" | "center" | "right";
  font_size: number;
  font_path: string;
  text_transform: "none" | "uppercase" | "lowercase" | "titlecase";
  line_spacing: number;
  max_width: number;
  stroke_width: number;
  stroke_color: string;
  text_color: string;
  shadow: OverlayShadow;
  box: OverlayBox;
  margin: number;
  offset_x: number;
  offset_y: number;
  overlays: OverlayLayer[];
  podcast_cover: PodcastCover;
};

/** Khung cắt 1:1 dùng cho ảnh bìa podcast — cùng artwork với thumbnail. */
export type PodcastCover = {
  enabled: boolean;
  /** Tâm khung cắt theo chiều ngang, 0–100 (%). */
  focus_x: number;
  /** Tâm khung cắt theo chiều dọc, 0–100 (%). */
  focus_y: number;
  /** Cạnh ảnh vuông xuất ra (px). */
  size: number;
};

export const PODCAST_COVER_SIZES = [800, 1080, 1280, 1600, 2048] as const;

export type FontDetail = {
  name: string;
  path: string;
};

export type OverlayConfigResponse = {
  config: OverlayConfig;
  fonts: FontDetail[];
  backgrounds: BackgroundItem[];
  background_path?: string | null;
  placeholders: { key: string; label: string }[];
};

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
  /** True khi preview/import giới hạn theo khoảng mục trong file mới. */
  ranged?: boolean;
  parsed_start?: number | null;
  parsed_end?: number | null;
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

/** Một luật tìm/thay áp cho cả sách, chạy sau bước chuẩn hóa TTS. */
export type ReplaceRule = {
  id: number;
  book_id: number;
  find: string;
  replace: string;
  is_regex: boolean;
  position: number;
};

export type ReplaceRuleResult = { rule: ReplaceRule; reset_patches: number };
export type ReplaceRuleDeleteResult = { deleted: boolean; reset_patches: number };

/** GET /books/{id}/text-studio/patches/{patchId} — text hiệu lực của patch. */
export type PatchTextPayload = { text: string; warnings: TextWarning[]; is_edited: boolean };

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
  ttsOptions: Record<string, string | number>;
  /** Chỉ dùng cho cấu hình TTS của sách (export không có khoảng lặng riêng). */
  chunkPauseMs?: string;
  chapterPauseMs?: string;
};

export type BackgroundItem = { name: string; path: string; is_video: boolean; is_default?: boolean };
export type AudioSettingsResponse = AudioSettings;
export type MusicSettings = { music_id: number | null; music_volume: number };

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
