import React, { useEffect, useMemo, useState } from "react";
import { api, del, postForm } from "@/api";
import { Header, LoadingState } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Copy, Download, Plus, Shield, Sparkles, Trophy, Upload } from "lucide-react";

type PoolRow = { profile_key: string; game_id?: string | null; status: string; clip_count: number; duration_seconds: number };
type Fighter = { id: number; name: string; class_name: string; matches: number; wins: number; eliminations: number };
type Theme = { id: string; version: number; name: string; enabled: number; builtin: number; asset_dir?: string; error_message?: string };
type ThemePack = { id: string; version: number; family: "pixel" | "neon"; name: string; enabled: number; builtin: number; content_sha256: string; validation_status: string; error_message?: string };
type Game = { id: string; name: string; family: "pixel" | "neon" | "legacy"; waveform_policy: string; description: string; enabled: boolean; sprite_roles?: string[] };
type Status = { catalog?: Game[]; pool: PoolRow[]; target_seconds: number; fighters: Fighter[]; themes: Theme[]; theme_packs?: ThemePack[]; health?: { failed_clips: number; oldest_lease_at?: string | null } };

const fallbackCatalog: Game[] = [
  { id: "garden_cycle", name: "Garden Cycle", family: "pixel", waveform_policy: "allowed_with_safe_area", description: "Trồng và thu hoạch một khu vườn yên bình.", enabled: true, sprite_roles: ["carrot", "wheat", "tomato", "lavender", "pumpkin"] },
  { id: "aquarium_ecosystem", name: "Aquarium Ecosystem", family: "pixel", waveform_policy: "default_off", description: "Đàn cá và cây thủy sinh chuyển động chậm.", enabled: true, sprite_roles: ["kind_0", "kind_1", "kind_2", "kind_3"] },
  { id: "parcel_route", name: "Parcel Route", family: "pixel", waveform_policy: "allowed_with_safe_area", description: "Chuyến giao hàng yên bình qua thị trấn.", enabled: true, sprite_roles: ["kind_0", "kind_1", "kind_2", "kind_3"] },
  { id: "cloud_runner", name: "Cloud Runner", family: "pixel", waveform_policy: "allowed_with_safe_area", description: "Chuyến chạy ngắm cảnh qua mây và đồng cỏ.", enabled: true, sprite_roles: ["kind_0", "kind_1", "kind_2", "kind_3"] },
  { id: "orbit_drift", name: "Orbit Drift", family: "neon", waveform_policy: "default_off", description: "Trôi qua các cổng sáng trong không gian.", enabled: true, sprite_roles: ["gate", "ship"] },
  { id: "marble_flow", name: "Marble Flow", family: "neon", waveform_policy: "forbidden", description: "Những viên bi sáng đi qua mạng đường mềm.", enabled: true, sprite_roles: ["node"] },
  { id: "territory_bloom", name: "Territory Bloom", family: "neon", waveform_policy: "default_off", description: "Các dải màu nở và hòa vào nhau.", enabled: true, sprite_roles: ["node"] },
  { id: "signal_garden", name: "Signal Garden", family: "neon", waveform_policy: "default_off", description: "Mạng nút ánh sáng đồng bộ theo nhịp chậm.", enabled: true, sprite_roles: ["node"] },
  { id: "battle_royale", name: "Neon Battle Royale", family: "legacy", waveform_policy: "forbidden", description: "Chế độ tương thích cũ.", enabled: true, sprite_roles: [] },
];

