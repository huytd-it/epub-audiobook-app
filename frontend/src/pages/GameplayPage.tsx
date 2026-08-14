import React, { useEffect, useState } from "react";
import { api, del, postForm } from "@/api";
import { Header, LoadingState } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Copy, Download, Plus, Shield, Swords, Trophy, Upload } from "lucide-react";

type PoolRow = { profile_key: string; status: string; clip_count: number; duration_seconds: number };
type Fighter = { id: number; name: string; class_name: string; matches: number; wins: number; eliminations: number };
type Theme = { id: string; version: number; name: string; enabled: number; builtin: number; asset_dir?: string; error_message?: string };
type Status = { pool: PoolRow[]; target_seconds: number; fighters: Fighter[]; themes: Theme[] };

export function GameplayPage() {
  const [data, setData] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = () => api<Status>("/gameplay/status").then(setData).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);

  const generate = async () => {
    setBusy(true); setError("");
    const form = new FormData(); form.append("count", "1");
    try { await postForm("/gameplay/generate", form); await load(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const upload = async (file?: File) => {
    if (!file) return;
    setBusy(true); const form = new FormData(); form.append("file", file);
    try { await postForm("/gameplay/themes/upload", form); await load(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const toggle = async (theme: Theme) => {
    const form = new FormData(); form.append("enabled", String(!theme.enabled));
    await postForm(`/gameplay/themes/${theme.id}/${theme.version}/toggle`, form); await load();
  };
  const remove = async (theme: Theme) => {
    if (!confirm(`Xóa theme ${theme.name}?`)) return;
    try { await del(`/gameplay/themes/${theme.id}/${theme.version}`); await load(); } catch (e) { setError(String(e)); }
  };
  const prompt = "Army theme: futuristic neon army. Unit class: tank, assassin, ranger. Colors: cyan, magenta, electric lime. Top-down 2D game sprite, transparent alpha, centered, no text, no watermark, no background, clear silhouette, consistent lighting and proportions.";
  if (!data) return <><Header title="Đấu trường" subtitle="Nền gameplay Battle Royale cho audiobook" />{error ? <p className="text-destructive">{error}</p> : <LoadingState />}</>;
  const available = data.pool.filter((p) => p.status === "available").reduce((sum, p) => sum + p.duration_seconds, 0);
  return <div className="space-y-6">
    <Header title="Đấu trường" subtitle="Replay deterministic, clip pool và Army Theme Pack" action={<Button onClick={generate} disabled={busy}><Plus className="mr-2 h-4 w-4" />Tạo clip</Button>} />
    {error && <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
    <section className="grid gap-4 md:grid-cols-3">
      <Card className="overflow-hidden border-cyan-400/30 bg-gradient-to-br from-cyan-400/10 to-transparent"><CardHeader><CardTitle className="flex items-center gap-2"><Swords className="h-5 w-5 text-cyan-400" />Pool khả dụng</CardTitle></CardHeader><CardContent><div className="font-mono text-3xl font-black">{(available / 60).toFixed(1)} phút</div><p className="mt-2 text-xs text-muted-foreground">Mục tiêu {(data.target_seconds / 60).toFixed(0)} phút / profile</p></CardContent></Card>
      <Card><CardHeader><CardTitle>Trạng thái clip</CardTitle></CardHeader><CardContent className="space-y-2">{data.pool.map((row) => <div key={`${row.profile_key}-${row.status}`} className="flex justify-between text-xs"><Badge variant="secondary">{row.status}</Badge><span className="font-mono">{row.clip_count} · {(row.duration_seconds/60).toFixed(1)}m</span></div>)}</CardContent></Card>
      <Card><CardHeader><CardTitle className="flex items-center gap-2"><Shield className="h-5 w-5" />Renderer</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">CPU / Pillow / NumPy / libx264<br />24 fighters · 3–5 phút · video-only</CardContent></Card>
    </section>
    <section><h2 className="mb-3 flex items-center gap-2 text-lg font-bold"><Trophy className="h-5 w-5 text-lime-400" />Leaderboard</h2><div className="overflow-x-auto rounded-lg border"><table className="w-full text-sm"><thead className="bg-muted/50 text-left text-xs uppercase"><tr><th className="p-3">Fighter</th><th>Class</th><th>Matches</th><th>Wins</th><th>Eliminations</th></tr></thead><tbody>{data.fighters.slice(0, 20).map((f) => <tr key={f.id} className="border-t"><td className="p-3 font-semibold">{f.name}</td><td><Badge variant="outline">{f.class_name}</Badge></td><td>{f.matches}</td><td>{f.wins}</td><td>{f.eliminations}</td></tr>)}</tbody></table></div></section>
    <section className="space-y-3"><div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center"><div><h2 className="text-lg font-bold">Army Theme Packs</h2><p className="text-xs text-muted-foreground">ZIP đã kiểm tra PNG alpha và path traversal.</p></div><label><input type="file" accept=".zip" className="hidden" onChange={(e) => upload(e.target.files?.[0])} /><Button asChild variant="outline"><span><Upload className="mr-2 h-4 w-4" />Upload ZIP</span></Button></label></div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{data.themes.map((theme) => <Card key={`${theme.id}-${theme.version}`}><CardHeader><CardTitle className="flex justify-between"><span>{theme.name}</span><Badge variant={theme.enabled ? "default" : "secondary"}>v{theme.version}</Badge></CardTitle></CardHeader><CardContent className="space-y-3"><div className="text-xs font-mono text-muted-foreground">{theme.id}</div>{theme.error_message && <p className="text-xs text-destructive">{theme.error_message}</p>}<div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => toggle(theme)}>{theme.enabled ? "Tắt" : "Bật"}</Button>{!theme.builtin && <Button size="sm" variant="destructive" onClick={() => remove(theme)}>Xóa</Button>}</div></CardContent></Card>)}</div>
      <Card><CardHeader><CardTitle>Prompt template</CardTitle></CardHeader><CardContent><p className="rounded-md bg-muted p-3 text-xs leading-relaxed">{prompt}</p><div className="mt-3 flex gap-2"><Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(prompt)}><Copy className="mr-2 h-4 w-4" />Copy</Button><Button size="sm" variant="outline" asChild><a download="army-theme-prompt.txt" href={`data:text/plain;charset=utf-8,${encodeURIComponent(prompt)}`}><Download className="mr-2 h-4 w-4" />Download</a></Button></div></CardContent></Card>
    </section>
  </div>;
}
