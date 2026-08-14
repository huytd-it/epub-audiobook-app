import React, { useEffect, useState } from "react";
import { Save, Settings2 } from "lucide-react";
import { api, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ProductionGroup, ProductionSettings, errorText } from "@/pages/book-detail/types";

const groups: { key: ProductionGroup; label: string; hint: string }[] = [
  { key: "audio", label: "Âm thanh", hint: "TTS model, voice, độ dài chunk và hiệu ứng." },
  { key: "normalization", label: "Chuẩn hóa TTS", hint: "Quy tắc làm sạch văn bản trước khi đọc." },
  { key: "video", label: "Video", hint: "Khung hình, codec, background, waveform và phụ đề." },
  { key: "youtube", label: "YouTube", hint: "Mẫu tiêu đề, description, tags, playlist và quyền riêng tư." },
];

export function ProductionSettingsPage() {
  const [settings, setSettings] = useState<ProductionSettings>();
  const [drafts, setDrafts] = useState<Partial<Record<ProductionGroup, string>>>({});
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState<ProductionGroup>();

  useEffect(() => {
    api<ProductionSettings>("/production-settings")
      .then((value) => {
        setSettings(value);
        setDrafts(Object.fromEntries(groups.map(({ key }) => [key, JSON.stringify(value.defaults[key], null, 2)])));
      })
      .catch((error) => setMessage(errorText(error)));
  }, []);

  const save = async (group: ProductionGroup) => {
    setSaving(group);
    setMessage("");
    try {
      const value = JSON.parse(drafts[group] || "{}") as object;
      const result = await postJson<ProductionSettings>("/production-settings", { groups: { [group]: value } });
      setSettings(result);
      setDrafts((current) => ({ ...current, [group]: JSON.stringify(result.defaults[group], null, 2) }));
      setMessage(`Đã lưu mặc định ${groups.find((item) => item.key === group)?.label.toLowerCase()}. Ebook đang dùng mặc định sẽ nhận cấu hình mới.`);
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setSaving(undefined);
    }
  };

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <div className="flex items-center gap-2 text-primary"><Settings2 className="h-5 w-5" /><span className="font-mono text-xs uppercase tracking-wider">Production defaults</span></div>
        <h1 className="text-2xl font-bold tracking-tight">Mặc định sản xuất</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">Thiết lập dùng chung cho toàn bộ ebook. Mỗi ebook có thể kế thừa hoặc chuyển sang tùy chỉnh riêng; metadata số tập và khoảng chương vẫn được tạo tự động.</p>
      </header>

      {message && <div className="rounded-md border border-border bg-muted/30 px-4 py-3 text-xs">{message}</div>}

      <div className="grid gap-4 lg:grid-cols-2">
        {groups.map(({ key, label, hint }) => (
          <section key={key} className="space-y-3 rounded-lg border border-border bg-card p-4">
            <div><h2 className="font-semibold">{label}</h2><p className="text-xs text-muted-foreground">{hint}</p></div>
            <Textarea
              aria-label={`Cấu hình mặc định ${label}`}
              className="min-h-72 font-mono text-[11px] leading-5"
              value={drafts[key] || ""}
              onChange={(event) => setDrafts((current) => ({ ...current, [key]: event.target.value }))}
              disabled={!settings}
            />
            <div className="flex justify-end"><Button onClick={() => save(key)} disabled={!settings || saving === key}><Save className="h-4 w-4" />{saving === key ? "Đang lưu..." : `Lưu ${label}`}</Button></div>
          </section>
        ))}
      </div>
    </div>
  );
}
