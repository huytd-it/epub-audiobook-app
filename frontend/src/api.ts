export type Book = { id: number; title: string; status: string; original_filename: string; created_at: string; patches?: { total: number; done: number; active: number; failed: number } };
export type Patch = { id: number; patch_index: number; name: string; status: string; chunk_count: number; next_chunk_index: number; error_message: string | null };
export type Chapter = { id: number; chapter_index: number; title: string; char_count: number; is_excluded: boolean };
export type Job = { id: number; job_type: string; status: string; phase: string; percent: number; book_id: number | null; error_message: string | null; created_at: string };
export type Media = { music: Array<{ id: number; name: string; duration_sec: number | null; description?: string; license?: string }>; photos: Array<{ name: string; size: number; is_video: boolean }>; voices: Array<{ name: string; size: number; description?: string }> };

export type MusicItem = { id: number; name: string; duration_sec: number | null; description?: string; license?: string };
export type PhotoItem = { name: string; size: number; size_kb?: number; is_video: boolean };
export type VoiceItem = { name: string; size: number; size_kb?: number; description?: string };
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

export type PatchExport = {
  id: number;
  patch_id: number;
  export_dir: string;
  created_at: string;
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
export const postForm = <T>(url: string, formData: FormData) =>
  api<T>(url, { method: "POST", body: formData });