export function GameplayPage() {
  const [data, setData] = useState<Status | null>(null);
  const [gameId, setGameId] = useState("garden_cycle");
  const [fps, setFps] = useState(30);
  const [resolution, setResolution] = useState("1920x1080");
  const [count, setCount] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = () => api<Status>("/gameplay/status").then(setData).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);
  const catalog = data?.catalog?.length ? data.catalog : fallbackCatalog;
  const profilePool = useMemo(() => (data?.pool || []).filter((row) => (row.game_id || "battle_royale") === gameId), [data, gameId]);
  const generate = async () => {
    setBusy(true); setError("");
    const [width, height] = resolution.split("x");
    const form = new FormData();
    form.append("game_id", gameId); form.append("width", width); form.append("height", height);
    form.append("fps", String(fps)); form.append("count", String(count));
    try { await postForm("/gameplay/generate", form); await load(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const toggleGame = async (game: Game) => {
    setBusy(true); setError(""); const form = new FormData(); form.append("enabled", String(!game.enabled));
    try { await postForm(`/gameplay/games/${game.id}/toggle`, form); await load(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const uploadPack = async (file?: File) => {
    if (!file) return;
    setBusy(true); setError(""); const form = new FormData(); form.append("file", file);
    try { await postForm("/gameplay/theme-packs/upload", form); await load(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const upload = async (file?: File) => {
    if (!file) return;
    setBusy(true); setError(""); const form = new FormData(); form.append("file", file);
    try { await postForm("/gameplay/themes/upload", form); await load(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const toggle = async (theme: Theme) => {
    setBusy(true); setError(""); const form = new FormData(); form.append("enabled", String(!theme.enabled));
    try { await postForm(`/gameplay/themes/${theme.id}/${theme.version}/toggle`, form); await load(); } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const remove = async (theme: Theme) => {
    if (!confirm(`Xóa theme ${theme.name}?`)) return;
    try { await del(`/gameplay/themes/${theme.id}/${theme.version}`); await load(); } catch (e) { setError(String(e)); }
  };
  const prompt = "Calm 2D gameplay theme, readable silhouettes, restrained motion, no text, no watermark, no violence. Provide a consistent pixel-art or neon-geometry palette and transparent assets.";
  if (!data) return <><Header title="Gameplay" subtitle="Catalog nền deterministic cho audiobook" />{error ? <p className="text-destructive">{error}</p> : <LoadingState />}</>;
  const available = profilePool.filter((p) => p.status === "available").reduce((sum, p) => sum + p.duration_seconds, 0);
  return <div className="space-y-6">
    <Header title="Gameplay" subtitle="Catalog Pixel và Neon · replay deterministic · CPU renderer" />
    {error && <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
    <section className="grid gap-4 md:grid-cols-3">
      {catalog.map((game) => <Card key={game.id} className={game.id === gameId ? "border-primary" : ""}>
        <CardHeader><CardTitle className="flex items-center justify-between gap-2"><span>{game.name}</span><Badge variant={game.family === "legacy" ? "secondary" : "outline"}>{game.family}</Badge></CardTitle></CardHeader>
        <CardContent className="space-y-3"><p className="min-h-10 text-xs text-muted-foreground">{game.description}</p><div className="flex flex-wrap gap-2 text-[11px]"><Badge variant="secondary">3–5 phút</Badge><Badge variant="secondary">{game.waveform_policy}</Badge></div>{!!game.sprite_roles?.length && <p className="font-mono text-[10px] text-muted-foreground">sprite: {game.sprite_roles.map((r) => `${r}.png`).join(", ")}</p>}<div className="flex gap-2"><Button size="sm" variant={game.id === gameId ? "default" : "outline"} onClick={() => setGameId(game.id)} disabled={!game.enabled}>Chọn</Button><Button size="sm" variant="ghost" disabled={busy} onClick={() => toggleGame(game)}>{game.enabled ? "Tắt" : "Bật"}</Button></div></CardContent>
      </Card>)}
    </section>
    <section className="grid gap-4 lg:grid-cols-3">
      <Card><CardHeader><CardTitle className="flex items-center gap-2"><Sparkles className="h-5 w-5" />Tạo clip</CardTitle></CardHeader><CardContent className="space-y-3">
        <select className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={resolution} onChange={(e) => setResolution(e.target.value)}>{["1920x1080", "1280x720", "854x480", "1080x1920", "1080x1080"].map((v) => <option key={v}>{v}</option>)}</select>
        <div className="grid grid-cols-2 gap-2"><select className="h-9 rounded-md border bg-background px-2 text-sm" value={fps} onChange={(e) => setFps(Number(e.target.value))}>{[24,30,60].map((v) => <option key={v} value={v}>{v} FPS</option>)}</select><input className="h-9 rounded-md border bg-background px-3 text-sm" type="number" min={1} max={10} value={count} onChange={(e) => setCount(Math.max(1, Math.min(10, Number(e.target.value))))} /></div>
        <Button className="w-full" onClick={generate} disabled={busy}><Plus className="mr-2 h-4 w-4" />Tạo / lấp pool</Button>
      </CardContent></Card>
      <Card><CardHeader><CardTitle>Pool của game</CardTitle></CardHeader><CardContent><div className="font-mono text-3xl font-black">{(available / 60).toFixed(1)} phút</div><p className="mt-2 text-xs text-muted-foreground">Mục tiêu {(data.target_seconds / 60).toFixed(0)} phút / profile</p><div className="mt-4 space-y-2">{profilePool.length ? profilePool.map((row) => <div key={`${row.profile_key}-${row.status}`} className="flex justify-between text-xs"><Badge variant="secondary">{row.status}</Badge><span className="font-mono">{row.clip_count} · {(row.duration_seconds/60).toFixed(1)}m</span></div>) : <p className="text-xs text-muted-foreground">Chưa có clip cho game/profile này.</p>}</div></CardContent></Card>
      <Card><CardHeader><CardTitle className="flex items-center gap-2"><Shield className="h-5 w-5" />Renderer</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">CPU / Pillow / NumPy / raw RGB / libx264<br />Replay trước, render sau · video-only<br />Resolution và FPS theo cấu hình sách{data.health && <><br />Failed clips: {data.health.failed_clips}{data.health.oldest_lease_at && <> · lease cũ nhất: {data.health.oldest_lease_at}</>}</>}</CardContent></Card>
    </section>
    <section><h2 className="mb-3 flex items-center gap-2 text-lg font-bold"><Trophy className="h-5 w-5" />Battle Royale Legacy</h2><div className="overflow-x-auto rounded-lg border"><table className="w-full text-sm"><thead className="bg-muted/50 text-left text-xs uppercase"><tr><th className="p-3">Fighter</th><th>Class</th><th>Matches</th><th>Wins</th><th>Eliminations</th></tr></thead><tbody>{data.fighters.slice(0, 10).map((f) => <tr key={f.id} className="border-t"><td className="p-3 font-semibold">{f.name}</td><td><Badge variant="outline">{f.class_name}</Badge></td><td>{f.matches}</td><td>{f.wins}</td><td>{f.eliminations}</td></tr>)}</tbody></table></div></section>
    <section className="space-y-3"><div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center"><div><h2 className="text-lg font-bold">Pixel / Neon Theme Packs v2</h2><p className="text-xs text-muted-foreground">Pack immutable theo ID, version và SHA-256; ZIP được kiểm tra trước khi publish. theme.json cần khai báo <code className="font-mono">supported_games.&lt;game_id&gt;.assets</code> ánh xạ các role ở trên (vd. carrot.png) sang PNG trong ZIP — game nào không có role khớp sẽ tự dùng hình vẽ mặc định.</p></div><label><input type="file" accept=".zip" className="hidden" onChange={(e) => uploadPack(e.target.files?.[0])} /><Button asChild variant="outline"><span><Upload className="mr-2 h-4 w-4" />Upload Pack v2</span></Button></label></div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{(data.theme_packs || []).map((pack) => <Card key={`${pack.id}-${pack.version}`}><CardHeader><CardTitle className="flex justify-between gap-2"><span>{pack.name}</span><Badge variant="outline">{pack.family}</Badge></CardTitle></CardHeader><CardContent className="space-y-2 text-xs"><div className="font-mono">{pack.id} · v{pack.version}</div><div className="truncate font-mono text-muted-foreground" title={pack.content_sha256}>{pack.content_sha256}</div><Badge variant={pack.validation_status === "valid" ? "default" : "destructive"}>{pack.validation_status}</Badge>{pack.error_message && <p className="text-destructive">{pack.error_message}</p>}</CardContent></Card>)}{!(data.theme_packs || []).length && <p className="text-xs text-muted-foreground">Các game dùng built-in fallback; chưa có custom pack.</p>}</div>
    </section>
    <section className="space-y-3"><div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center"><div><h2 className="text-lg font-bold">Legacy Army Theme Packs</h2><p className="text-xs text-muted-foreground">Áp dụng cho Battle Royale; pack Pixel/Neon v2 sẽ được quản lý riêng.</p></div><label><input type="file" accept=".zip" className="hidden" onChange={(e) => upload(e.target.files?.[0])} /><Button asChild variant="outline"><span><Upload className="mr-2 h-4 w-4" />Upload ZIP</span></Button></label></div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{data.themes.map((theme) => <Card key={`${theme.id}-${theme.version}`}><CardHeader><CardTitle className="flex justify-between"><span>{theme.name}</span><Badge variant={theme.enabled ? "default" : "secondary"}>v{theme.version}</Badge></CardTitle></CardHeader><CardContent className="space-y-3"><div className="text-xs font-mono text-muted-foreground">{theme.id}</div>{theme.error_message && <p className="text-xs text-destructive">{theme.error_message}</p>}<div className="flex gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => toggle(theme)}>{theme.enabled ? "Tắt" : "Bật"}</Button>{!theme.builtin && <Button size="sm" variant="destructive" onClick={() => remove(theme)}>Xóa</Button>}</div></CardContent></Card>)}</div>
      <Card><CardHeader><CardTitle>Prompt template</CardTitle></CardHeader><CardContent><p className="rounded-md bg-muted p-3 text-xs leading-relaxed">{prompt}</p><div className="mt-3 flex gap-2"><Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(prompt)}><Copy className="mr-2 h-4 w-4" />Copy</Button><Button size="sm" variant="outline" asChild><a download="gameplay-theme-prompt.txt" href={`data:text/plain;charset=utf-8,${encodeURIComponent(prompt)}`}><Download className="mr-2 h-4 w-4" />Download</a></Button></div></CardContent></Card>
    </section>
  </div>;
}
