import React, { useEffect, useMemo, useState } from "react";
import { api, postForm } from "@/api";
import { Header, LoadingState } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Crown, FolderOpen, Gamepad2, Plus, Shield, Sparkles, X } from "lucide-react";

type PoolRow = { profile_key: string; game_id?: string | null; status: string; clip_count: number; duration_seconds: number };
type Fighter = { id: number; name: string; class_name: string; matches: number; wins: number; eliminations: number };
type Game = { id: string; name: string; family: "retro" | "procedural"; waveform_policy: string; description: string; enabled: boolean; sprite_roles?: string[] };
type Entry = { position: number; game_id: string; player_tag: string; score: number; rating: number; rank_tier: string; level: number; games: number; deaths: number; rendered: number; duration_seconds: number; metrics: Record<string, number> };
type Standing = { game_id: string; runs: number; best: number; average: number; rendered: number; deaths: number; top_level: number; champion: string; last_run_at?: string };
type StorageFolder = { name: string; count: number };
type Storage = { root: string; folders: StorageFolder[] };
type BatchResult = { requested: number; queued: number; game_ids: string[]; per_game: Record<string, number>; job_id: number | null; dest_path: string | null; offset: number };
type Status = { catalog?: Game[]; pool: PoolRow[]; target_seconds: number; fighters: Fighter[]; leaderboard?: Entry[]; standings?: Standing[]; stat_labels?: Record<string, [string, string][]>; health?: { failed_clips: number; oldest_lease_at?: string | null }; storage?: Storage; batch_limit?: number };

const gameplayPreview = (gameId: string) => ({
  video: `/gameplay/${gameId}.mp4`,
  poster: `/gameplay/${gameId}.jpg`,
});
const TIER_STYLE: Record<string, string> = { S: "bg-amber-400/20 text-amber-300 border-amber-400/40", A: "bg-emerald-400/20 text-emerald-300 border-emerald-400/40", B: "bg-sky-400/15 text-sky-300 border-sky-400/30" };
const COUNT_PRESETS = [10, 50, 100, 500, 1000];

