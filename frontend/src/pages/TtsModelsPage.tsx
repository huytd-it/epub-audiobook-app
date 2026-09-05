import React, { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Cloud, Cpu, Download, ExternalLink, Gauge, Loader2, Play, RefreshCw, Settings2, TriangleAlert, Volume2 } from "lucide-react";
import { api, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, Header, LoadingState } from "@/components/common/Header";
import { ProductionSettings, TtsModel } from "@/pages/book-detail/types";

type Job = { state: "running" | "done" | "failed"; action: string; log: string; returncode: number | null };
type PlaygroundResult = {
  audio_base64: string; mime_type: string; sample_rate: number;
  latency_seconds: number; duration_seconds: number;
  realtime_factor: number | null; characters_per_second: number | null;
};
type ManagedModel = TtsModel & {
  install: { managed: boolean; ready: boolean | null; path: string; size_bytes: number; detail: string };
  job: Job | null;
};

const SAMPLE_TEXT = "Xin chào, đây là bản nghe thử để kiểm tra chất giọng, độ rõ và tốc độ tổng hợp tiếng Việt.";
const isApi = (model: ManagedModel) => model.capabilities.kind === "api" || model.capabilities.runtime === "api";
const size = (bytes: number) => bytes ? `${(bytes / 1024 / 1024).toFixed(bytes > 1024 ** 3 ? 0 : 1)} ${bytes > 1024 ** 3 ? "GB" : "MB"}` : "—";

