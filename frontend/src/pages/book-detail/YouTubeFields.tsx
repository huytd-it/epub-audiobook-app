import React from "react";
import { ShieldCheck } from "lucide-react";
import { Textarea } from "@/components/ui/textarea";
import { DESCRIPTION_EXTRA_FIELDS, DescriptionExtra, YouTubeConfig } from "./types";
import { CheckField, Field, fieldClass, selectClass } from "./parts";

/** Các control cấu hình YouTube dùng chung cho hộp thoại từng ebook và trang
 * Cấu hình mặc định — để hai nơi không lệch nhau khi thêm tùy chọn mới. */
export function YouTubeConfigFields({
  config,
  onChange,
  playlists,
}: {
  config: YouTubeConfig;
  onChange: (patch: Partial<YouTubeConfig>) => void;
  playlists: { id: string; title: string }[];
}) {
  return (
    <div className="space-y-4">
      <CheckField
        checked={config.auto_upload}
        onChange={(value) => onChange({ auto_upload: value })}
        label="Tự động upload sau khi tạo video"
      />

      <div className="grid grid-cols-1 gap-4">
        <Field label="Title template">
          <input
            className={fieldClass}
            value={config.title_template}
            onChange={(event) => onChange({ title_template: event.target.value })}
          />
        </Field>
        <Field label="Mô tả">
          <Textarea
            className="min-h-20 text-xs"
            value={config.description}
            onChange={(event) => onChange({ description: event.target.value })}
          />
        </Field>
        <Field label="Genre tags">
          <input
            className={fieldClass}
            value={config.genre_tags}
            onChange={(event) => onChange({ genre_tags: event.target.value })}
          />
        </Field>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="Privacy">
            <select
              className={selectClass}
              value={config.privacy_status}
              onChange={(event) => onChange({ privacy_status: event.target.value })}
            >
              <option value="private">Private</option>
              <option value="unlisted">Unlisted</option>
              <option value="public">Public</option>
            </select>
          </Field>
          <Field label="Playlist">
            <select
              className={selectClass}
              value={config.playlist.playlist_id}
              onChange={(event) =>
                onChange({
                  playlist: {
                    ...config.playlist,
                    mode: event.target.value ? "existing" : "none",
                    playlist_id: event.target.value,
                  },
                })
              }
            >
              <option value="">Không chọn</option>
              {playlists.map((playlist) => (
                <option key={playlist.id} value={playlist.id}>
                  {playlist.title}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <PlaylistLinkNote playlistId={config.playlist.playlist_id} />
      </div>

      <div className="rounded-md border border-border p-3">
        <CheckField
          checked={config.timeline_enabled}
          onChange={(value) => onChange({ timeline_enabled: value })}
          label="Hiển thị timeline chương trong description"
        />
        <p className="mt-1.5 pl-6 text-[11px] leading-4 text-muted-foreground">
          Timeline chỉ xuất hiện khi audio có đủ mốc chương. Tắt để description không kèm danh sách mốc thời gian.
        </p>
      </div>

      <DescriptionExtraFields
        value={config.description_extra}
        onChange={(patch) => onChange({ description_extra: { ...config.description_extra, ...patch } })}
      />
    </div>
  );
}

export const PLAYLIST_URL_PREFIX = "https://www.youtube.com/playlist?list=";

/** Nhắc rằng link playlist luôn được chèn vào description — người xem cần link này
 * mới theo dõi được trọn bộ. Backend tự thêm khi upload, ô Mô tả không cần gõ tay. */
function PlaylistLinkNote({ playlistId }: { playlistId: string }) {
  if (!playlistId)
    return (
      <p className="text-[11px] leading-4 text-amber-700">
        Chưa chọn playlist — description sẽ không có link để người nghe theo dõi trọn bộ.
      </p>
    );
  return (
    <p className="text-[11px] leading-4 text-muted-foreground">
      Link playlist được tự động chèn vào description mỗi video:{" "}
      <a
        href={`${PLAYLIST_URL_PREFIX}${playlistId}`}
        target="_blank"
        rel="noreferrer"
        className="break-all font-mono text-primary underline"
      >
        {PLAYLIST_URL_PREFIX}
        {playlistId}
      </a>
    </p>
  );
}

/** Khối nội dung mở rộng nối vào cuối description: phần chữ cố định nằm trong
 * template, phần thay đổi theo ebook nằm ở các ô điền bên trên. */
export function DescriptionExtraFields({
  value,
  onChange,
}: {
  value: DescriptionExtra;
  onChange: (patch: Partial<DescriptionExtra>) => void;
}) {
  const blanks = DESCRIPTION_EXTRA_FIELDS.filter(
    (field) => value.template.includes(field.hint) && !String(value[field.key] || "").trim()
  );

  return (
    <section className="space-y-3 rounded-md border border-border p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold">
            <ShieldCheck className="h-4 w-4 text-primary" /> Nội dung mở rộng (tránh bản quyền)
          </div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            Nối vào cuối description mỗi video: thông báo bản quyền, miễn trừ AI, nội dung hư cấu, nguồn truyện và
            Fair Use.
          </p>
        </div>
        <CheckField checked={value.enabled} onChange={(next) => onChange({ enabled: next })} label="Bật" />
      </div>

      <div className={value.enabled ? "space-y-3" : "pointer-events-none space-y-3 opacity-45"}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {DESCRIPTION_EXTRA_FIELDS.map((field) => (
            <Field key={field.key} label={field.label} hint={field.hint}>
              <input
                className={fieldClass}
                value={String(value[field.key] || "")}
                onChange={(event) => onChange({ [field.key]: event.target.value } as Partial<DescriptionExtra>)}
              />
            </Field>
          ))}
        </div>

        {blanks.length > 0 && (
          <div className="rounded-md bg-amber-50 px-3 py-2 text-[11px] leading-4 text-amber-800">
            Chưa điền: {blanks.map((field) => field.label.toLowerCase()).join(", ")}. Dòng chứa các ô này sẽ bị bỏ khỏi
            description thay vì đăng thiếu nội dung.
          </div>
        )}

        <Field label="Nội dung khối mở rộng" hint="Giữ nguyên placeholder để tự điền">
          <Textarea
            className="min-h-56 font-mono text-[11px] leading-5"
            value={value.template}
            onChange={(event) => onChange({ template: event.target.value })}
          />
        </Field>
      </div>
    </section>
  );
}
