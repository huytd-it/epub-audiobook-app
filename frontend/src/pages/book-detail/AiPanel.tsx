import React, { useCallback, useEffect, useState } from "react";
import { Image as ImageIcon, Sparkles, Type } from "lucide-react";
import { api, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { errorText } from "./types";

type AiStatus = {
  configured: boolean;
  providers: { id: string; adapter: string; configured: boolean; active: boolean }[];
  tasks: { name: string; label: string }[];
  context: {
    title: string;
    author: string;
    description: string;
    language: string;
    publisher: string;
    subjects: string[];
    chapter_count: number;
    total_chars: number;
    excerpt_chapter_1: string;
    has_cover: boolean;
  };
  saved_content: Record<string, string>;
  ai_thumbnail_path: string | null;
  cover_image_path: string | null;
};

type GenerateResult = {
  task: string;
  result: Record<string, string>;
  saved?: { applied_to_youtube_config?: string[]; playlist_title?: string; playlist_description?: string };
};

/** AI studio: nội dung YouTube + ảnh bìa per-book, grounded trong metadata sách. */
export function AiPanel({
  bookId,
  onMessage,
  onSaved,
}: {
  bookId: string;
  onMessage: (msg: string) => void;
  onSaved: () => void;
}) {
  const [status, setStatus] = useState<AiStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState<"content" | "thumbnail" | null>(null);
  const [result, setResult] = useState<GenerateResult | null>(null);
  const [thumbPrompt, setThumbPrompt] = useState("");
  const [thumbStyle, setThumbStyle] = useState("");
  const [thumbUrl, setThumbUrl] = useState("");

  // Metadata sách (grounding) — sửa tại đây để AI generate đúng nội dung.
  const [meta, setMeta] = useState({ author: "", description: "", language: "", subjects: "" });
  const [metaBusy, setMetaBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api<AiStatus>(`/books/${bookId}/ai/status`);
      setStatus(data);
      setMeta({
        author: data.context.author || "",
        description: data.context.description || "",
        language: data.context.language || "",
        subjects: (data.context.subjects || []).join(", "),
      });
      if (data.saved_content && Object.keys(data.saved_content).length) {
        setResult({ task: "youtube_content", result: data.saved_content });
      }
      if (data.ai_thumbnail_path) {
        setThumbUrl(`/books/${bookId}/ai/thumbnail?t=${Date.now()}`);
      } else {
        setThumbUrl("");
      }
    } catch (err) {
      onMessage(errorText(err));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bookId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function saveMeta() {
    setMetaBusy(true);
    try {
      await postJson(`/books/${bookId}/metadata`, meta);
      onMessage("Đã lưu thông tin sách — AI sẽ dùng đúng nội dung này.");
      await load();
      onSaved();
    } catch (err) {
      onMessage(errorText(err));
    } finally {
      setMetaBusy(false);
    }
  }

  async function generateContent() {
    setGenerating("content");
    try {
      const data = await postJson<GenerateResult>(`/books/${bookId}/ai/generate`, {
        task: "youtube_content",
        apply: true,
        save: true,
      });
      setResult(data);
      onMessage(
        `Đã tạo nội dung AI${data.saved?.applied_to_youtube_config?.length ? ` (đã điền: ${data.saved.applied_to_youtube_config.join(", ")})` : ""}.`
      );
      await load();
      onSaved();
    } catch (err) {
      onMessage(errorText(err));
    } finally {
      setGenerating(null);
    }
  }

  async function generateThumbnail() {
    setGenerating("thumbnail");
    try {
      const data = await postJson<{ path: string; prompt: string; prompt_source: string }>(
        `/books/${bookId}/ai/thumbnail`,
        {
          prompt: thumbPrompt.trim() || undefined,
          style: thumbStyle.trim() || undefined,
          size: "1280x720",
          save: true,
        }
      );
      setThumbUrl(`/books/${bookId}/ai/thumbnail?t=${Date.now()}`);
      onMessage(`Đã tạo ảnh bìa AI (${data.prompt_source === "override" ? "theo prompt của bạn" : "AI tự dựng từ thông tin sách"}).`);
      await load();
      onSaved();
    } catch (err) {
      onMessage(errorText(err));
    } finally {
      setGenerating(null);
    }
  }

  if (loading && !status) {
    return <div className="rounded-md border border-border bg-card p-6 text-xs text-muted-foreground">Đang tải trạng thái AI...</div>;
  }

  const ctx = status?.context;
  const activeProvider = status?.providers.find((p) => p.active);

  return (
    <div className="space-y-4">
      {!status?.configured && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
          Chưa cấu hình AI. Đặt <span className="font-mono font-semibold">OPENAI_API_KEY</span> trong{" "}
          <span className="font-mono">.env</span> (xem <span className="font-mono">.env.example</span> mục
          Generative AI) rồi khởi động lại server. Muốn dùng server tương thích OpenAI khác, đặt{" "}
          <span className="font-mono">AI_BASE_URL</span>.
        </div>
      )}

      {/* Grounding: thông tin sách AI dựa vào */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Type className="h-4 w-4 text-primary" /> Thông tin sách cho AI
            <span className="text-xs font-normal text-muted-foreground">
              · {ctx?.chapter_count ?? 0} chương · {((ctx?.total_chars ?? 0) / 1000).toFixed(1)}k ký tự
              {ctx?.has_cover ? " · có bìa gốc" : ""}
              {activeProvider ? ` · provider: ${activeProvider.id}` : ""}
            </span>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-xs font-medium">
              Tác giả
              <Input className="mt-1 h-9 text-sm" value={meta.author} onChange={(e) => setMeta({ ...meta, author: e.target.value })} placeholder="Kim Dung..." />
            </label>
            <label className="block text-xs font-medium">
              Ngôn ngữ / Thể loại
              <div className="mt-1 flex gap-2">
                <Input className="h-9 w-24 text-sm" value={meta.language} onChange={(e) => setMeta({ ...meta, language: e.target.value })} placeholder="vi" />
                <Input className="h-9 text-sm" value={meta.subjects} onChange={(e) => setMeta({ ...meta, subjects: e.target.value })} placeholder="kiếm hiệp, tiên hiệp" />
              </div>
            </label>
          </div>
          <label className="block text-xs font-medium">
            Tóm tắt
            <Textarea className="mt-1 min-h-20 text-xs" value={meta.description} onChange={(e) => setMeta({ ...meta, description: e.target.value })} placeholder="Tóm tắt để AI bám đúng nội dung..." maxLength={2000} />
          </label>
          <div className="flex justify-end">
            <Button size="sm" variant="outline" disabled={metaBusy} onClick={saveMeta}>
              {metaBusy ? "Đang lưu..." : "Lưu thông tin sách"}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Nội dung YouTube */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex items-center gap-2 text-sm font-semibold">
              <Sparkles className="h-4 w-4 text-primary" /> Nội dung YouTube (cấp sách)
            </span>
            <Button size="sm" className="ml-auto" disabled={generating !== null || !status?.configured} onClick={generateContent}>
              {generating === "content" ? "AI đang viết..." : "Tạo nội dung bằng AI"}
            </Button>
          </div>
          {result ? (
            <div className="space-y-2 text-xs">
              {result.result.playlist_title && (
                <div><span className="font-semibold">Playlist:</span> {result.result.playlist_title}</div>
              )}
              {result.result.playlist_description && (
                <div className="whitespace-pre-wrap rounded-md bg-muted/40 p-2">{result.result.playlist_description}</div>
              )}
              {result.result.genre_tags && (
                <div><span className="font-semibold">Tags:</span> <span className="font-mono">{result.result.genre_tags}</span></div>
              )}
              {result.result.video_title_template && (
                <div><span className="font-semibold">Mẫu tiêu đề tập:</span> <span className="font-mono">{result.result.video_title_template}</span></div>
              )}
              {result.result.video_description && (
                <div className="whitespace-pre-wrap rounded-md bg-muted/40 p-2">{result.result.video_description}</div>
              )}
              {result.saved?.applied_to_youtube_config?.length ? (
                <div className="text-emerald-700">Đã điền vào cấu hình YouTube: {result.saved.applied_to_youtube_config.join(", ")}.</div>
              ) : null}
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">Chưa có bản nháp. AI viết từ tên sách, tác giả, thể loại và trích đoạn chương 1.</div>
          )}
        </CardContent>
      </Card>

      {/* Ảnh bìa per-book */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex items-center gap-2 text-sm font-semibold">
              <ImageIcon className="h-4 w-4 text-primary" /> Ảnh bìa AI (1 ảnh cho cả sách)
            </span>
            <Button size="sm" variant="outline" className="ml-auto" disabled={generating !== null || !status?.configured} onClick={generateThumbnail}>
              {generating === "thumbnail" ? "AI đang vẽ..." : "Vẽ bìa bằng AI"}
            </Button>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-xs font-medium">
              Prompt tùy ý (bỏ trống = AI tự dựng từ thông tin sách)
              <Textarea className="mt-1 min-h-20 text-xs" value={thumbPrompt} onChange={(e) => setThumbPrompt(e.target.value)} placeholder="a lonely swordsman on a misty mountain at dawn..." />
            </label>
            <div className="space-y-3">
              <label className="block text-xs font-medium">
                Phong cách
                <Input className="mt-1 h-9 text-sm" value={thumbStyle} onChange={(e) => setThumbStyle(e.target.value)} placeholder="cinematic, epic, highly detailed" />
              </label>
              {thumbUrl ? (
                <img src={thumbUrl} alt="Ảnh bìa AI" className="w-full rounded-md border border-border" />
              ) : (
                <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted-foreground">Chưa có ảnh bìa AI.</div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