const fallbackCatalog: Game[] = [
  { id: "snake_arena", name: "Rắn Săn Mồi", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Con rắn tự săn mồi, dài ra và tăng tốc theo từng cấp.", enabled: true },
  { id: "brick_stack", name: "Xếp Gạch", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Tetris cổ điển: khối rơi, xếp kín hàng và phá hàng liên tiếp.", enabled: true },
  { id: "tank_duel", name: "Xe Tăng 90", family: "retro", waveform_policy: "default_off", description: "Xe tăng bắn phá tường gạch, diệt địch và giữ căn cứ.", enabled: true },
  { id: "brick_breaker", name: "Đập Gạch", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Thanh trượt đỡ bóng, phá sạch tường gạch nhiều màu.", enabled: true },
  { id: "star_defender", name: "Bắn Ruồi", family: "retro", waveform_policy: "default_off", description: "Đội hình địch tiến xuống, phi thuyền né bom và bắn trả.", enabled: true },
  { id: "pixel_dash", name: "Đua Xe", family: "retro", waveform_policy: "forbidden", description: "Xe lách qua dòng xe ngược chiều, càng lâu càng nhanh.", enabled: true },
  { id: "pacman_maze", name: "Pac-Man", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Ăn chấm trong mê cung, né ma và săn ma khi nhặt viên năng lượng.", enabled: true },
  { id: "chicken_shooter", name: "Phi Thuyền Bắn Gà", family: "retro", waveform_policy: "default_off", description: "Phi thuyền tự né trứng, bắn đội hình gà không gian theo từng đợt.", enabled: true },
  { id: "spaceship_voyager", name: "Phi Thuyền", family: "retro", waveform_policy: "default_off", description: "Bay xuyên trường sao, né thiên thạch và đối đầu ba Boss với kỹ năng riêng.", enabled: true },
  { id: "flappy_bird", name: "Flappy Bird", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Chú chim tự vỗ cánh luồn qua ống nước, tốc độ tăng dần theo quãng bay.", enabled: true },
  { id: "gold_miner", name: "Đào Vàng", family: "retro", waveform_policy: "allowed_with_safe_area", description: "Móc máy tự săn vàng và đá quý, kéo kho báu lên để mở tầng mỏ mới.", enabled: true },
  { id: "aurora_veil", name: "Aurora Veil", family: "procedural", waveform_policy: "allowed_with_safe_area", description: "Rèm cực quang trôi trên nền sao, sáng dần theo từng đợt.", enabled: true },
  { id: "plasma_tide", name: "Plasma Tide", family: "procedural", waveform_policy: "default_off", description: "Sóng plasma giao thoa với các đường viền phát sáng.", enabled: true },
  { id: "ripple_pond", name: "Ripple Pond", family: "procedural", waveform_policy: "allowed_with_safe_area", description: "Mặt nước tĩnh với những vòng sóng lan và ánh khúc xạ.", enabled: true },
  { id: "lumen_bloom", name: "Lumen Bloom", family: "procedural", waveform_policy: "default_off", description: "Hoa ánh sáng xoè theo dãy Fibonacci, xoay chậm và đổi sắc.", enabled: true },
  { id: "silk_current", name: "Silk Current", family: "procedural", waveform_policy: "default_off", description: "Hàng nghìn hạt sáng chảy theo dòng xoáy, để lại vệt lụa.", enabled: true },
  { id: "starfall_warp", name: "Starfall Warp", family: "procedural", waveform_policy: "forbidden", description: "Bay xuyên trường sao với vệt kéo dài và sao chổi.", enabled: true },
];

const formatScore = (value: number) => value.toLocaleString("vi-VN");
const DEFAULT_GAME = "snake_arena";

export function GameplayPage() {
  const [data, setData] = useState<Status | null>(null);
  const [board, setBoard] = useState<Entry[]>([]);
  const [boardGameId, setBoardGameId] = useState(DEFAULT_GAME);
  const [selectedIds, setSelectedIds] = useState<string[]>([DEFAULT_GAME]);
  const [fps, setFps] = useState(30);
  const [resolution, setResolution] = useState("1920x1080");
  const [count, setCount] = useState(10);
  const [destFolder, setDestFolder] = useState("");
  const [newFolder, setNewFolder] = useState("");
  const [lastBatch, setLastBatch] = useState<BatchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [previewGameId, setPreviewGameId] = useState("");
  const [previewVersion, setPreviewVersion] = useState<Record<string, number>>({});
  const [error, setError] = useState("");

  const load = () => api<Status>("/gameplay/status").then(setData).catch((e) => setError(String(e)));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    api<{ entries: Entry[] }>(`/gameplay/leaderboard?game_id=${boardGameId}&limit=10`)
      .then((payload) => setBoard(payload.entries)).catch(() => setBoard([]));
  }, [boardGameId, data]);

  const catalog = data?.catalog?.length ? data.catalog : fallbackCatalog;
  const batchLimit = data?.batch_limit ?? 1000;
  const names = useMemo(() => Object.fromEntries(catalog.map((game) => [game.id, game.name])), [catalog]);
  const enabledIds = useMemo(() => catalog.filter((game) => game.enabled).map((game) => game.id), [catalog]);

  // Drop games from the selection as soon as they disappear or get disabled.
  useEffect(() => {
    setSelectedIds((current) => {
      const next = current.filter((id) => enabledIds.includes(id));
      if (next.length) return next.length === current.length ? current : next;
      return enabledIds.includes(DEFAULT_GAME) ? [DEFAULT_GAME] : enabledIds.slice(0, 1);
    });
  }, [enabledIds]);

  const toggleSelect = (id: string) =>
    setSelectedIds((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id]);
  const selectAll = () => setSelectedIds(enabledIds);
  const clearSelection = () => setSelectedIds([]);

  const profilePool = useMemo(
    () => (data?.pool || []).filter((row) => row.game_id && selectedIds.includes(row.game_id)),
    [data, selectedIds]);
  const available = profilePool.filter((row) => row.status === "available").reduce((sum, row) => sum + row.duration_seconds, 0);

  const storage = data?.storage;
  const storageRoot = storage?.root || "data/backgrounds";
  const storageFolders = storage?.folders || [];
  const destName = newFolder.trim() || destFolder;
  const destPreview = destName ? `${storageRoot} / ${destName}` : storageRoot;

  const generate = async () => {
    if (!selectedIds.length) { setError("Chọn ít nhất một game để tạo hàng loạt."); return; }
    setBusy(true); setError(""); setLastBatch(null);
    const [width, height] = resolution.split("x");
    const form = new FormData();
    form.append("game_ids", selectedIds.join(","));
    form.append("game_id", selectedIds[0]);
    form.append("width", width); form.append("height", height);
    form.append("fps", String(fps)); form.append("count", String(count));
    form.append("dest_folder", newFolder.trim() || destFolder || "");
    try {
      const result = await postForm<BatchResult>("/gameplay/generate", form);
      setLastBatch(result);
      if (newFolder.trim()) { setDestFolder(newFolder.trim()); setNewFolder(""); }
      await load();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const toggleGame = async (game: Game) => {
    setBusy(true); setError("");
    const form = new FormData(); form.append("enabled", String(!game.enabled));
    try {
      await postForm(`/gameplay/games/${game.id}/toggle`, form);
      if (game.enabled) setSelectedIds((current) => current.filter((id) => id !== game.id));
      await load();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const generatePreview = async (id: string) => {
    setPreviewGameId(id); setError("");
    try { await postForm(`/gameplay/previews/${id}`, new FormData()); setPreviewVersion((current) => ({ ...current, [id]: Date.now() })); }
    catch (e) { setError(String(e)); } finally { setPreviewGameId(""); }
  };
  const clampCount = (value: number) => Math.max(1, Math.min(batchLimit, Math.round(value) || 1));

  if (!data) return <><Header title="Gameplay" subtitle="Catalog nền deterministic cho audiobook" />{error ? <p className="text-destructive">{error}</p> : <LoadingState />}</>;
  const statLabels = data.stat_labels?.[boardGameId] || [];
  const standings = data.standings || [];
  const tier = (value: string) => <Badge variant="outline" className={TIER_STYLE[value] || ""}>{value}</Badge>;
  return <div className="space-y-6">
    <Header title="Gameplay" subtitle="Game console cầm tay · khung 16:9 · không cần asset · tạo hàng loạt tới 1000 video vào thư viện background" />
    {error && <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}

    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="outline" onClick={selectAll}>Chọn tất cả</Button>
        <Button size="sm" variant="ghost" onClick={clearSelection}>Bỏ chọn</Button>
      </div>
      <span className="text-xs text-muted-foreground">Đã chọn <b>{selectedIds.length}</b>/{catalog.length} game · tối đa {batchLimit} video mỗi lần tạo</span>
    </div>

    <section className="grid gap-4 md:grid-cols-3">
      {catalog.map((game) => {
        const selected = selectedIds.includes(game.id);
        return <Card key={game.id} className={selected ? "overflow-hidden border-primary ring-1 ring-primary/40" : "overflow-hidden"}>
          <video
            className="aspect-video w-full bg-muted object-cover"
            src={`${gameplayPreview(game.id).video}?v=${previewVersion[game.id] || 0}`}
            poster={`${gameplayPreview(game.id).poster}?v=${previewVersion[game.id] || 0}`}
            muted
            loop
            playsInline
            autoPlay
            preload="metadata"
            aria-label={`Preview ${game.name}`}
          />
          <CardHeader><CardTitle className="flex items-center justify-between gap-2">
            <label className="flex items-center gap-2 min-w-0 cursor-pointer">
              <input
                type="checkbox"
                className="h-4 w-4 shrink-0 accent-primary"
                checked={selected}
                disabled={!game.enabled}
                onChange={() => toggleSelect(game.id)}
                aria-label={`Chọn ${game.name} để tạo hàng loạt`}
              />
              <span className="truncate">{game.name}</span>
            </label>
            <Badge variant="outline">{game.family}</Badge>
          </CardTitle></CardHeader>
          <CardContent className="space-y-3"><p className="min-h-10 text-xs text-muted-foreground">{game.description}</p><div className="flex flex-wrap gap-2 text-[11px]"><Badge variant="secondary">3–5 phút</Badge><Badge variant="secondary">{game.waveform_policy}</Badge>{standings.find((row) => row.game_id === game.id) && <Badge variant="secondary">HI {formatScore(standings.find((row) => row.game_id === game.id)!.best)}</Badge>}{!game.enabled && <Badge variant="secondary" className="text-destructive">Đang tắt</Badge>}</div>{game.family === "retro" && <p className="text-[10px] text-muted-foreground">Máy chơi game cầm tay: bàn cờ ô vuông nhiều màu, HUD điểm số — không dùng ảnh hay theme pack.</p>}{game.family === "procedural" && <p className="text-[10px] text-muted-foreground">Không cần asset — render bằng palette LUT, sóng giải tích và glow cộng dồn.</p>}<div className="flex flex-wrap gap-2">
            <Button size="sm" variant={boardGameId === game.id ? "default" : "outline"} onClick={() => setBoardGameId(game.id)}>Xem BXH</Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => toggleGame(game)}>{game.enabled ? "Tắt" : "Bật"}</Button>
            <Button size="sm" variant="outline" disabled={!!previewGameId} onClick={() => generatePreview(game.id)}>{previewGameId === game.id ? "Đang tạo..." : "Tạo video preview"}</Button>
          </div></CardContent>
        </Card>;
      })}
    </section>

    <section className="grid gap-4 lg:grid-cols-3">
      <Card className="lg:col-span-2"><CardHeader><CardTitle className="flex items-center gap-2"><Sparkles className="h-5 w-5" />Tạo hàng loạt</CardTitle></CardHeader><CardContent className="space-y-4">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs font-semibold uppercase text-muted-foreground">Game đã chọn</span>
            <span className="text-[11px] text-muted-foreground">Mỗi video lấy seed ngẫu nhiên, chia đều theo game đã chọn.</span>
          </div>
          {selectedIds.length ? <div className="flex flex-wrap gap-2">{selectedIds.map((id) => <span key={id} className="inline-flex items-center gap-1 rounded-full border bg-secondary/50 px-2 py-1 text-xs">
            {names[id] || id}
            <button type="button" className="text-muted-foreground transition-colors hover:text-foreground" onClick={() => toggleSelect(id)} aria-label={`Bỏ chọn ${names[id] || id}`}><X className="h-3 w-3" /></button>
          </span>)}</div> : <p className="text-xs text-destructive">Chưa chọn game nào — tích vào thẻ game phía trên để bắt đầu.</p>}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-xs font-semibold uppercase text-muted-foreground">
            Độ phân giải
            <select className="h-9 w-full rounded-md border bg-background px-3 text-sm font-normal normal-case" value={resolution} onChange={(e) => setResolution(e.target.value)}>{["1920x1080", "1280x720", "854x480", "1080x1920", "1080x1080"].map((v) => <option key={v}>{v}</option>)}</select>
          </label>
          <label className="space-y-1 text-xs font-semibold uppercase text-muted-foreground">
            Khung hình
            <select className="h-9 w-full rounded-md border bg-background px-3 text-sm font-normal normal-case" value={fps} onChange={(e) => setFps(Number(e.target.value))}>{[24, 30, 60].map((v) => <option key={v} value={v}>{v} FPS</option>)}</select>
          </label>
        </div>

        <div className="space-y-2">
          <label className="space-y-1 text-xs font-semibold uppercase text-muted-foreground block">
            Số video cần tạo
            <Input type="number" min={1} max={batchLimit} value={count} onChange={(e) => setCount(clampCount(Number(e.target.value)))} className="font-mono" />
          </label>
          <div className="flex flex-wrap gap-2">{COUNT_PRESETS.map((preset) => <Button key={preset} size="sm" variant={count === preset ? "default" : "outline"} onClick={() => setCount(clampCount(preset))}>{preset}</Button>)}</div>
        </div>

        <div className="space-y-3 rounded-md border p-3">
          <div className="flex items-center gap-2 text-xs font-semibold uppercase text-muted-foreground"><FolderOpen className="h-4 w-4" />Nơi lưu trữ</div>
          <label className="space-y-1 text-xs text-muted-foreground block">
            Thư mục trong data/backgrounds
            <select className="h-9 w-full rounded-md border bg-background px-3 text-sm text-foreground" value={destFolder} onChange={(e) => setDestFolder(e.target.value)}>
              <option value="">(gốc thư viện background)</option>
              {storageFolders.map((folder) => <option key={folder.name} value={folder.name}>{folder.name} · {folder.count} file</option>)}
            </select>
          </label>
          <label className="space-y-1 text-xs text-muted-foreground block">
            Hoặc tạo / dùng thư mục mới
            <Input placeholder="vd: gameplay-bg" value={newFolder} onChange={(e) => setNewFolder(e.target.value)} />
          </label>
          <p className="text-[11px] text-muted-foreground">Video lưu vào: <code className="break-all">{destPreview}</code></p>
        </div>

        <Button className="w-full" onClick={generate} disabled={busy || !selectedIds.length}>
          <Plus className="mr-2 h-4 w-4" />{busy ? "Đang xếp hàng..." : `Tạo ${count} video${selectedIds.length > 1 ? ` từ ${selectedIds.length} game` : ""}`}
        </Button>

        {lastBatch && <div className="rounded-md border border-emerald-400/40 bg-emerald-400/10 p-3 text-xs text-emerald-200">
          Đã lên lịch <b>{lastBatch.queued}</b>/{lastBatch.requested} video{lastBatch.job_id != null ? <> · job #{lastBatch.job_id}</> : null}{lastBatch.dest_path ? <> · lưu tại <code className="break-all">{lastBatch.dest_path}</code></> : null}.
          {Object.keys(lastBatch.per_game).length > 1 && <> Phân bổ: {Object.entries(lastBatch.per_game).map(([id, total]) => `${names[id] || id}: ${total}`).join(", ")}.</>}
          <span className="block text-[11px] opacity-80">Replay + render chạy nền theo hàng đợi; theo dõi ở trang Queue.</span>
        </div>}
      </CardContent></Card>

      <div className="space-y-4">
        <Card><CardHeader><CardTitle>Pool đã tạo</CardTitle></CardHeader><CardContent>
          <div className="font-mono text-3xl font-black">{(available / 60).toFixed(1)} phút</div>
          <p className="mt-2 text-xs text-muted-foreground">Mục tiêu {(data.target_seconds / 60).toFixed(0)} phút / profile</p>
          <div className="mt-4 space-y-2">{profilePool.length ? profilePool.map((row) => <div key={`${row.profile_key}-${row.status}`} className="flex items-center justify-between gap-2 text-xs">
            <span className="flex min-w-0 items-center gap-2"><Badge variant="secondary">{row.status}</Badge><span className="truncate text-muted-foreground">{names[row.game_id || ""] || row.game_id}</span></span>
            <span className="shrink-0 font-mono">{row.clip_count} · {(row.duration_seconds / 60).toFixed(1)}m</span>
          </div>) : <p className="text-xs text-muted-foreground">Chưa có clip cho game đã chọn.</p>}</div>
        </CardContent></Card>
        <Card><CardHeader><CardTitle className="flex items-center gap-2"><Shield className="h-5 w-5" />Renderer</CardTitle></CardHeader><CardContent className="text-sm text-muted-foreground">CPU / Pillow / NumPy / raw RGB / libx264<br />Replay trước, render sau · video-only<br />Resolution và FPS theo cấu hình sách{data.health && <><br />Failed clips: {data.health.failed_clips}{data.health.oldest_lease_at && <> · lease cũ nhất: {data.health.oldest_lease_at}</>}</>}</CardContent></Card>
      </div>
    </section>

    <section className="grid gap-4 lg:grid-cols-2">
      <Card><CardHeader><CardTitle className="flex items-center justify-between gap-2"><span className="flex items-center gap-2"><Gamepad2 className="h-5 w-5" />Bảng xếp hạng</span>
        <select className="h-8 rounded-md border bg-background px-2 text-xs font-normal" value={boardGameId} onChange={(e) => setBoardGameId(e.target.value)}>{catalog.map((game) => <option key={game.id} value={game.id}>{game.name}</option>)}</select>
      </CardTitle></CardHeader><CardContent>
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
  </div>;
}
