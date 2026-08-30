import React, { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { UploadCloud, FileText, CheckCircle2, AlertCircle, ListMusic, Globe, Tv } from "lucide-react";
import { Header } from "@/components/common/Header";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { api } from "@/api";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

type PlaylistOption = { id: string; title: string };

export function Upload() {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  // Playlist combobox state
  const [parsedTitle, setParsedTitle] = useState("");
  const [parsedDescription, setParsedDescription] = useState("");
  const [parsing, setParsing] = useState(false);
  const [playlists, setPlaylists] = useState<PlaylistOption[]>([]);
  const [playlistsLoading, setPlaylistsLoading] = useState(false);
  const [playlistConnected, setPlaylistConnected] = useState(true);
  const [playlistChoice, setPlaylistChoice] = useState<string>("__auto__");
  const [playlistCountry, setPlaylistCountry] = useState("VN");
  const [customPlaylistTitle, setCustomPlaylistTitle] = useState("");
  const [customPlaylistDesc, setCustomPlaylistDesc] = useState("");

  // Khi chọn file -> parse preview + tải playlists
  useEffect(() => {
    if (!selectedFile) {
      setParsedTitle("");
      setParsedDescription("");
      setCustomPlaylistTitle("");
      setCustomPlaylistDesc("");
      return;
    }
    let cancelled = false;
    setParsing(true);
    setError("");
    const form = new FormData();
    form.append("epub_file", selectedFile);
    fetch("/books/parse-epub?preview_chars=1200", { method: "POST", body: form })
      .then(async (res) => {
        if (!res.ok) throw new Error(`Parse preview thất bại (${res.status})`);
        const data = await res.json();
        if (cancelled) return;
        const title = data.title || selectedFile.name.replace(/\.epub$/i, "");
        const first = data.chapters?.[0]?.text_excerpt || data.chapters?.[0]?.text || "";
        const desc = String(first).slice(0, 1200);
        setParsedTitle(title);
        setParsedDescription(desc);
        setCustomPlaylistTitle(title);
        setCustomPlaylistDesc(desc);
        // auto-detect playlist trùng tên
        // sẽ xử lý sau khi playlists tải xong
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setParsing(false));

    // tải danh sách playlist có sẵn
    setPlaylistsLoading(true);
    api<{ items: { id: string; title: string }[] }>("/youtube/api/playlists")
      .then((res) => {
        if (cancelled) return;
        setPlaylists(res.items || []);
        setPlaylistConnected(true);
      })
      .catch(() => {
        if (cancelled) return;
        setPlaylists([]);
        setPlaylistConnected(false);
      })
      .finally(() => !cancelled && setPlaylistsLoading(false));

    return () => {
      cancelled = true;
    };
  }, [selectedFile]);

  // Tự động detect khi cả title và playlists đã có
  useEffect(() => {
    if (!parsedTitle || !playlists.length) return;
    const norm = parsedTitle.trim().toLowerCase();
    const matched = playlists.find((pl) => pl.title.trim().toLowerCase() === norm);
    if (matched) {
      setPlaylistChoice(matched.id);
    } else {
      setPlaylistChoice("__auto__");
    }
  }, [parsedTitle, playlists]);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!selectedFile) return;
    setError("");
    setBusy(true);

    const form = new FormData();
    form.append("epub_file", selectedFile);
    // chuyển playlistChoice thành playlist_mode/playlist_id cho backend
    let mode: string = "auto";
    let pid = "";
    if (playlistChoice === "__auto__") {
      mode = "auto";
    } else if (playlistChoice === "__new__") {
      mode = "new";
    } else {
      mode = "existing";
      pid = playlistChoice;
    }
    form.append("playlist_mode", mode);
    form.append("playlist_id", pid);
    form.append("playlist_country", playlistCountry || "VN");
    form.append("playlist_title", customPlaylistTitle || parsedTitle);
    form.append("playlist_description", customPlaylistDesc || parsedDescription);
    try {
      const response = await fetch("/books/upload", {
        method: "POST",
        body: form,
      });

      if (!response.ok) {
        let msg = `Tải tệp thất bại (Mã lỗi ${response.status})`;
        try {
          const j = await response.json();
          if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
        } catch {}
        throw new Error(msg);
      }

      const match = response.url.match(/books\/(\d+)/);
      const id = match?.[1];
      navigate(id ? `/books/${id}` : "/books");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="max-w-3xl space-y-6">
      <Header
        title="Đưa một cuốn sách vào xưởng"
        subtitle="Chọn tệp EPUB từ máy tính. Dây chuyền tự động sẽ giải mã cấu trúc, trích xuất mục lục và phân đoạn văn bản."
      />

      <Card className="border-border bg-card">
        <CardContent className="p-6 sm:p-10">
          <form onSubmit={submit} className="space-y-6">
            <div className="border-2 border-dashed border-border hover:border-primary/60 transition-colors rounded-lg p-8 sm:p-12 text-center bg-background/50 flex flex-col items-center justify-center space-y-4">
              <div className="h-16 w-16 rounded-full bg-primary/10 flex items-center justify-center text-primary">
                <UploadCloud className="h-8 w-8" />
              </div>

              <div className="space-y-1 max-w-md">
                <h3 className="text-base font-bold text-foreground">Kéo thả tệp EPUB vào đây</h3>
                <p className="text-xs text-muted-foreground">
                  Hỗ trợ định dạng .epub tiêu chuẩn. Dung lượng tối đa phụ thuộc cấu hình hệ thống.
                </p>
              </div>

              <div className="pt-2">
                <label className="inline-flex items-center gap-2 px-4 py-2 rounded-md bg-secondary hover:bg-secondary/80 text-secondary-foreground text-xs font-semibold cursor-pointer border border-border transition-colors">
                  <FileText className="h-4 w-4 text-primary" />
                  <span>{selectedFile ? selectedFile.name : "Duyệt tìm tệp EPUB..."}</span>
                  <input
                    name="epub_file"
                    type="file"
                    accept=".epub"
                    required
                    className="hidden"
                    onChange={(e) => {
                      if (e.target.files && e.target.files[0]) {
                        setSelectedFile(e.target.files[0]);
                      }
                    }}
                  />
                </label>
              </div>

              {selectedFile && (
                <div className="flex items-center gap-2 text-xs font-mono text-emerald-600 bg-emerald-50 px-3 py-1.5 rounded border border-emerald-200">
                  <CheckCircle2 className="h-3.5 w-3.5" />
                  <span>Đã chọn: {selectedFile.name} ({(selectedFile.size / 1024 / 1024).toFixed(2)} MB)</span>
                </div>
              )}
            </div>

            {/* Playlist combobox — hiển thị sau khi chọn file */}
            {selectedFile && (
              <Card className="border-border bg-muted/20">
                <CardContent className="p-4 space-y-4">
                  <div className="flex items-center gap-2 text-sm font-semibold">
                    <ListMusic className="h-4 w-4 text-primary" />
                    Cấu hình Playlist YouTube
                    {parsing && <span className="text-xs font-normal text-muted-foreground">(đang phân tích EPUB...)</span>}
                    {playlistsLoading && <span className="text-xs font-normal text-muted-foreground">(đang tải playlists...)</span>}
                  </div>

                  {!playlistConnected && (
                    <div className="rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-xs text-amber-800 flex items-center gap-2">
                      <Tv className="h-3.5 w-3.5" />
                      Chưa kết nối YouTube — sẽ tạo playlist sau khi kết nối. Vẫn có thể upload sách.
                    </div>
                  )}

                  {parsedTitle && (
                    <div className="text-xs">
                      <span className="text-muted-foreground">Tên sách phát hiện:</span>{" "}
                      <span className="font-semibold">{parsedTitle}</span>
                      {parsedDescription && (
                        <span className="text-muted-foreground"> · Chương 1: {parsedDescription.length} ký tự</span>
                      )}
                    </div>
                  )}

                  <div className="grid gap-4 sm:grid-cols-2">
                    <label className="block text-xs font-medium">
                      <span className="flex items-center gap-1.5 mb-1.5">
                        <Tv className="h-3.5 w-3.5 text-muted-foreground" /> Playlist
                      </span>
                      <select
                        className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={playlistChoice}
                        onChange={(e) => setPlaylistChoice(e.target.value)}
                        disabled={playlistsLoading}
                      >
                        <option value="__auto__">
                          Tự động — detect theo tên sách {parsedTitle ? `“${parsedTitle}”` : ""} hoặc tạo mới
                        </option>
                        <option value="__new__">Tạo mới playlist: {parsedTitle || "theo tên sách"}</option>
                        {playlists.map((pl) => (
                          <option key={pl.id} value={pl.id}>
                            Có sẵn: {pl.title}
                          </option>
                        ))}
                      </select>
                      <span className="mt-1 block text-[11px] text-muted-foreground">
                        Tự động sẽ tìm playlist trùng tên sách (không phân biệt hoa/thường); nếu không có sẽ tạo mới lấy tên book_title.
                      </span>
                    </label>

                    <label className="block text-xs font-medium">
                      <span className="flex items-center gap-1.5 mb-1.5">
                        <Globe className="h-3.5 w-3.5 text-muted-foreground" /> Quốc gia
                      </span>
                      <select
                        className="flex h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
                        value={playlistCountry}
                        onChange={(e) => setPlaylistCountry(e.target.value)}
                      >
                        <option value="VN">Việt Nam (VN) — mặc định</option>
                        <option value="US">Hoa Kỳ (US)</option>
                        <option value="JP">Nhật Bản (JP)</option>
                        <option value="KR">Hàn Quốc (KR)</option>
                        <option value="GB">Anh (GB)</option>
                        <option value="FR">Pháp (FR)</option>
                      </select>
                      <span className="mt-1 block text-[11px] text-muted-foreground">
                        Dùng để đặt defaultLanguage khi tạo playlist (VN → vi).
                      </span>
                    </label>
                  </div>

                  {(playlistChoice === "__auto__" || playlistChoice === "__new__") && (
                    <div className="space-y-3 rounded-md border border-border bg-background p-3">
                      <div>
                        <label className="block text-xs font-medium mb-1.5">Tên playlist sẽ tạo</label>
                        <Input
                          value={customPlaylistTitle}
                          onChange={(e) => setCustomPlaylistTitle(e.target.value)}
                          placeholder={parsedTitle || "Tên playlist"}
                          className="h-9 text-sm"
                        />
                      </div>
                      <div>
                        <label className="block text-xs font-medium mb-1.5">
                          Mô tả playlist — 1200 ký tự đầu Chương 1
                        </label>
                        <Textarea
                          value={customPlaylistDesc}
                          onChange={(e) => setCustomPlaylistDesc(e.target.value)}
                          placeholder="Nội dung 1200 ký tự đầu của Chương 1 sẽ tự điền..."
                          className="min-h-24 text-xs"
                          maxLength={5000}
                        />
                        <div className="mt-1 text-[11px] text-muted-foreground text-right">
                          {customPlaylistDesc.length} / 5000 ký tự · mặc định {parsedDescription.length} ký tự từ Chương 1
                        </div>
                      </div>
                    </div>
                  )}

                  {playlistChoice !== "__auto__" && playlistChoice !== "__new__" && (
                    <div className="rounded-md bg-emerald-50 border border-emerald-200 px-3 py-2 text-xs text-emerald-800">
                      Sẽ dùng playlist có sẵn: <span className="font-semibold">{playlists.find((p) => p.id === playlistChoice)?.title || playlistChoice}</span>
                    </div>
                  )}
                </CardContent>
              </Card>
            )}

            {error && (
              <div className="p-3 rounded-md bg-red-50 border border-red-200 text-red-700 text-xs flex items-center gap-2 font-mono">
                <AlertCircle className="h-4 w-4 shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <div className="flex items-center justify-end gap-3 pt-2">
              <Button type="button" variant="outline" onClick={() => navigate("/books")} disabled={busy}>
                Hủy bỏ
              </Button>
              <Button type="submit" variant="accent" disabled={busy || !selectedFile} className="min-w-[140px]">
                {busy ? (
                  <span className="flex items-center gap-2">
                    <span className="h-3.5 w-3.5 border-2 border-foreground border-t-transparent rounded-full animate-spin" />
                    Đang phân tích...
                  </span>
                ) : (
                  "Bắt đầu phân tích"
                )}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
