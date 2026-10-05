import React, { useEffect, useState } from "react";
import { Clapperboard, Plus, RefreshCw, Send, Sparkles } from "lucide-react";
import { api, postJson } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

type Short = {
  id: number; book_id: number; script_text: string; script_source: string;
  duration_target: number; voice_id?: string | null; music_id?: number | null;
  resolution: string; video_path?: string | null; caption: string;
  story_link: string; status: string; renderer?: string;
};

type ShortDetail = Short & {
  uploads: Array<{ platform: string; platform_video_id?: string | null; status: string; error_message?: string | null }>;
};

const RESOLUTIONS = ["1080x1920", "1080x1080", "1920x1080"];
/** ffmpeg = pipeline có sẵn; remotion = chỉ lớp đồ hoạ dọc (cần npm install trong remotion/). */
const RENDERERS = ["ffmpeg", "remotion"];
const PLATFORM_LABEL: Record<string, string> = { fb: "Facebook", tiktok: "TikTok", youtube: "YouTube Shorts" };

export function ShortsStudio() {
  const [shorts, setShorts] = useState<Short[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<ShortDetail | null>(null);
  const [bookId, setBookId] = useState("");
  const [resolution, setResolution] = useState("1080x1920");
  const [renderer, setRenderer] = useState("ffmpeg");
  const [busy, setBusy] = useState(false);

  const load = () => {
    setLoading(true);
    api<{ shorts: Short[] }>("/shorts")
      .then((r) => setShorts(r.shorts || []))
      .catch((e) => console.error(e))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const openDetail = async (id: number) => {
    try {
      const d = await api<ShortDetail>(`/shorts/${id}`);
      setSelected(d);
    } catch (e: any) { alert(e.message); }
  };

  const createShort = async () => {
    if (!bookId.trim()) return alert("Nhập book_id của truyện");
    setBusy(true);
    try {
      const form = new FormData();
      form.append("book_id", bookId.trim());
      form.append("resolution", resolution);
      form.append("renderer", renderer);
      const res = await fetch("/shorts", { method: "POST", body: form });
      if (!res.ok) throw new Error(await res.text());
      setBookId("");
      load();
    } catch (e: any) { alert(e.message); } finally { setBusy(false); }
  };

  const patch = async (id: number, body: any, path = "script") => {
    setBusy(true);
    try {
      await postJson(`/shorts/${id}/${path}`, body);
      await openDetail(id);
      load();
    } catch (e: any) { alert(e.message); } finally { setBusy(false); }
  };

  const previewAi = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const r = await postJson<any>(`/shorts/${selected.id}/script/preview`, {});
      setSelected({ ...selected, script_text: r.script || selected.script_text, caption: r.caption || selected.caption });
    } catch (e: any) { alert(e.message); } finally { setBusy(false); }
  };

  const render = async () => {
    if (!selected) return;
    await patch(selected.id, {
      resolution: selected.resolution,
      voice_id: selected.voice_id ?? null,
      music_id: selected.music_id ?? null,
      renderer: selected.renderer || "ffmpeg",
      render_config: {},
    }, "render");
  };

  const publish = async () => {
    if (!selected) return;
    if (!selected.video_path) return alert("Short chưa render xong");
    setBusy(true);
    try {
      await postJson(`/shorts/${selected.id}/publish`, {});
      await openDetail(selected.id);
    } catch (e: any) { alert(e.message); } finally { setBusy(false); }
  };

  return (
    <div className="space-y-6">
      <Header title="Short Video Studio" subtitle="1 short = 1 truyện · script AI/sửa tay · render dọc · caption chung · auto-upload FB/TikTok/YouTube Shorts." />
      <Card>
        <CardHeader><CardTitle>Tạo short mới từ sách</CardTitle></CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2 items-center">
            <Input placeholder="book_id (vd 12)" value={bookId} onChange={(e) => setBookId(e.target.value)} className="w-40" />
            <select value={resolution} onChange={(e) => setResolution(e.target.value)} className="border rounded px-2 py-1.5 text-sm bg-background">
              {RESOLUTIONS.map((r) => <option key={r} value={r}>{r}{r === "1080x1920" ? " (mặc định)" : ""}</option>)}
            </select>
            <select value={renderer} onChange={(e) => setRenderer(e.target.value)} className="border rounded px-2 py-1.5 text-sm bg-background" title="Lớp đồ hoạ dọc">
              {RENDERERS.map((r) => <option key={r} value={r}>{r === "ffmpeg" ? "ffmpeg (có sẵn)" : "remotion (đồ hoạ dọc)"}</option>)}
            </select>
            <Button size="sm" onClick={createShort} disabled={busy}><Plus className="h-4 w-4" /> Tạo short</Button>
          </div>
          <p className="text-xs text-muted-foreground mt-2">Render 1 khổ mỗi lần tạo. Voice mặc định kế thừa sách, nhạc từ thư viện. Không làm feed trong app.</p>
        </CardContent>
      </Card>

      <div className="grid md:grid-cols-2 gap-4">
        <Card>
          <CardHeader><CardTitle>Danh sách shorts ({shorts.length})</CardTitle></CardHeader>
          <CardContent>
            {loading ? <LoadingState /> : shorts.length === 0 ? <EmptyState text="Chưa có short nào" /> : (
              <div className="space-y-2 max-h-[560px] overflow-auto">
                {shorts.map((s) => (
                  <button key={s.id} onClick={() => openDetail(s.id)} className={`w-full text-left border rounded p-2.5 hover:bg-muted ${selected?.id === s.id ? "border-primary" : ""}`}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium text-sm">#{s.id} · sách {s.book_id} · {s.resolution}</span>
                      <StatusBadge value={s.status} />
                    </div>
                    <div className="text-xs text-muted-foreground truncate mt-1">{s.script_text?.slice(0, 90) || "(chưa có kịch bản)"}</div>
                  </button>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>{selected ? `Short #${selected.id}` : "Chi tiết"}</CardTitle></CardHeader>
          <CardContent>
            {!selected ? <EmptyState text="Chọn 1 short để sửa" /> : (
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <StatusBadge value={selected.status} />
                  <span className="text-xs text-muted-foreground">{selected.resolution} · nguồn: {selected.script_source} · renderer: {selected.renderer || "ffmpeg"}</span>
                </div>
                <label className="text-xs font-medium">Kịch bản 60-90s (AI hoặc sửa tay)</label>
                <Textarea rows={6} value={selected.script_text} onChange={(e) => setSelected({ ...selected, script_text: e.target.value })} />
                <div className="flex flex-wrap gap-2 items-center">
                  <select
                    value={selected.renderer || "ffmpeg"}
                    onChange={(e) => setSelected({ ...selected, renderer: e.target.value })}
                    className="border rounded px-2 py-1.5 text-sm bg-background"
                    title="ffmpeg: pipeline có sẵn. remotion: lớp đồ hoạ dọc (hook + sub highlight từ), cần npm install trong remotion/"
                  >
                    {RENDERERS.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                  <Button size="sm" variant="secondary" onClick={previewAi} disabled={busy}><Sparkles className="h-4 w-4" /> Sinh AI</Button>
                  <Button size="sm" variant="outline" onClick={() => patch(selected.id, { script_text: selected.script_text, script_source: "manual", duration_target: 75 })} disabled={busy}>Lưu script</Button>
                  <Button size="sm" onClick={render} disabled={busy}><Clapperboard className="h-4 w-4" /> Render dọc</Button>
                </div>
                {selected.video_path && (
                  <video src={selected.video_path} controls className="w-full max-h-72 rounded border" />
                )}
                <label className="text-xs font-medium">Caption chung 3 kênh</label>
                <Textarea rows={3} value={selected.caption} onChange={(e) => setSelected({ ...selected, caption: e.target.value })} placeholder="Caption + hashtags…" />
                <Input value={selected.story_link} onChange={(e) => setSelected({ ...selected, story_link: e.target.value })} placeholder="story_link (URL dán tay, TikTok/Reels không bấm được)" />
                <Button size="sm" variant="outline" onClick={() => patch(selected.id, { caption: selected.caption, story_link: selected.story_link }, "caption")} disabled={busy}>Lưu caption + link</Button>
                <div className="border-t pt-3 space-y-2">
                  <div className="text-xs font-medium">Trạng thái per-platform</div>
                  {(selected.uploads || []).map((u) => (
                    <div key={u.platform} className="flex items-center justify-between gap-2 text-sm border rounded px-2 py-1.5">
                      <span>{PLATFORM_LABEL[u.platform] || u.platform} · <StatusBadge value={u.status} /></span>
                      <span className="flex items-center gap-2">
                        {u.error_message && <span className="text-xs text-destructive max-w-48 truncate" title={u.error_message}>{u.error_message}</span>}
                        {u.status === "failed" && (
                          <Button size="sm" variant="outline" onClick={async () => { await postJson(`/shorts/${selected.id}/retry/${u.platform}`, {}); await openDetail(selected.id); }}>
                            <RefreshCw className="h-3.5 w-3.5" /> Retry
                          </Button>
                        )}
                      </span>
                    </div>
                  ))}
                  <Button size="sm" onClick={publish} disabled={busy}><Send className="h-4 w-4" /> Đăng 3 kênh ngay</Button>
                  <p className="text-[11px] text-muted-foreground">Mặc định đăng ngay/private-draft. YouTube Shorts tự nhận video dọc ≤3 phút.</p>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
