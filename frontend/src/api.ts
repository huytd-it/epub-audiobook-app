export type Book = {
  id: number;
  title: string;
  status: string;
  priority: number;
  original_filename: string;
  created_at: string;
  normalize_numbers_enabled?: number;
  normalize_junk_enabled?: number;
  normalize_spellcheck_enabled?: number;
  normalize_dictionary_enabled?: number;
  normalize_transliteration_enabled?: number;
  patches?: { total: number; done: number; active: number; failed: number };
};
export type Patch = { id: number; book_id: number; patch_index: number; chapter_start: number; chapter_end: number; chapter_no_start?: number | null; chapter_no_end?: number | null; name: string; status: string; chunk_count: number; next_chunk_index: number; error_message: string | null; audio_path?: string | null };
export type Chapter = {
  id: number;
  chapter_index: number;
  title: string;
  char_count: number;
  is_excluded: boolean;
  chapter_no?: number | null;
};
export type Job = {
  id: number;
  job_type: string;
  status: string;
  phase: string;
  percent: number;
  book_id: number | null;
  production_name: string | null;
  error_message: string | null;
  created_at: string;
  attempt_count?: number;
  max_attempts?: number;
  patch_id?: number | null;
  flow_run_id?: number | null;
  payload?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
};
export type Media = { music: Array<{ id: number; name: string; duration_sec: number | null; description?: string; license?: string }>; photos: Array<{ name: string; size: number; is_video: boolean }>; voices: Array<{ name: string; size: number; description?: string }> };

export type MusicItem = { id: number; name: string; duration_sec: number | null; description?: string; license?: string };
export type PhotoItem = { name: string; size: number; size_kb?: number; is_video: boolean };
export type VoiceItem = {
  name: string;
  size: number;
  size_kb?: number;
  description?: string;
  /** Gender slug from /voices/taxonomy; "" when unclassified. */
  gender?: string;
  /** Story-genre slugs from /voices/taxonomy. */
  genre?: string[];
};

/** One selectable {slug, label} pair from GET /voices/taxonomy. */
export type VoiceTag = { value: string; label: string };
export type VoiceTaxonomy = { genders: VoiceTag[]; genres: VoiceTag[] };

/** What ffprobe could read about one clip — shared by the voice and music
 *  libraries, which both feed the audio editor. */
export type AudioClipInfo = {
  name?: string;
  size?: number;
  duration_sec?: number | null;
  sample_rate?: number | null;
  channels?: number | null;
  codec?: string | null;
  bit_rate?: number | null;
};

/** GET /voices/{name}/info — clip metadata plus the probe. */
export type VoiceInfo = VoiceItem & AudioClipInfo;

/** GET /music/{id}/info — track metadata plus the probe. */
export type MusicInfo = MusicItem & AudioClipInfo;

/** Body of POST /voices/{name}/process and /music/{id}/process. Omitted fields
 *  mean "leave alone". */
export type AudioClipOps = {
  trim_start?: number;
  trim_end?: number | null;
  highpass?: boolean;
  lowpass?: boolean;
  denoise?: boolean;
  trim_silence?: boolean;
  normalize?: boolean;
  gain_db?: number;
  fade_in?: number;
  fade_out?: number;
  mono?: boolean;
  sample_rate?: number | null;
};

/** Result of either library's /process: the re-probed clip plus Vietnamese
 *  labels for what was applied. */
export type AudioProcessResult = AudioClipInfo & { applied: string[] };

export type EffectItem = { id: number; marker: string; file_path: string; description: string };

export type YouTubeUploadItem = {
  id: number;
  video_path: string;
  title: string;
  description: string;
  tags: string[];
  privacy_status: string;
  playlist_id?: string;
  status: string;
  youtube_video_id?: string;
  error_message?: string;
  created_at: string;
};

