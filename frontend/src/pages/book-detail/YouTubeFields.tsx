import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Loader2, Mic, ShieldCheck, Sparkles } from "lucide-react";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { AiContentStatus, DESCRIPTION_EXTRA_FIELDS, DescriptionExtra, PodcastConfig, YouTubeConfig, errorText } from "./types";
import { CheckField, Field, fieldClass, selectClass } from "./parts";

/** Giữ default cho cấu hình cũ chưa có khối podcast. */
export const DEFAULT_PODCAST_CONFIG: PodcastConfig = { enabled: false, upload_cover: true };

/** Các control cấu hình YouTube dùng chung cho hộp thoại từng ebook và trang
 * Cấu hình mặc định — để hai nơi không lệch nhau khi thêm tùy chọn mới. */
export function YouTubeConfigFields({
  config,
  onChange,
  playlists,
  podcastAction,
}: {
  config: YouTubeConfig;
  onChange: (patch: Partial<YouTubeConfig>) => void;
  playlists: { id: string; title: string }[];
  /** Nút "áp dụng ngay" — chỉ trang cấu hình của từng ebook mới có sách để đẩy lên. */
  podcastAction?: React.ReactNode;
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

      <div className="rounded-md border border-border p-3 space-y-3">
        <CheckField
          checked={config.timeline_enabled}
          onChange={(value) => onChange({ timeline_enabled: value })}
          label="Hiển thị timeline chương trong description"
        />
        <p className="pl-6 text-[11px] leading-4 text-muted-foreground">
          Timeline chỉ xuất hiện khi audio có đủ mốc chương. Tắt để description không kèm danh sách mốc thời gian.
        </p>
        <CheckField
          checked={Boolean((config as any).auto_ai_labels)}
          onChange={(value) => onChange({ auto_ai_labels: value } as Partial<YouTubeConfig>)}
          label="Tự động gắn nhãn AI (AI-generated tags)"
        />
        <p className="pl-6 text-[11px] leading-4 text-muted-foreground">
          Khi bật, hệ thống tự sinh nhãn AI từ tiêu đề + mô tả và gộp vào tags trước khi upload (ví dụ: sách nói, audiobook, thể loại).
        </p>
      </div>

      <div className="rounded-md border border-border p-3 space-y-3">
        <div className="text-xs font-semibold">Sắp xếp playlist</div>
        <Field label="Chế độ sắp xếp">
          <select
            className={selectClass}
            value={(config as any).playlist_sort_mode || "manual"}
            onChange={(event) => onChange({ playlist_sort_mode: event.target.value as YouTubeConfig["playlist_sort_mode"] } as Partial<YouTubeConfig>)}
          >
            <option value="manual">Thủ công (giữ nguyên thứ tự upload)</option>
            <option value="natural">Số tự nhiên (tên video)</option>
            <option value="episode">Theo tập (Episode)</option>
          </select>
        </Field>
        <CheckField
          checked={Boolean((config as any).auto_sort_episode)}
          onChange={(value) => onChange({ auto_sort_episode: value } as Partial<YouTubeConfig>)}
          label="Tự động sắp xếp theo episode khi upload YouTube"
        />
        <p className="text-[11px] leading-4 text-muted-foreground">
          Khi bật, mỗi video mới thêm vào playlist sẽ được sắp xếp lại theo số tập (Tập 1, Tập 2…) để playlist luôn đúng thứ tự.
          Nếu chế độ là “Thủ công” và không bật tự động, playlist giữ nguyên thứ tự chèn.
        </p>
      </div>

      <PodcastFields
        value={config.podcast || DEFAULT_PODCAST_CONFIG}
        playlistId={config.playlist.playlist_id}
        onChange={(patch) => onChange({ podcast: { ...(config.podcast || DEFAULT_PODCAST_CONFIG), ...patch } })}
        action={podcastAction}
      />

      <DescriptionExtraFields
        value={config.description_extra}
        onChange={(patch) => onChange({ description_extra: { ...config.description_extra, ...patch } })}
      />
    </div>
  );
}

/** Cài đặt podcast: YouTube coi podcast là một playlist được đánh dấu, kèm ảnh
 * bìa vuông 1:1 lấy từ tab Thumbnail. */
export function PodcastFields({
  value,
  playlistId,
  onChange,
  action,
}: {
  value: PodcastConfig;
  playlistId: string;
  onChange: (patch: Partial<PodcastConfig>) => void;
  action?: React.ReactNode;
}) {
  return (
    <section className="space-y-3 rounded-md border border-border p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold">
            <Mic className="h-4 w-4 text-primary" /> Podcast
          </div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            Đánh dấu playlist của sách là podcast trên YouTube. Mỗi tập upload vào playlist sẽ tự đồng bộ thiết lập này.
          </p>
        </div>
        <CheckField checked={value.enabled} onChange={(enabled) => onChange({ enabled })} label="Bật" />
      </div>

      <div className={value.enabled ? "space-y-3" : "pointer-events-none space-y-3 opacity-45"}>
        <CheckField
          checked={value.upload_cover}
          onChange={(upload_cover) => onChange({ upload_cover })}
          label="Tải ảnh bìa 1:1 lên làm ảnh podcast"
        />
        <p className="pl-6 text-[11px] leading-4 text-muted-foreground">
          Ảnh lấy từ tab <strong>Thumbnail → Ảnh bìa Podcast (1:1)</strong>. Bật khung cắt vuông ở đó trước, nếu không
          sẽ không có ảnh để đẩy lên.
        </p>
        {!playlistId && (
          <p className="text-[11px] leading-4 text-amber-700">
            Chưa chọn playlist — podcast chính là playlist, hãy chọn hoặc để pipeline tự tạo playlist trước.
          </p>
        )}
        {action}
      </div>
    </section>
  );
}

