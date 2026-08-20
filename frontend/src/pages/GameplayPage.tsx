import React, { useEffect, useMemo, useState } from "react";
import { api, del, postForm } from "@/api";
import { Header, LoadingState } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Copy, Crown, Download, Gamepad2, Plus, Shield, Sparkles, Trophy, Upload } from "lucide-react";

type PoolRow = { profile_key: string; game_id?: string | null; status: string; clip_count: number; duration_seconds: number };
type Fighter = { id: number; name: string; class_name: string; matches: number; wins: number; eliminations: number };
type Theme = { id: string; version: number; name: string; enabled: number; builtin: number; asset_dir?: string; error_message?: string };
type Game = { id: string; name: string; family: "retro" | "procedural" | "legacy"; waveform_policy: string; description: string; enabled: boolean; sprite_roles?: string[] };
type Entry = { position: number; game_id: string; player_tag: string; score: number; rating: number; rank_tier: string; level: number; games: number; deaths: number; rendered: number; duration_seconds: number; metrics: Record<string, number> };
type Standing = { game_id: string; runs: number; best: number; average: number; rendered: number; deaths: number; top_level: number; champion: string; last_run_at?: string };
type Status = { catalog?: Game[]; pool: PoolRow[]; target_seconds: number; fighters: Fighter[]; themes: Theme[]; leaderboard?: Entry[]; standings?: Standing[]; stat_labels?: Record<string, [string, string][]>; health?: { failed_clips: number; oldest_lease_at?: string | null } };

const gameplayPreview = (gameId: string) => `/gameplay/${gameId}.jpg`;
const TIER_STYLE: Record<string, string> = { S: "bg-amber-400/20 text-amber-300 border-amber-400/40", A: "bg-emerald-400/20 text-emerald-300 border-emerald-400/40", B: "bg-sky-400/15 text-sky-300 border-sky-400/30" };

