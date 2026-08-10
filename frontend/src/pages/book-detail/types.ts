import { Book, Chapter, DriveAccount, DriveTarget, Patch, PatchExport } from "@/api";

export type PlannedPatch = {
  patch_index: number;
  chapter_start: number;
  chapter_end: number;
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

export type ConfigTab = "audio" | "video" | "youtube";

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
