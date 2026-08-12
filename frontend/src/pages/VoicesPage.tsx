import React, { useEffect, useMemo, useState } from "react";
import { AudioWaveform, Edit2, Mic, Pause, Play, Plus, Save, Trash2, X } from "lucide-react";
import {
  api,
  postForm,
  postJson,
  VoiceItem,
  VoiceProcessResult,
  VoiceTag,
  VoiceTaxonomy,
} from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { AudioStudioDialog } from "./voices/AudioStudioDialog";

const selectClass =
  "h-9 rounded-md border border-input bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15";

/** Sentinel for the "chưa phân loại" filter option — the empty string is the
 *  stored value for an unclassified clip, so it can't double as "all". */
const UNTAGGED = "__untagged__";

export function VoicesPage() {
  const [items, setItems] = useState<VoiceItem[]>([]);
  const [taxonomy, setTaxonomy] = useState<VoiceTaxonomy>({ genders: [], genres: [] });
  const [loading, setLoading] = useState(true);
  const [playingName, setPlayingName] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [search, setSearch] = useState("");
  const [genderFilter, setGenderFilter] = useState("");
  const [genreFilter, setGenreFilter] = useState("");
  const [editingItem, setEditingItem] = useState<VoiceItem | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [editGender, setEditGender] = useState("");
  const [editGenres, setEditGenres] = useState<string[]>([]);
  const [isSaving, setIsSaving] = useState(false);
  const [studioItem, setStudioItem] = useState<VoiceItem | null>(null);

  const genderLabels = useMemo(
    () => Object.fromEntries(taxonomy.genders.map((tag) => [tag.value, tag.label])),
    [taxonomy.genders]
  );
  const genreLabels = useMemo(
    () => Object.fromEntries(taxonomy.genres.map((tag) => [tag.value, tag.label])),
    [taxonomy.genres]
  );

  const loadData = () => {
    setLoading(true);
    api<{ voices: VoiceItem[] }>("/api/ui/media")
      .then((res) => setItems(res.voices || []))
      .catch((err) => console.error("Lỗi tải danh sách voice:", err))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadData();
    // The vocabulary is served by the backend so validation and the pickers
    // can never disagree about which slugs exist.
    api<VoiceTaxonomy>("/voices/taxonomy")
      .then(setTaxonomy)
      .catch((err) => console.error("Lỗi tải danh mục phân loại giọng:", err));
  }, []);

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!e.target.files || e.target.files.length === 0) return;
    setIsUploading(true);
    const formData = new FormData();
    Array.from(e.target.files).forEach((file) => formData.append("files", file));
    try {
      await postForm("/voices/upload", formData);
      loadData();
    } catch (err: any) {
      alert(`Tải file giọng mẫu thất bại: ${err.message}`);
    } finally {
      setIsUploading(false);
      e.target.value = "";
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`Bạn có chắc muốn xóa giọng mẫu "${name}"?`)) return;
    const form = new FormData();
    form.append("name", name);
    try {
      await postForm("/voices/delete", form);
      if (playingName === name) setPlayingName(null);
      loadData();
    } catch (err: any) {
      alert(`Xóa giọng mẫu thất bại: ${err.message}`);
    }
  };

  const openEdit = (item: VoiceItem) => {
    setEditingItem(item);
    setEditName(item.name);
    setEditDesc(item.description || "");
    setEditGender(item.gender || "");
    setEditGenres(item.genre || []);
  };

  const handleSaveEdit = async () => {
    if (!editingItem) return;
    setIsSaving(true);
    try {
      // Save the tags against the current name *first*, then rename: the rename
      // endpoint carries the metadata row over to the new filename, so we never
      // have to predict how the server will sanitize the name the user typed.
      await postJson(`/voices/${encodeURIComponent(editingItem.name)}/meta`, {
        description: editDesc.trim(),
        gender: editGender,
        genre: editGenres,
      });
      if (editName.trim() && editName.trim() !== editingItem.name) {
        const renameForm = new FormData();
        renameForm.append("old_name", editingItem.name);
        renameForm.append("new_name", editName.trim());
        await postForm("/voices/rename", renameForm);
      }

      setEditingItem(null);
      loadData();
    } catch (err: any) {
      alert(`Lưu thông tin giọng mẫu thất bại: ${err.message}`);
    } finally {
      setIsSaving(false);
    }
  };

  const handleProcessed = (result: VoiceProcessResult) => {
    // Reload rather than patch state locally: a "save as copy" adds a row and an
    // overwrite changes the file size, both of which come from the media list.
    loadData();
    if (playingName === result.name) setPlayingName(null);
  };

  const toggleEditGenre = (value: string) => {
    setEditGenres((current) =>
      current.includes(value) ? current.filter((item) => item !== value) : [...current, value]
    );
  };

  const filteredItems = items.filter((item) => {
    const term = search.trim().toLowerCase();
    if (term) {
      const haystack = [
        item.name,
        item.description || "",
        genderLabels[item.gender || ""] || "",
        ...(item.genre || []).map((slug) => genreLabels[slug] || slug),
      ]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(term)) return false;
    }
    if (genderFilter === UNTAGGED ? !!item.gender : genderFilter && item.gender !== genderFilter) {
      return false;
    }
    const genres = item.genre || [];
    if (genreFilter === UNTAGGED ? genres.length > 0 : genreFilter && !genres.includes(genreFilter)) {
      return false;
    }
    return true;
  });

  const hasFilter = !!(search || genderFilter || genreFilter);

  return (
    <div className="space-y-6">
      <Header
        title="Thư viện giọng mẫu Voice Studio"
        subtitle="Quản lý các đoạn audio voice clip làm mẫu sinh giọng đọc AI (Cloning / TTS): phân loại theo giới tính, thể loại truyện và xử lý làm sạch âm thanh."
        action={
          <label className="cursor-pointer">
            <input
              type="file"
              multiple
              accept="audio/wav,audio/mp3,audio/m4a,audio/ogg"
              className="hidden"
              onChange={handleUpload}
              disabled={isUploading}
            />
            <Button variant="default" size="sm" asChild disabled={isUploading}>
              <span>
                <Plus className="h-4 w-4" />
                {isUploading ? "Đang tải lên..." : "Tải voice mẫu lên"}
              </span>
            </Button>
          </label>
        }
      />

      <div className="flex flex-wrap items-center gap-3">
        <Input
          placeholder="Tìm theo tên, mô tả, giới tính hoặc thể loại..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-xs"
        />
        <select
          className={selectClass}
          value={genderFilter}
          onChange={(e) => setGenderFilter(e.target.value)}
          aria-label="Lọc theo giới tính giọng"
        >
          <option value="">Mọi giới tính</option>
          {taxonomy.genders.map((tag) => (
            <option key={tag.value} value={tag.value}>
              {tag.label}
            </option>
          ))}
          <option value={UNTAGGED}>Chưa phân loại giới tính</option>
        </select>
        <select
          className={selectClass}
          value={genreFilter}
          onChange={(e) => setGenreFilter(e.target.value)}
          aria-label="Lọc theo thể loại truyện"
        >
          <option value="">Mọi thể loại truyện</option>
          {taxonomy.genres.map((tag) => (
            <option key={tag.value} value={tag.value}>
              {tag.label}
            </option>
          ))}
          <option value={UNTAGGED}>Chưa gắn thể loại</option>
        </select>
        {hasFilter && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setSearch("");
              setGenderFilter("");
              setGenreFilter("");
            }}
          >
            <X className="h-3.5 w-3.5" />
            Bỏ lọc
          </Button>
        )}
        <span className="ml-auto text-xs font-mono text-muted-foreground">
          {hasFilter ? `${filteredItems.length}/${items.length}` : `Tổng số: ${items.length}`} giọng
        </span>
      </div>

      {loading ? (
        <LoadingState text="Đang tải thư viện giọng mẫu..." />
      ) : filteredItems.length === 0 ? (
        <EmptyState
          text={
            items.length === 0
              ? "Chưa có voice clip mẫu nào trong thư viện."
              : "Không có giọng nào khớp bộ lọc hiện tại."
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredItems.map((item) => {
            const isPlaying = playingName === item.name;
            const genres = item.genre || [];
            return (
              <Card key={item.name} className="border-border flex flex-col justify-between hover:border-primary/40 transition-colors">
                <CardHeader className="p-4 pb-2">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <div className="h-9 w-9 rounded-md bg-lime/10 text-lime flex items-center justify-center shrink-0">
                        <Mic className="h-4 w-4" />
                      </div>
                      <div className="min-w-0">
                        <CardTitle className="text-sm font-bold text-foreground truncate" title={item.name}>
                          {item.name}
                        </CardTitle>
                        <span className="text-[11px] font-mono text-muted-foreground">
                          {(item.size / 1024 / 1024).toFixed(2)} MB
                        </span>
                      </div>
                    </div>

                    <Button
                      variant={isPlaying ? "destructive" : "outline"}
                      size="icon"
                      className="h-8 w-8 shrink-0"
                      onClick={() => setPlayingName(isPlaying ? null : item.name)}
                    >
                      {isPlaying ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4 ml-0.5" />}
                    </Button>
                  </div>
                </CardHeader>

                <CardContent className="p-4 pt-2 space-y-3 flex-1 flex flex-col justify-between">
                  <div className="space-y-2">
                    <div className="flex flex-wrap gap-1">
                      {item.gender ? (
                        <Badge variant="lime" className="normal-case tracking-normal">
                          {genderLabels[item.gender] || item.gender}
                        </Badge>
                      ) : (
                        <Badge variant="outline" className="normal-case tracking-normal text-muted-foreground">
                          Chưa rõ giới tính
                        </Badge>
                      )}
                      {genres.map((slug) => (
                        <Badge key={slug} variant="secondary" className="normal-case tracking-normal">
                          {genreLabels[slug] || slug}
                        </Badge>
                      ))}
                    </div>
                    {item.description ? (
                      <p className="text-xs text-muted-foreground line-clamp-2">{item.description}</p>
                    ) : (
                      <span className="text-[11px] font-mono text-muted-foreground italic">Chưa có mô tả giọng</span>
                    )}
                  </div>

                  {isPlaying && (
                    <div className="bg-muted/40 p-2 rounded border border-border">
                      <audio
                        src={`/voices/file/${encodeURIComponent(item.name)}`}
                        controls
                        autoPlay
                        className="w-full h-8"
                        onEnded={() => setPlayingName(null)}
                      />
                    </div>
                  )}

                  <div className="flex items-center justify-end gap-1 pt-2 border-t border-border/50">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => setStudioItem(item)}
                      title="Cắt sửa và làm sạch âm thanh"
                    >
                      <AudioWaveform className="h-3.5 w-3.5" />
                      Xử lý
                    </Button>
                    <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => openEdit(item)}>
                      <Edit2 className="h-3.5 w-3.5" />
                      Sửa
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs text-destructive hover:text-destructive"
                      onClick={() => handleDelete(item.name)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      Xóa
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {editingItem && (
        <Dialog open={!!editingItem} onOpenChange={(open) => !open && setEditingItem(null)}>
          <DialogContent className="max-w-lg max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Chỉnh sửa giọng mẫu</DialogTitle>
            </DialogHeader>
            <div className="space-y-4 py-2">
              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Tên file giọng mẫu</label>
                <Input value={editName} onChange={(e) => setEditName(e.target.value)} />
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground mb-1.5 block">Giới tính giọng</label>
                <div className="flex flex-wrap gap-1.5">
                  <TagChip
                    label="Chưa phân loại"
                    active={!editGender}
                    onClick={() => setEditGender("")}
                  />
                  {taxonomy.genders.map((tag) => (
                    <TagChip
                      key={tag.value}
                      label={tag.label}
                      active={editGender === tag.value}
                      onClick={() => setEditGender(tag.value)}
                    />
                  ))}
                </div>
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground mb-1.5 block">
                  Thể loại truyện phù hợp
                  <span className="ml-1 font-normal text-muted-foreground">(chọn nhiều)</span>
                </label>
                <div className="flex flex-wrap gap-1.5">
                  {taxonomy.genres.map((tag) => (
                    <TagChip
                      key={tag.value}
                      label={tag.label}
                      active={editGenres.includes(tag.value)}
                      onClick={() => toggleEditGenre(tag.value)}
                    />
                  ))}
                </div>
                {editGenres.length > 0 && (
                  <button
                    type="button"
                    className="mt-2 text-[11px] font-mono text-muted-foreground underline hover:text-foreground"
                    onClick={() => setEditGenres([])}
                  >
                    Bỏ chọn tất cả thể loại
                  </button>
                )}
              </div>

              <div>
                <label className="text-xs font-semibold text-foreground mb-1 block">Mô tả đặc tính giọng đọc</label>
                <Textarea
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  placeholder="VD: Giọng nam Miền Nam ấm, truyền cảm, tốc độ vừa phải..."
                />
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => setEditingItem(null)} disabled={isSaving}>
                Hủy
              </Button>
              <Button variant="default" onClick={handleSaveEdit} disabled={isSaving}>
                <Save className="h-4 w-4" />
                {isSaving ? "Đang lưu..." : "Lưu thay đổi"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}

      {studioItem && (
        <AudioStudioDialog
          voice={studioItem}
          onClose={() => setStudioItem(null)}
          onSaved={handleProcessed}
        />
      )}
    </div>
  );
}

/** Selectable pill used for both the single-choice gender and multi-choice genres. */
function TagChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded-md border px-2 py-1 text-xs font-medium transition-colors",
        active
          ? "border-primary bg-primary text-primary-foreground"
          : "border-border bg-background text-muted-foreground hover:border-primary/50 hover:text-foreground"
      )}
    >
      {label}
    </button>
  );
}
