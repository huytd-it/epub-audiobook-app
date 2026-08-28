import React, { useCallback, useEffect, useState } from "react";
import { Box, CheckCircle2, Download, ExternalLink, Loader2, RefreshCw, Settings2, TriangleAlert } from "lucide-react";
import { api, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ProductionSettings, TtsModel } from "@/pages/book-detail/types";

type Job = { state: "running" | "done" | "failed"; action: string; log: string; returncode: number | null };
type ManagedModel = TtsModel & {
  install: { managed: boolean; ready: boolean | null; path: string; size_bytes: number; detail: string };
  job: Job | null;
};

const size = (bytes: number) => bytes ? `${(bytes / 1024 / 1024).toFixed(bytes > 1024 ** 3 ? 0 : 1)} ${bytes > 1024 ** 3 ? "GB" : "MB"}` : "—";

export function TtsModelsPage() {
  const [models, setModels] = useState<ManagedModel[]>([]);
  const [message, setMessage] = useState("");
  const [defaultModelId, setDefaultModelId] = useState("");
  const [settingDefault, setSettingDefault] = useState("");
  const load = useCallback(() => api<{ models: ManagedModel[] }>("/tts-models").then((value) => setModels(value.models || [])).catch((error) => setMessage(error.message)), []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    api<ProductionSettings>("/production-settings")
      .then((value) => setDefaultModelId(value.defaults.audio.model_id))
      .catch((error) => setMessage(error instanceof Error ? error.message : "Không thể tải model mặc định."));
  }, []);
  const active = models.some((model) => model.job?.state === "running");
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(load, 1500);
    return () => window.clearInterval(timer);
  }, [active, load]);

  const start = async (model: ManagedModel, update = false) => {
    setMessage("");
    try {
      await postJson(`/tts-models/${model.id}/download?update=${update}`, {});
      await load();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Không thể bắt đầu tải model."); }
  };

  const setDefault = async (model: ManagedModel) => {
    setMessage("");
    setSettingDefault(model.id);
    try {
      const settings = await api<ProductionSettings>("/production-settings");
      await postJson("/production-settings", {
        audio: { ...settings.defaults.audio, model_id: model.id },
      });
      setDefaultModelId(model.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không thể đặt model mặc định.");
    } finally {
      setSettingDefault("");
    }
  };

  return <div className="space-y-5">
    <header className="space-y-2">
      <div className="flex items-center gap-2 text-primary"><Box className="h-5 w-5" /><span className="font-mono text-xs uppercase tracking-wider">TTS models</span></div>
      <h1 className="text-2xl font-bold tracking-tight">Quản lý model TTS</h1>
      <p className="max-w-3xl text-sm text-muted-foreground">Kiểm tra weights cục bộ, tải hoặc làm mới các model được app quản lý. Đặt model mặc định để áp dụng cho các lượt tạo audio mới. Nên dừng các job đang dùng model trước khi cập nhật.</p>
    </header>
    {message && <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{message}</div>}
    <div className="grid gap-4 lg:grid-cols-2">
      {models.map((model) => {
        const { install, job } = model;
        const running = job?.state === "running";
        return <Card key={model.id}>
          <CardHeader className="space-y-1 pb-3">
            <div className="flex items-start justify-between gap-3"><CardTitle className="text-base">{model.name}</CardTitle>
              {install.ready === true ? <span className="inline-flex items-center gap-1 text-xs text-emerald-600"><CheckCircle2 className="h-3.5 w-3.5" /> Sẵn sàng</span>
                : install.ready === false ? <span className="inline-flex items-center gap-1 text-xs text-amber-600"><TriangleAlert className="h-3.5 w-3.5" /> Chưa sẵn sàng</span>
                : <span className="text-xs text-muted-foreground">Theo package</span>}</div>
            <p className="font-mono text-[11px] text-muted-foreground">{model.model_id}</p>
          </CardHeader>
          <CardContent className="space-y-3 text-xs">
            <p className="text-muted-foreground">{install.detail}</p>
            {install.path && <p className="break-all rounded bg-muted px-2 py-1.5 font-mono text-[10px]">{install.path}</p>}
            {install.managed ? <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={() => start(model)} disabled={running}>{running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}{install.ready ? "Kiểm tra / sửa" : "Tải model"}</Button>
              <Button size="sm" variant="outline" onClick={() => start(model, true)} disabled={running}><RefreshCw className="h-3.5 w-3.5" /> Cập nhật</Button>
              <span className="text-muted-foreground">{size(install.size_bytes)}</span>
            </div> : <a className="inline-flex items-center gap-1 text-primary underline" href={`https://huggingface.co/${model.model_id}`} target="_blank" rel="noreferrer">Xem nguồn model <ExternalLink className="h-3 w-3" /></a>}
            <Button
              size="sm"
              variant={defaultModelId === model.id ? "secondary" : "outline"}
              onClick={() => setDefault(model)}
              disabled={Boolean(settingDefault) || defaultModelId === model.id}
            >
              {settingDefault === model.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : defaultModelId === model.id ? <CheckCircle2 className="h-3.5 w-3.5" /> : <Settings2 className="h-3.5 w-3.5" />}
              {defaultModelId === model.id ? "Model mặc định" : "Đặt làm mặc định"}
            </Button>
            {job && <pre className={`max-h-28 overflow-auto whitespace-pre-wrap rounded p-2 text-[10px] ${job.state === "failed" ? "bg-destructive/10 text-destructive" : "bg-muted"}`}>{job.state === "running" ? "Đang tải…\n" : ""}{job.log || "Đang chờ dữ liệu..."}</pre>}
          </CardContent>
        </Card>;
      })}
    </div>
  </div>;
}