/** One row of the editable upload sheet from GET /youtube/uploads/export. */
export type YouTubeUploadRecord = {
  id: number | string;
  title: string;
  description: string;
  tags: string;
  privacy_status: string;
  playlist_id: string;
  video_path: string;
  status: string;
  youtube_video_id: string;
  created_at: string;
};

/** Per-row verdict of POST /youtube/uploads/import. */
export type YouTubeImportRow = {
  row: number;
  id: number | string | null;
  status: "updated" | "created" | "unchanged" | "skipped" | "error";
  changes: Record<string, unknown>;
  message: string;
  warning: string;
};

export type YouTubeImportSummary = {
  mode: string;
  dry_run: boolean;
  total: number;
  counts: Record<string, number>;
  results: YouTubeImportRow[];
};

export type PlaylistItem = {
  id: string;
  title: string;
  description: string;
  itemCount: number;
  privacy: string;
  thumbnail?: string;
};

export type PlaylistItemDetail = {
  playlist_item_id: string;
  playlist_id: string;
  video_id: string;
  title: string;
  thumbnail: string;
  position: number;
  published_at: string;
};

export type ChannelVideo = {
  video_id: string;
  title: string;
  thumbnail: string;
  published_at: string;
};

export type DriveTarget = {
  id: number;
  name: string;
  account_email: string;
  folder_path: string;
  rclone_remote: string | null;
  created_at: string;
};

export type DriveAccount = {
  id: number;
  account_email: string;
  created_at: string;
};

export type DriveClient = {
  id: number;
  name: string;
  client_id: string;
  client_secret: string;
  created_at: string;
};

/** Kết quả xử lý từng patch từ inbox / upload kết quả hàng loạt. */
export type UploadResult = {
  patch_id?: number;
  patch_index?: number;
  patch_name?: string;
  filename: string;
  status: "ok" | "error" | "skipped" | string;
  detail?: string;
  audio?: boolean;
  timeline?: string;
  publish_status?: string;
  job_id?: number | null;
  publish_error?: string | null;
};

export type InboxStatus = {
  path: string;
  files: string[];
  count: number;
};

export type InboxProcessResult = {
  ok: boolean;
  installed: number;
  renamed: { from: string; to: string }[];
  results: UploadResult[];
  path: string;
  auto_upload?: boolean;
  publish_ready?: boolean;
  publish_warning?: string | null;
};

export type TtsGenerateResult = {
  queued: number;
  auto_create_video: boolean;
  auto_upload_youtube: boolean;
  retry_count: number;
};

export type PublishResult = {
  metadata: Record<string, unknown>;
  pipeline: Record<string, unknown>;
};

export type PatchExport = {
  id: number;
  patch_id: number;
  drive_folder_id: string;
  drive_folder_link: string;
  status: string;
  exported_chunk_count: number;
  imported_chunk_count: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  drive_account_id: number | null;
  account_email: string | null;
  sync_target_id: number | null;
  local_folder_path: string | null;
  sync_target_name: string | null;
  sync_target_email: string | null;
};

export type FlowDefinition = {
  id: number;
  name: string;
  nodes: string[];
};

export type VideoItem = {
  id: number;
  filename: string;
  original_name: string;
  file_path: string;
  file_size_bytes: number;
  resolution: string;
  upload_status: string;
  created_at: string;
  title?: string;
  description?: string;
  tags?: string;
  privacy?: string;
  batch_id?: string;
};

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    let message = `Lỗi ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail?.message || body.detail || message;
    } catch {}
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return (type.includes("json") ? await response.json() : await response.text()) as T;
}

export const post = (url: string, body?: BodyInit) => api(url, { method: "POST", body });
export const del = (url: string, body?: BodyInit) => api(url, { method: "DELETE", body });
export const postJson = <T>(url: string, data: any) =>
  api<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
export const patchJson = <T>(url: string, data: any) =>
  api<T>(url, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
export const put = <T>(url: string, data: any) =>
  api<T>(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
export const postForm = <T>(url: string, formData: FormData) =>
  api<T>(url, { method: "POST", body: formData });