const fallbackCatalog: Game[] = [
  { id: "snake_arena", name: "Rắn Săn Mồi", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Con rắn tự săn mồi, dài ra và tăng tốc theo từng cấp.", enabled: true },
  { id: "brick_stack", name: "Xếp Gạch", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Tetris cổ điển: khối rơi, xếp kín hàng và phá hàng liên tiếp.", enabled: true },
  { id: "tank_duel", name: "Xe Tăng 90", family: "retro", waveform_policy: "default_off", description: "Xe tăng bắn phá tường gạch, diệt địch và giữ căn cứ.", enabled: true },
  { id: "brick_breaker", name: "Đập Gạch", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Thanh trượt đỡ bóng, phá sạch tường gạch nhiều màu.", enabled: true },
  { id: "star_defender", name: "Bắn Ruồi", family: "retro", waveform_policy: "default_off", description: "Đội hình địch tiến xuống, phi thuyền né bom và bắn trả.", enabled: true },
  { id: "pixel_dash", name: "Đua Xe", family: "retro", waveform_policy: "forbidden", description: "Xe lách qua dòng xe ngược chiều, càng lâu càng nhanh.", enabled: true },
  { id: "aurora_veil", name: "Aurora Veil", family: "procedural", waveform_policy: "allowed_with_safe_area", description: "Rèm cực quang trôi trên nền sao, sáng dần theo từng đợt.", enabled: true },
  { id: "plasma_tide", name: "Plasma Tide", family: "procedural", waveform_policy: "default_off", description: "Sóng plasma giao thoa với các đường viền phát sáng.", enabled: true },
  { id: "ripple_pond", name: "Ripple Pond", family: "procedural", waveform_policy: "allowed_with_safe_area", description: "Mặt nước tĩnh với những vòng sóng lan và ánh khúc xạ.", enabled: true },
  { id: "lumen_bloom", name: "Lumen Bloom", family: "procedural", waveform_policy: "default_off", description: "Hoa ánh sáng xoè theo dãy Fibonacci, xoay chậm và đổi sắc.", enabled: true },
  { id: "silk_current", name: "Silk Current", family: "procedural", waveform_policy: "default_off", description: "Hàng nghìn hạt sáng chảy theo dòng xoáy, để lại vệt lụa.", enabled: true },
  { id: "starfall_warp", name: "Starfall Warp", family: "procedural", waveform_policy: "forbidden", description: "Bay xuyên trường sao với vệt kéo dài và sao chổi.", enabled: true },
  { id: "battle_royale", name: "Neon Battle Royale", family: "legacy", waveform_policy: "forbidden", description: "Chế độ tương thích cũ.", enabled: true },
];

const formatScore = (value: number) => value.toLocaleString("vi-VN");

export function GameplayPage() {
  const [data, setData] = useState<Status | null>(null);
  const [board, setBoard] = useState<Entry[]>([]);
  const [gameId, setGameId] = useState("snake_arena");
  const [fps, setFps] = useState(30);
  const [resolution, setResolution] = useState("1920x1080");
  const [count, setCount] = useState(1);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = () => api<Status>("/gameplay/status").then(setData).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    api<{ entries: Entry[] }>(`/gameplay/leaderboard?game_id=${gameId}&limit=10`)
      .then((payload) => setBoard(payload.entries)).catch(() => setBoard([]));
  }, [gameId, data]);
  const catalog = data?.catalog?.length ? data.catalog : fallbackCatalog;
  const names = useMemo(() => Object.fromEntries(catalog.map((game) => [game.id, game.name])), [catalog]);
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
  const statLabels = data.stat_labels?.[gameId] || [];
  const standings = data.standings || [];
  const tier = (value: string) => <Badge variant="outline" className={TIER_STYLE[value] || ""}>{value}</Badge>;
  return <div className="space-y-6">
    <Header title="Gameplay" subtitle="Game console cầm tay · khung 16:9 · không cần asset · replay deterministic" />
    {error && <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
    <section className="grid gap-4 md:grid-cols-3">
      {catalog.map((game) => <Card key={game.id} className={game.id === gameId ? "overflow-hidden border-primary" : "overflow-hidden"}>
        <img className="aspect-video w-full bg-muted object-cover" src={gameplayPreview(game.id)} alt={`Preview ${game.name}`} loading="lazy" />
        <CardHeader><CardTitle className="flex items-center justify-between gap-2"><span>{game.name}</span><Badge variant={game.family === "legacy" ? "secondary" : "outline"}>{game.family}</Badge></CardTitle></CardHeader>
        <CardContent className="space-y-3"><p className="min-h-10 text-xs text-muted-foreground">{game.description}</p><div className="flex flex-wrap gap-2 text-[11px]"><Badge variant="secondary">3–5 phút</Badge><Badge variant="secondary">{game.waveform_policy}</Badge>{standings.find((row) => row.game_id === game.id) && <Badge variant="secondary">HI {formatScore(standings.find((row) => row.game_id === game.id)!.best)}</Badge>}</div>{game.family === "retro" && <p className="text-[10px] text-muted-foreground">Máy chơi game cầm tay: bàn cờ ô vuông nhiều màu, HUD điểm số — không dùng ảnh hay theme pack.</p>}{game.family === "procedural" && <p className="text-[10px] text-muted-foreground">Không cần asset — render bằng palette LUT, sóng giải tích và glow cộng dồn.</p>}<div className="flex gap-2"><Button size="sm" variant={game.id === gameId ? "default" : "outline"} onClick={() => setGameId(game.id)} disabled={!game.enabled}>Chọn</Button><Button size="sm" variant="ghost" disabled={busy} onClick={() => toggleGame(game)}>{game.enabled ? "Tắt" : "Bật"}</Button></div></CardContent>
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
    <section className="grid gap-4 lg:grid-cols-2">
      <Card><CardHeader><CardTitle className="flex items-center gap-2"><Gamepad2 className="h-5 w-5" />Bảng xếp hạng · {names[gameId] || gameId}</CardTitle></CardHeader><CardContent>
        {board.length ? <div className="overflow-x-auto"><table className="w-full text-sm"><thead className="text-left text-xs uppercase text-muted-foreground"><tr><th className="py-2">#</th><th>Người chơi</th><th className="text-right">Điểm</th><th>Hạng</th><th className="text-right">Cấp</th><th className="text-right">Chết</th>{statLabels.map(([key, label]) => <th key={key} className="text-right">{label}</th>)}</tr></thead>
          <tbody>{board.map((entry) => <tr key={`${entry.position}-${entry.player_tag}`} className="border-t"><td className="py-2 font-mono">{entry.position}</td><td className="font-mono font-semibold">{entry.player_tag}{entry.rendered ? "" : " *"}</td><td className="text-right font-mono font-bold">{formatScore(entry.score)}</td><td>{tier(entry.rank_tier)}</td><td className="text-right font-mono">{entry.level}</td><td className="text-right font-mono">{entry.deaths}</td>{statLabels.map(([key]) => <td key={key} className="text-right font-mono">{entry.metrics?.[key] ?? 0}</td>)}</tr>)}</tbody></table>
          <p className="mt-2 text-[11px] text-muted-foreground">Dấu * là lượt chơi đã mô phỏng nhưng clip chưa render xong. Hạng S nghĩa là phá kỷ lục đang giữ khi trận bắt đầu.</p></div>
          : <p className="text-xs text-muted-foreground">Chưa có lượt chơi nào cho game này — tạo clip để ghi điểm.</p>}
      </CardContent></Card>
      <Card><CardHeader><CardTitle className="flex items-center gap-2"><Crown className="h-5 w-5" />Kỷ lục toàn catalog</CardTitle></CardHeader><CardContent className="space-y-3">
        {standings.length ? <div className="overflow-x-auto"><table className="w-full text-sm"><thead className="text-left text-xs uppercase text-muted-foreground"><tr><th className="py-2">Game</th><th>Quán quân</th><th className="text-right">Kỷ lục</th><th className="text-right">TB</th><th className="text-right">Lượt</th></tr></thead>
          <tbody>{standings.map((row) => <tr key={row.game_id} className="border-t"><td className="py-2 font-semibold">{names[row.game_id] || row.game_id}</td><td className="font-mono text-xs">{row.champion}</td><td className="text-right font-mono font-bold">{formatScore(row.best)}</td><td className="text-right font-mono text-muted-foreground">{formatScore(row.average)}</td><td className="text-right font-mono">{row.runs}</td></tr>)}</tbody></table></div>
          : <p className="text-xs text-muted-foreground">Chưa có dữ liệu xếp hạng.</p>}
        {!!data.leaderboard?.length && <div><h3 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">Top rating chung (0–1000, so với kỷ lục của chính game đó)</h3>
          <div className="space-y-1">{data.leaderboard.slice(0, 6).map((entry) => <div key={`${entry.game_id}-${entry.position}`} className="flex items-center gap-2 text-xs"><span className="w-5 font-mono text-muted-foreground">{entry.position}</span><span className="flex-1 truncate">{names[entry.game_id] || entry.game_id}</span><span className="font-mono">{entry.player_tag}</span>{tier(entry.rank_tier)}<span className="w-12 text-right font-mono font-bold">{entry.rating}</span></div>)}</div></div>}
      </CardContent></Card>
    </section>
    <section><h2 className="mb-3 flex items-center gap-2 text-lg font-bold"><Trophy className="h-5 w-5" />Battle Royale Legacy</h2><div className="overflow-x-auto rounded-lg border"><table className="w-full text-sm"><thead className="bg-muted/50 text-left text-xs uppercase"><tr><th className="p-3">Fighter</th><th>Class</th><th>Matches</th><th>Wins</th><th>Eliminations</th></tr></thead><tbody>{data.fighters.slice(0, 10).map((f) => <tr key={f.id} className="border-t"><td className="p-3 font-semibold">{f.name}</td><td><Badge variant="outline">{f.class_name}</Badge></td><td>{f.matches}</td><td>{f.wins}</td><td>{f.eliminations}</td></tr>)}</tbody></table></div></section>
    <section className="space-y-3"><div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center"><div><h2 className="text-lg font-bold">Legacy Army Theme Packs</h2><p className="text-xs text-muted-foreground">Chỉ áp dụng cho Battle Royale. Catalog Retro và Procedural vẽ hoàn toàn bằng code nên không nhận theme pack.</p></div><label><input type="file" accept=".zip" className="hidden" onChange={(e) => upload(e.target.files?.[0])} /><Button asChild variant="outline"><span><Upload className="mr-2 h-4 w-4" />Upload ZIP</span></Button></label></div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{data.themes.map((theme) => <Card key={`${theme.id}-${theme.version}`}><CardHeader><CardTitle className="flex justify-between"><span>{theme.name}</span><Badge variant={theme.enabled ? "default" : "secondary"}>v{theme.version}</Badge></CardTitle></CardHeader><CardContent className="space-y-3"><div className="text-xs font-mono text-muted-foreground">{theme.id}</div>{theme.error_message && <p className="text-xs text-destructive">{theme.error_message}</p>}<div className="flex gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => toggle(theme)}>{theme.enabled ? "Tắt" : "Bật"}</Button>{!theme.builtin && <Button size="sm" variant="destructive" onClick={() => remove(theme)}>Xóa</Button>}</div></CardContent></Card>)}</div>
      <Card><CardHeader><CardTitle>Prompt template</CardTitle></CardHeader><CardContent><p className="rounded-md bg-muted p-3 text-xs leading-relaxed">{prompt}</p><div className="mt-3 flex gap-2"><Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(prompt)}><Copy className="mr-2 h-4 w-4" />Copy</Button><Button size="sm" variant="outline" asChild><a download="gameplay-theme-prompt.txt" href={`data:text/plain;charset=utf-8,${encodeURIComponent(prompt)}`}><Download className="mr-2 h-4 w-4" />Download</a></Button></div></CardContent></Card>
    </section>
  </div>;
}