type AiJobState = { status: string; percent: number; error_message: string | null };

/** Nút sinh Mô tả + Genre tags bằng API AI khai trong .env.
 *
 * Bấm là xếp job vào hàng đợi (một lượt gọi LLM chờ hàng chục giây, không giữ
 * request), rồi poll `/queue/jobs/{id}` cho tới khi job kết thúc. Xong sẽ nạp
 * lại cấu hình đã lưu nên hai ô trên form hiện đúng kết quả vừa sinh — chỉnh
 * tay trước khi bấm thì mất, nên phần ghi chú nhắc rõ điều đó. */
export function AiContentGenerator({
  bookId,
  status,
  onApplied,
  onMessage,
}: {
  bookId: string;
  status?: AiContentStatus;
  /** Nạp lại cấu hình YouTube từ server sau khi job đã ghi xong. */
  onApplied: () => Promise<void> | void;
  onMessage: (message: string) => void;
}) {
  const [jobId, setJobId] = useState<number>();
  const [detail, setDetail] = useState("");
  const running = jobId !== undefined;

  // Poll bám theo jobId: đặt id là bắt đầu, job về terminal thì dừng, đóng
  // dialog giữa chừng thì timer bị huỷ theo cleanup.
  useEffect(() => {
    if (jobId === undefined) return;
    let stopped = false;
    let timer = 0;
    const tick = async () => {
      try {
        const job = await api<AiJobState>(`/queue/jobs/${jobId}`);
        if (stopped) return;
        if (job.status === "done") {
          setJobId(undefined);
          setDetail("Đã ghi mô tả và thẻ vào cấu hình sách.");
          await onApplied();
          return;
        }
        if (job.status === "failed" || job.status === "cancelled") {
          setJobId(undefined);
          setDetail(job.error_message || (job.status === "cancelled" ? "Job đã bị hủy." : "Job sinh nội dung thất bại."));
          return;
        }
        setDetail(
          `Job #${jobId} · ${job.status === "running" ? "đang sinh nội dung" : "đang chờ worker"} (${job.percent}%)`
        );
        timer = window.setTimeout(tick, 2000);
      } catch (err) {
        if (stopped) return;
        setJobId(undefined);
        setDetail(errorText(err));
      }
    };
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [jobId, onApplied]);

  const run = useCallback(async () => {
    if (jobId !== undefined) return;
    setDetail("");
    try {
      const queued = await api<{ job_id: number }>(`/books/${bookId}/youtube-metadata-generate`, {
        method: "POST",
      });
      setJobId(queued.job_id);
    } catch (err) {
      setDetail(errorText(err));
      onMessage(errorText(err));
    }
  }, [bookId, jobId, onMessage]);

  return (
    <section className="space-y-2 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-semibold">
            <Sparkles className="h-4 w-4 text-primary" /> Sinh nội dung &amp; thẻ bằng AI
          </div>
          <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
            Job đọc tên sách và mục lục rồi ghi đè đúng hai ô <strong>Mô tả</strong> và{" "}
            <strong>Genre tags</strong>; các ô khác giữ nguyên. Chạy nền nên form vẫn dùng được — mọi chỉnh sửa
            chưa lưu sẽ bị thay khi job xong.
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={!status?.configured || running}
          onClick={run}
          title={status?.configured ? undefined : status?.detail || "Chưa cấu hình provider AI trong .env"}
        >
          {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          {running ? "Đang sinh..." : "Sinh nội dung & thẻ"}
        </Button>
      </div>

      {!status?.configured && (
        <p className="flex items-start gap-1.5 rounded-md bg-amber-50 px-2.5 py-2 text-[11px] leading-4 text-amber-800">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {status?.detail || "Chưa cấu hình provider AI trong .env (AI_CONTENT_PROVIDER + API key)."}
        </p>
      )}

      {status?.configured && !running && !detail && (
        <p className="text-[11px] text-muted-foreground">
          Provider: {status.label || `${status.provider} · ${status.model}`}
        </p>
      )}

      {detail && <p className="text-[11px] text-muted-foreground">{detail}</p>}

      {running && (
        <Link to="/queue" className="inline-block text-[11px] text-primary underline">
          Theo dõi ở hàng đợi →
        </Link>
      )}
    </section>
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