export function TtsModelsPage() {
  const [models, setModels] = useState<ManagedModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [defaultModelId, setDefaultModelId] = useState("");
  const [settingDefault, setSettingDefault] = useState("");
  const [runtime, setRuntime] = useState<"local" | "api">("local");
  const [playModelId, setPlayModelId] = useState("");
  const [playVoice, setPlayVoice] = useState("");
  const [sampleText, setSampleText] = useState(SAMPLE_TEXT);
  const [playing, setPlaying] = useState(false);
  const [playError, setPlayError] = useState("");
  const [playResult, setPlayResult] = useState<PlaygroundResult>();
  const load = useCallback(() => api<{ models: ManagedModel[] }>("/tts-models").then((value) => setModels(value.models || [])).catch((error) => setMessage(error instanceof Error ? error.message : "Không thể tải catalog TTS.")).finally(() => setLoading(false)), []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    api<ProductionSettings>("/production-settings")
      .then((value) => setDefaultModelId(value.defaults.audio.model_id))
      .catch((error) => setMessage(error instanceof Error ? error.message : "Không thể tải model mặc định."));
  }, []);
  const active = models.some((model) => model.job?.state === "running");
  const filteredModels = models.filter((model) => runtime === "api" ? isApi(model) : !isApi(model));
  const playableModels = models.filter((model) => !model.supports_reference);
  const playModel = playableModels.find((model) => model.id === playModelId);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(load, 1500);
    return () => window.clearInterval(timer);
  }, [active, load]);

  useEffect(() => {
    if (playModelId || !playableModels.length) return;
    const model = playableModels.find((item) => item.id === defaultModelId) || playableModels[0];
    setPlayModelId(model.id);
    setPlayVoice(model.default_voice || model.voices?.[0]?.id || "");
  }, [defaultModelId, playModelId, playableModels]);

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

  const choosePlayModel = (modelId: string) => {
    const model = playableModels.find((item) => item.id === modelId);
    setPlayModelId(modelId);
    setPlayVoice(model?.default_voice || model?.voices?.[0]?.id || "");
    setPlayResult(undefined);
    setPlayError("");
  };

  const runPlayground = async () => {
    setPlaying(true); setPlayError(""); setPlayResult(undefined);
    try {
      setPlayResult(await postJson<PlaygroundResult>("/tts-models/playground", {
        model_id: playModelId, voice: playVoice || null, text: sampleText,
      }));
    } catch (error) {
      setPlayError(error instanceof Error ? error.message : "Không thể tạo bản nghe thử.");
    } finally { setPlaying(false); }
  };

  return <div className="space-y-7">
    <Header title="Model & provider TTS" subtitle="Model local dùng tài nguyên trên máy; provider API chạy trong pool riêng nên không phải chờ GPU local. Nghe thử và đo tốc độ trước khi dùng cho production." />


    {message && <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{message}</div>}
    <section className="grid gap-5 rounded-xl border bg-card p-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(300px,.75fr)]">
      <div className="space-y-4">
        <div className="flex items-center gap-2"><Volume2 className="h-5 w-5 text-primary" /><h2 className="text-lg font-semibold">Playground giọng đọc</h2></div>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1.5 text-xs font-medium">Model / provider
            <select className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={playModelId} onChange={(event) => choosePlayModel(event.target.value)}>
              {playableModels.map((model) => <option key={model.id} value={model.id}>{isApi(model) ? "API · " : "Local · "}{model.name}</option>)}
            </select>
          </label>
          <label className="space-y-1.5 text-xs font-medium">Voice ID
            {playModel?.voices?.length ? <select className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={playVoice} onChange={(event) => setPlayVoice(event.target.value)}>{playModel.voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.label || voice.id}</option>)}</select>
              : <input className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={playVoice} onChange={(event) => setPlayVoice(event.target.value)} placeholder="Voice mặc định" />}
          </label>
        </div>
        <textarea aria-label="Nội dung nghe thử" className="min-h-28 w-full resize-y rounded-md border bg-background px-3 py-2 text-sm leading-relaxed outline-none focus:ring-2 focus:ring-primary/30" maxLength={1200} value={sampleText} onChange={(event) => setSampleText(event.target.value)} />
        <div className="flex items-center justify-between gap-3"><span className="text-xs tabular-nums text-muted-foreground">{sampleText.length}/1200 ký tự</span><Button onClick={runPlayground} disabled={playing || !playModelId || !sampleText.trim()}>{playing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}{playing ? "Đang tổng hợp..." : "Tạo & nghe thử"}</Button></div>
        {!loading && !playableModels.length && <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">Chưa có model phù hợp để nghe thử. Model dùng giọng tham chiếu hiện chưa được hỗ trợ trong playground.</p>}
        {playError && <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{playError}</p>}
      </div>
      <div className="flex min-h-52 flex-col justify-center rounded-lg bg-muted/50 p-4">
        {playResult ? <div className="space-y-4"><audio className="w-full" controls autoPlay src={`data:${playResult.mime_type};base64,${playResult.audio_base64}`} /><div className="grid grid-cols-2 gap-3 text-xs tabular-nums"><Metric label="Độ trễ" value={`${playResult.latency_seconds.toFixed(2)} s`} /><Metric label="Audio" value={`${playResult.duration_seconds.toFixed(2)} s`} /><Metric label="Real-time factor" value={playResult.realtime_factor == null ? "—" : `${playResult.realtime_factor.toFixed(2)}×`} /><Metric label="Tốc độ" value={playResult.characters_per_second == null ? "—" : `${playResult.characters_per_second.toFixed(1)} ký tự/s`} /><Metric label="Sample rate" value={`${playResult.sample_rate.toLocaleString()} Hz`} /></div><p className="text-xs text-muted-foreground">{playResult.realtime_factor == null ? "Không đủ dữ liệu để so tốc độ thời gian thực." : playResult.realtime_factor < 1 ? `Nhanh hơn thời lượng audio khoảng ${(1 / playResult.realtime_factor).toFixed(1)} lần.` : "Chậm hơn thời lượng audio; phù hợp để kiểm tra chất lượng hơn là xử lý thời gian thực."}</p></div>
          : <div className="text-center text-muted-foreground"><Gauge className="mx-auto mb-3 h-8 w-8" /><p className="text-sm font-medium text-foreground">Chưa có phép đo</p><p className="mt-1 text-xs">RTF dưới 1× là nhanh hơn thời lượng audio.</p></div>}
      </div>
    </section>
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-lg font-semibold">Catalog TTS</h2><p className="text-xs text-muted-foreground">Hai runtime độc lập, cùng dùng một pipeline audiobook.</p></div><div className="inline-flex rounded-lg border bg-muted/40 p-1"><Button size="sm" variant={runtime === "local" ? "secondary" : "ghost"} aria-pressed={runtime === "local"} onClick={() => setRuntime("local")}><Cpu className="h-3.5 w-3.5" /> Local ({models.filter((model) => !isApi(model)).length})</Button><Button size="sm" variant={runtime === "api" ? "secondary" : "ghost"} aria-pressed={runtime === "api"} onClick={() => setRuntime("api")}><Cloud className="h-3.5 w-3.5" /> API ({models.filter(isApi).length})</Button></div></div>
    {loading && <LoadingState text="Đang tải catalog TTS..." />}
    {!loading && filteredModels.length === 0 && <EmptyState text={runtime === "api" ? "Chưa có provider API nào được cấu hình." : "Chưa có model TTS local nào."} />}
    <div className="grid gap-4 lg:grid-cols-2">
      {filteredModels.map((model) => {
        const { install, job } = model;
        const running = job?.state === "running";
        return <Card key={model.id}>
          <CardHeader className="space-y-1 pb-3">
            <div className="flex items-start justify-between gap-3"><div className="flex items-center gap-2">{isApi(model) ? <Cloud className="h-4 w-4 text-sky-600" /> : <Cpu className="h-4 w-4 text-violet-600" />}<CardTitle className="text-base">{model.name}</CardTitle></div>
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

function Metric({ label, value }: { label: string; value: string }) {
  return <div><p className="text-muted-foreground">{label}</p><p className="mt-0.5 font-mono font-semibold text-foreground">{value}</p></div>;
}