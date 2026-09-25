import React, { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, Cloud, Cpu, Download, ExternalLink, FlaskConical, FolderDown, Gauge, Loader2, Pencil, Play, Plus, RefreshCw, Save, SlidersHorizontal, Trash2, TriangleAlert, Volume2 } from "lucide-react";
import { api, del, postJson, put } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Combobox } from "@/components/ui/combobox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState, Header, LoadingState } from "@/components/common/Header";
import { ProductionSettings, TtsModel } from "@/pages/book-detail/types";

type Job = { state: "running" | "done" | "failed"; action: string; log: string; returncode: number | null };
type PlaygroundResult = {
  audio_base64: string; mime_type: string; sample_rate: number;
  latency_seconds: number; duration_seconds: number;
  realtime_factor: number | null; characters_per_second: number | null;
};
type ManagedModel = TtsModel & {
  install: { managed: boolean; ready: boolean | null; path: string; size_bytes: number; detail: string; package?: string | null; package_version?: string | null };
  job: Job | null;
  custom?: boolean;
  has_api_key?: boolean;
};

type OfflineJob = { state: "running" | "done" | "failed"; done: number; total: number; moved?: number; log: string; [key: string]: unknown };

type SampleVoice = { id: string; label: string; language: string; cached: boolean; audio_url: string | null };
type SampleGroup = { model_id: string; name: string; count: number; cached: number; voices: SampleVoice[] };
type SampleCatalog = { models: Record<string, SampleGroup>; total: number; cached: number };

type CustomProvider = {
  id: string; name?: string; adapter: string; base_url?: string; model?: string;
  voice?: string; voices?: { id: string; label?: string; language?: string }[];
  models?: { id: string; label?: string }[];
  api_key_env?: string; sample_rate?: number; timeout_seconds?: number;
  instructions?: string; app_id?: string; callback_url?: string; speed_rate?: number;
  language_code?: string; speaking_rate?: number; has_api_key?: boolean; custom?: boolean;
};

const ADAPTERS = [
  { value: "custom", label: "Custom (OpenAI-compatible)", hint: "Mọi endpoint /audio/speech kiểu OpenAI: base_url + model + voice." },
  { value: "openai", label: "OpenAI", hint: "api.openai.com hoặc Azure OpenAI qua base_url." },
  { value: "google", label: "Google Cloud TTS", hint: "Cloud Text-to-Speech: voice dạng vi-VN-Standard-A, cần API key." },
  { value: "gemini", label: "Gemini TTS", hint: "Generative Language API, voice ví dụ Kore." },
  { value: "elevenlabs", label: "ElevenLabs", hint: "Voice ID của ElevenLabs." },
  { value: "vbee", label: "Vbee", hint: "Cần app_id + callback_url, voice_code tiếng Việt." },
];

const EMPTY_FORM: CustomProvider = {
  id: "", name: "", adapter: "custom", base_url: "", model: "", voice: "",
  api_key_env: "", sample_rate: 24000, timeout_seconds: 120,
};

const SAMPLE_TEXT = "Xin chào, đây là bản nghe thử để kiểm tra chất giọng, độ rõ và tốc độ tổng hợp tiếng Việt.";
const isApi = (model: ManagedModel) => model.capabilities.kind === "api" || model.capabilities.runtime === "api";
const providerIdOf = (catalogId: string) => catalogId.split(":")[0];
const size = (bytes: number) => bytes ? `${(bytes / 1024 / 1024).toFixed(bytes > 1024 ** 3 ? 0 : 1)} ${bytes > 1024 ** 3 ? "GB" : "MB"}` : "—";

export function TtsModelsPage() {
  const [models, setModels] = useState<ManagedModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [defaultModelId, setDefaultModelId] = useState("");
  const [runtime, setRuntime] = useState<"local" | "api">("local");
  const [playModelId, setPlayModelId] = useState("");
  const [playVoice, setPlayVoice] = useState("");
  const [sampleText, setSampleText] = useState(SAMPLE_TEXT);
  const [playing, setPlaying] = useState(false);
  const [playError, setPlayError] = useState("");
  const [playResult, setPlayResult] = useState<PlaygroundResult>();
  const [customProviders, setCustomProviders] = useState<CustomProvider[]>([]);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<CustomProvider>(EMPTY_FORM);
  const [formVoices, setFormVoices] = useState("");
  const [formModels, setFormModels] = useState("");
  const [formKey, setFormKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [deleting, setDeleting] = useState("");
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState("");
  const [testAudio, setTestAudio] = useState("");
  const [samples, setSamples] = useState<SampleCatalog | null>(null);
  const [samplesLoading, setSamplesLoading] = useState(true);
  const [downloadingSamples, setDownloadingSamples] = useState(false);
  const [samplesMessage, setSamplesMessage] = useState("");
  const loadSamples = useCallback(() => {
    setSamplesLoading(true);
    api<SampleCatalog>("/tts-models/sample-voices")
      .then((value) => { setSamples(value); setSamplesMessage(""); })
      .catch((error) => setSamplesMessage(error instanceof Error ? error.message : "Không thể tải danh sách voice mẫu."))
      .finally(() => setSamplesLoading(false));
  }, []);
  useEffect(() => { loadSamples(); }, [loadSamples]);
  const load = useCallback(() => {
    api<{ models: ManagedModel[] }>("/tts-models").then((value) => setModels(value.models || [])).catch((error) => setMessage(error instanceof Error ? error.message : "Không thể tải catalog TTS.")).finally(() => setLoading(false));
    api<{ providers: CustomProvider[] }>("/tts-models/providers").then((value) => setCustomProviders(value.providers || [])).catch(() => {});
  }, []);
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

  // Lấy giọng mẫu / chuyển offline theo từng card model (zerotts, vieneu-fast).
  const [cardBusy, setCardBusy] = useState<Record<string, boolean>>({});
  const [cardMsg, setCardMsg] = useState<Record<string, string>>({});
  const offlineTimers = React.useRef<Record<string, number>>({});
  useEffect(() => () => { Object.values(offlineTimers.current).forEach((timer) => window.clearInterval(timer)); }, []);

  const setBusy = (key: string, busy: boolean) =>
    setCardBusy((prev) => ({ ...prev, [key]: busy }));
  const setMsg = (key: string, text: string) =>
    setCardMsg((prev) => ({ ...prev, [key]: text }));

  const pollOfflineJob = (modelId: string) => {
    window.clearInterval(offlineTimers.current[modelId]);
    offlineTimers.current[modelId] = window.setInterval(async () => {
      try {
        const status = await api<{ job: OfflineJob | null }>(`/tts-models/${modelId}/offline-voices`);
        const job = status.job;
        if (!job) { window.clearInterval(offlineTimers.current[modelId]); setBusy(`${modelId}:offline`, false); return; }
        if (job.state === "running") {
          setMsg(`${modelId}:offline`, `Đang render ${job.done}/${job.total} giọng... ${job.log || ""}`);
        } else {
          window.clearInterval(offlineTimers.current[modelId]);
          setBusy(`${modelId}:offline`, false);
          setMsg(`${modelId}:offline`, job.state === "done"
            ? `Đã chuyển ${job.moved ?? job.done}/${job.total} giọng vào thư viện voices.`
            : `Chuyển offline thất bại: ${job.log || "lỗi không rõ"}`);
          loadSamples();
        }
      } catch (error) {
        window.clearInterval(offlineTimers.current[modelId]);
        setBusy(`${modelId}:offline`, false);
        setMsg(`${modelId}:offline`, error instanceof Error ? error.message : "Không đọc được tiến trình.");
      }
    }, 2000);
  };

  const downloadModelSamples = async (model: ManagedModel) => {
    const key = `${model.id}:samples`;
    setBusy(key, true); setMsg(key, "");
    try {
      const result = await postJson<{ cached_or_downloaded: number; requested: number; failed: number; note?: string }>(`/tts-models/sample-voices/download?model_id=${encodeURIComponent(model.id)}`, {});
      setMsg(key, `Đã sẵn sàng ${result.cached_or_downloaded}/${result.requested} giọng mẫu${result.failed ? ` (${result.failed} lỗi)` : ""}${result.note ? ` — ${result.note}` : ""}`);
      loadSamples();
      load();
    } catch (error) {
      setMsg(key, error instanceof Error ? error.message : "Không thể tải giọng mẫu.");
    } finally { setBusy(key, false); }
  };

  const moveModelOffline = async (model: ManagedModel) => {
    const key = `${model.id}:offline`;
    setBusy(key, true); setMsg(key, "");
    try {
      const result = await postJson<{ moved_or_exists?: number; requested?: number; failed?: number; note?: string; job?: OfflineJob }>(`/tts-models/${model.id}/offline-voices`, {});
      if (result.job) {
        setMsg(key, "Đã bắt đầu render từng giọng bằng chính model (job nền)...");
        pollOfflineJob(model.id);
      } else {
        setBusy(key, false);
        setMsg(key, `Đã chuyển ${result.moved_or_exists}/${result.requested} giọng vào thư viện voices${result.failed ? ` (${result.failed} lỗi)` : ""}. Các model clone giờ lấy được để so sánh.`);
      }
    } catch (error) {
      setBusy(key, false);
      setMsg(key, error instanceof Error ? error.message : "Không thể chuyển offline.");
    }
  };

  // Cấu hình nâng cao theo từng model local.
  const [advModel, setAdvModel] = useState<(ManagedModel & { sample_rate?: number | null }) | null>(null);
  const [advOptions, setAdvOptions] = useState<Record<string, string | number>>({});
  const [advSchema, setAdvSchema] = useState<NonNullable<TtsModel["options_schema"]>>([]);
  const [advLoading, setAdvLoading] = useState(false);
  const [advSaving, setAdvSaving] = useState(false);
  const [advMsg, setAdvMsg] = useState("");

  const openAdvanced = async (model: ManagedModel) => {
    setAdvModel(model); setAdvMsg(""); setAdvSchema(model.options_schema || []); setAdvOptions({});
    setAdvLoading(true);
    try {
      const result = await api<{ options: Record<string, string | number>; schema: TtsModel["options_schema"] }>(`/tts-models/${model.id}/options`);
      setAdvSchema(result.schema || []);
      setAdvOptions(result.options || {});
    } catch (error) {
      setAdvMsg(error instanceof Error ? error.message : "Không thể tải cấu hình.");
    } finally { setAdvLoading(false); }
  };

  const saveAdvanced = async () => {
    if (!advModel) return;
    setAdvSaving(true); setAdvMsg("");
    try {
      const result = await put<{ options: Record<string, string | number> }>(`/tts-models/${advModel.id}/options`, { options: advOptions });
      setAdvOptions(result.options || {});
      setAdvMsg("Đã lưu. Playground ở trang này sẽ tự dùng cấu hình này khi nghe thử.");
    } catch (error) {
      setAdvMsg(error instanceof Error ? error.message : "Không thể lưu cấu hình.");
    } finally { setAdvSaving(false); }
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

  const useSampleForPlayground = (modelId: string, voiceId: string) => {
    choosePlayModel(modelId);
    setPlayVoice(voiceId);
    setPlayResult(undefined);
    setPlayError("");
    document.getElementById("tts-playground")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const downloadSamples = async () => {
    setDownloadingSamples(true); setSamplesMessage("");
    try {
      const result = await postJson<{ cached_or_downloaded: number; requested: number; failed: number }>("/tts-models/sample-voices/download", {});
      setSamplesMessage(`Đã sẵn sàng ${result.cached_or_downloaded}/${result.requested} voice mẫu ZeroTTS${result.failed ? ` (${result.failed} lỗi, kiểm tra mạng rồi bấm lại)` : ""}. VieNeu nghe thử qua Playground sau khi cài package.`);
      loadSamples();
      load();
    } catch (error) {
      setSamplesMessage(error instanceof Error ? error.message : "Không thể tải danh sách voice mẫu.");
    } finally { setDownloadingSamples(false); }
  };

  const setField = (key: keyof CustomProvider, value: string | number) =>
    setForm((prev) => ({ ...prev, [key]: value }));
  const inputClass = "h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-primary/30";
  const providerVoiceOptions = useMemo(
    () =>
      formVoices
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const [id, label, language] = line.split("|").map((part) => part.trim());
          return {
            value: id,
            label: label || id,
            description: [id, language].filter(Boolean).join(" · "),
          };
        })
        .filter((option) => option.value),
    [formVoices]
  );

  const openCreate = () => {
    setEditingId(null); setForm(EMPTY_FORM); setFormVoices(""); setFormModels(""); setFormKey("");
    setFormError(""); setTestError(""); setTestAudio("");
    setRuntime("api");
    setDialogOpen(true);
  };

  const openEdit = (catalogId: string) => {
    // Provider nhiều model hiện mỗi model một card, id dạng "<provider>:<model slug>".
    const id = providerIdOf(catalogId);
    const found = customProviders.find((item) => item.id === id);
    if (!found) return;
    setEditingId(id);
    setForm({ ...EMPTY_FORM, ...found });
    setFormVoices((found.voices || []).map((voice) => [voice.id, voice.label || "", voice.language || ""].filter(Boolean).join("|")).join("\n"));
    setFormModels((found.models || []).map((item) => [item.id, item.label || ""].filter(Boolean).join("|")).join("\n"));
    setFormKey("");
    setFormError(""); setTestError(""); setTestAudio(""); setDialogOpen(true);
  };

  const buildPayload = (): Record<string, unknown> => {
    if (form.adapter === "custom" && !(form.base_url || "").trim())
      throw new Error("Adapter Custom cần Base URL (vd: http://localhost:20128/v1); bỏ trống sẽ gọi nhầm api.openai.com.");
    const voices = formVoices.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const [id, label, language] = line.split("|").map((part) => part.trim());
      return { id, label: label || id, language: language || "" };
    }).filter((voice) => voice.id);
    const models = formModels.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
      const [id, label] = line.split("|").map((part) => part.trim());
      return { id, label: label || id };
    }).filter((item) => item.id);
    const payload: Record<string, unknown> = {
      ...form,
      id: editingId || form.id.trim(),
      voices,
      models,
      sample_rate: Number(form.sample_rate) || 24000,
      timeout_seconds: Number(form.timeout_seconds) || 120,
    };
    if (formKey.trim()) payload.api_key = formKey.trim();
    if (form.adapter === "vbee") payload.speed_rate = Number(form.speed_rate) || 1;
    if (form.adapter === "google") payload.speaking_rate = Number(form.speaking_rate) || 1;
    return payload;
  };

  const saveProvider = async () => {
    setSaving(true); setFormError("");
    try {
      const payload = buildPayload();
      if (!payload.id) throw new Error("Cần nhập id cho provider (a-z, 0-9, -, _).");
      if (editingId) await put(`/tts-models/providers/${editingId}`, payload);
      else await postJson("/tts-models/providers", payload);
      setDialogOpen(false);
      await load();
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Không thể lưu provider.");
    } finally { setSaving(false); }
  };

  const removeProvider = async (catalogId: string) => {
    const id = providerIdOf(catalogId);
    const siblings = models.filter((model) => model.custom && providerIdOf(model.id) === id).length;
    const warning = siblings > 1 ? ` Provider này đang phục vụ ${siblings} model, tất cả sẽ bị xóa.` : "";
    if (!window.confirm(`Xóa custom provider "${id}"?${warning}`)) return;
    setDeleting(id); setMessage("");
    try {
      await del(`/tts-models/providers/${id}`);
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Không thể xóa provider.");
    } finally { setDeleting(""); }
  };

  const testProvider = async () => {
    setTesting(true); setTestError(""); setTestAudio("");
    try {
      const payload = buildPayload();
      const result = await postJson<{ audio_base64: string; mime_type: string; latency_seconds: number }>("/tts-models/providers/test", {
        config: payload, api_key: formKey.trim() || undefined,
        voice: form.voice || null, text: sampleText.slice(0, 300),
      });
      setTestAudio(`data:${result.mime_type};base64,${result.audio_base64}`);
    } catch (error) {
      setTestError(error instanceof Error ? error.message : "Test provider thất bại.");
    } finally { setTesting(false); }
  };

  const adapterHint = ADAPTERS.find((item) => item.value === form.adapter)?.hint || "";

  return <div className="space-y-7">
    <Header
      title="Model & provider TTS"
      subtitle="Model local dùng tài nguyên trên máy; provider API chạy trong pool riêng nên không phải chờ GPU local. Nghe thử và đo tốc độ trước khi dùng cho production."
      action={<Button onClick={openCreate}><Plus className="h-4 w-4" /> Thêm provider TTS</Button>}
    />


    {message && <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{message}</div>}
    <section id="tts-playground" className="grid gap-5 scroll-mt-4 rounded-xl border bg-card p-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(300px,.75fr)]">
      <div className="space-y-4">
        <div className="flex items-center gap-2"><Volume2 className="h-5 w-5 text-primary" /><h2 className="text-lg font-semibold">Playground giọng đọc</h2></div>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1.5 text-xs font-medium">Model / provider
            <select className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={playModelId} onChange={(event) => choosePlayModel(event.target.value)}>
              {playableModels.map((model) => <option key={model.id} value={model.id}>{isApi(model) ? "API · " : "Local · "}{model.name}</option>)}
            </select>
          </label>
          <div className="space-y-1.5 text-xs font-medium">
            <label htmlFor="tts-playground-voice">Voice ID</label>
            {playModel?.voices?.length ? <Combobox
              id="tts-playground-voice"
              className="w-full"
              value={playVoice}
              options={playModel.voices.map((voice) => ({
                value: voice.id,
                label: voice.label || voice.id,
                description: [voice.id, voice.language].filter(Boolean).join(" · "),
              }))}
              onChange={setPlayVoice}
              placeholder="Tìm hoặc chọn voice..."
            />
              : <input id="tts-playground-voice" className="h-9 w-full rounded-md border bg-background px-3 text-sm" value={playVoice} onChange={(event) => setPlayVoice(event.target.value)} placeholder="Voice mặc định" />}
          </div>
        </div>
        <textarea aria-label="Nội dung nghe thử" className="min-h-28 w-full resize-y rounded-md border bg-background px-3 py-2 text-sm leading-relaxed outline-none focus:ring-2 focus:ring-primary/30" maxLength={1200} value={sampleText} onChange={(event) => setSampleText(event.target.value)} />
        <div className="flex items-center justify-between gap-3"><span className="text-xs tabular-nums text-muted-foreground">{sampleText.length}/1200 ký tự</span><Button onClick={runPlayground} disabled={playing || !playModelId || !sampleText.trim()}>{playing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}{playing ? "Đang tổng hợp..." : "Tạo & nghe thử"}</Button></div>
        <p className="text-[11px] text-muted-foreground">Playground tự dùng “Cấu hình nâng cao” đã lưu của model (nếu có).</p>
        {!loading && !playableModels.length && <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">Chưa có model phù hợp để nghe thử. Model dùng giọng tham chiếu hiện chưa được hỗ trợ trong playground.</p>}
        {playError && <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{playError}</p>}
      </div>
      <div className="flex min-h-52 flex-col justify-center rounded-lg bg-muted/50 p-4">
        {playResult ? <div className="space-y-4"><audio className="w-full" controls autoPlay src={`data:${playResult.mime_type};base64,${playResult.audio_base64}`} /><div className="grid grid-cols-2 gap-3 text-xs tabular-nums"><Metric label="Độ trễ" value={`${playResult.latency_seconds.toFixed(2)} s`} /><Metric label="Audio" value={`${playResult.duration_seconds.toFixed(2)} s`} /><Metric label="Real-time factor" value={playResult.realtime_factor == null ? "—" : `${playResult.realtime_factor.toFixed(2)}×`} /><Metric label="Tốc độ" value={playResult.characters_per_second == null ? "—" : `${playResult.characters_per_second.toFixed(1)} ký tự/s`} /><Metric label="Sample rate" value={`${playResult.sample_rate.toLocaleString()} Hz`} /></div><p className="text-xs text-muted-foreground">{playResult.realtime_factor == null ? "Không đủ dữ liệu để so tốc độ thời gian thực." : playResult.realtime_factor < 1 ? `Nhanh hơn thời lượng audio khoảng ${(1 / playResult.realtime_factor).toFixed(1)} lần.` : "Chậm hơn thời lượng audio; phù hợp để kiểm tra chất lượng hơn là xử lý thời gian thực."}</p></div>
          : <div className="text-center text-muted-foreground"><Gauge className="mx-auto mb-3 h-8 w-8" /><p className="text-sm font-medium text-foreground">Chưa có phép đo</p><p className="mt-1 text-xs">RTF dưới 1× là nhanh hơn thời lượng audio.</p></div>}
      </div>
    </section>
    <section className="space-y-4 rounded-xl border bg-card p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2"><Volume2 className="h-5 w-5 text-primary" /><div><h2 className="text-lg font-semibold">Danh sách voice mẫu</h2><p className="text-xs text-muted-foreground">ZeroTTS nghe trực tiếp file mẫu (không cần tải weights ~900 MB) · VieNeu V3 Turbo nghe qua Playground sau khi cài package{samples ? ` — ${samples.cached}/${samples.total} clip đã sẵn sàng` : ""}.</p></div></div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={loadSamples} disabled={samplesLoading}>{samplesLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Tải lại</Button>
          <Button size="sm" onClick={downloadSamples} disabled={downloadingSamples}>{downloadingSamples ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}{downloadingSamples ? "Đang tải..." : "Tải danh sách voice mẫu"}</Button>
        </div>
      </div>
      {samplesMessage && <p className="rounded-md border px-3 py-2 text-xs text-muted-foreground">{samplesMessage}</p>}
      {samplesLoading && <LoadingState text="Đang tải danh sách voice mẫu..." />}
      {!samplesLoading && samples && (samples.total === 0
        ? <EmptyState text="Chưa có voice mẫu nào. Bấm “Tải danh sách voice mẫu” để lấy preview ZeroTTS, hoặc cài package VieNeu để hiện presets." />
        : <div className="grid gap-4 lg:grid-cols-2">
          {(Object.values(samples.models) as SampleGroup[]).map((group) => <Card key={group.model_id}>
            <CardHeader className="space-y-1 pb-3">
              <div className="flex items-center justify-between gap-3"><CardTitle className="text-base">{group.name}</CardTitle><span className="text-xs tabular-nums text-muted-foreground">{group.cached}/{group.count} sẵn sàng</span></div>
              <p className="text-[11px] text-muted-foreground">{group.model_id === "zerotts" ? "File mẫu gốc từ Hugging Face — bấm play để nghe ngay." : "Preset trong package vieneu — bấm “Nghe thử” để tổng hợp qua Playground."}</p>
            </CardHeader>
            <CardContent className="space-y-2 text-xs">
              {group.voices.length === 0 && <p className="rounded-md border border-dashed px-3 py-2 text-muted-foreground">{group.model_id === "zerotts" ? "Chưa tải được danh sách — bấm “Tải danh sách voice mẫu”." : "Chưa cài package vieneu — dùng nút “Tải model” ở catalog bên dưới."}</p>}
              {group.voices.map((voice) => <div key={voice.id} className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/30 px-2.5 py-2">
                <div className="min-w-0 flex-1"><p className="truncate font-medium">{voice.label || voice.id}</p><p className="font-mono text-[10px] text-muted-foreground">{voice.id}</p></div>
                {voice.audio_url
                  ? <audio className="h-8 w-44" controls preload="none" src={voice.audio_url} />
                  : <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">{group.model_id === "zerotts" ? "chưa tải" : "qua Playground"}</span>}
                <Button size="sm" variant="outline" onClick={() => useSampleForPlayground(group.model_id, voice.id)}><Play className="h-3.5 w-3.5" /> Nghe thử</Button>
              </div>)}
            </CardContent>
          </Card>)}
        </div>)}
    </section>
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-lg font-semibold">Catalog TTS</h2><p className="text-xs text-muted-foreground">Hai runtime độc lập, cùng dùng một pipeline audiobook.</p></div><div className="flex items-center gap-2"><div className="inline-flex rounded-lg border bg-muted/40 p-1"><Button size="sm" variant={runtime === "local" ? "secondary" : "ghost"} aria-pressed={runtime === "local"} onClick={() => setRuntime("local")}><Cpu className="h-3.5 w-3.5" /> Local ({models.filter((model) => !isApi(model)).length})</Button><Button size="sm" variant={runtime === "api" ? "secondary" : "ghost"} aria-pressed={runtime === "api"} onClick={() => setRuntime("api")}><Cloud className="h-3.5 w-3.5" /> API ({models.filter(isApi).length})</Button></div></div></div>
    {loading && <LoadingState text="Đang tải catalog TTS..." />}
    {!loading && filteredModels.length === 0 && <div className="space-y-3">
      <EmptyState text={runtime === "api" ? "Chưa có provider API nào được cấu hình." : "Chưa có model TTS local nào."} />
      {runtime === "api" && <div className="flex justify-center"><Button size="sm" variant="outline" onClick={openCreate}><Plus className="h-3.5 w-3.5" /> Thêm provider TTS đầu tiên</Button></div>}
    </div>}
    <div className="grid gap-4 lg:grid-cols-2">
      {filteredModels.map((model) => {
        const { install, job } = model;
        const running = job?.state === "running";
        const isFixedCast = model.id === "zerotts" || model.id === "vieneu-fast";
        const version = install.package_version || "";
        return <Card key={model.id}>
          <CardHeader className="space-y-1 pb-3">
            <div className="flex items-start justify-between gap-3"><div className="flex items-center gap-2">{isApi(model) ? <Cloud className="h-4 w-4 text-sky-600" /> : <Cpu className="h-4 w-4 text-violet-600" />}<CardTitle className="text-base">{model.name}</CardTitle>{model.custom && <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-semibold text-primary">Custom</span>}{version && <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-[10px] text-muted-foreground" title={install.package ? `Package ${install.package}` : "Phiên bản đã cài"}>v{version}</span>}</div>
              {install.ready === true ? <span className="inline-flex items-center gap-1 text-xs text-emerald-600"><CheckCircle2 className="h-3.5 w-3.5" /> Sẵn sàng</span>
                : install.ready === false ? <span className="inline-flex items-center gap-1 text-xs text-amber-600"><TriangleAlert className="h-3.5 w-3.5" /> {model.custom && !model.configured ? "Thiếu API key" : "Chưa sẵn sàng"}</span>
                : <span className="text-xs text-muted-foreground">Theo package</span>}</div>
            <p className="font-mono text-[11px] text-muted-foreground">{model.model_id}</p>
          </CardHeader>
          <CardContent className="space-y-3 text-xs">
            <p className="text-muted-foreground">{install.detail}</p>
            {isFixedCast && model.voices?.length ? <p className="rounded bg-muted px-2 py-1.5">{model.voices.length} giọng mẫu sẵn sàng nghe thử — xem mục “Danh sách voice mẫu” ở trên.</p> : null}
            {isFixedCast && !model.voices?.length ? <p className="rounded border border-dashed px-2 py-1.5 text-muted-foreground">Chưa có danh sách voice — bấm “Tải giọng mẫu” ở dưới{model.id === "vieneu-fast" ? " hoặc “Tải model” để cài package" : ""}.</p> : null}
            {install.path && <p className="break-all rounded bg-muted px-2 py-1.5 font-mono text-[10px]">{install.path}</p>}
            {install.managed ? <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" onClick={() => start(model)} disabled={running}>{running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}{install.ready ? "Kiểm tra / sửa" : "Tải model"}</Button>
              <Button size="sm" variant="outline" onClick={() => start(model, true)} disabled={running}><RefreshCw className="h-3.5 w-3.5" /> Cập nhật version</Button>
              <span className="text-muted-foreground">{size(install.size_bytes)}</span>
            </div> : <a className="inline-flex items-center gap-1 text-primary underline" href={`https://huggingface.co/${model.model_id}`} target="_blank" rel="noreferrer">Xem nguồn model <ExternalLink className="h-3 w-3" /></a>}
            {isFixedCast && <div className="flex flex-wrap items-center gap-2 border-t pt-2">
              <Button size="sm" variant="outline" onClick={() => downloadModelSamples(model)} disabled={cardBusy[`${model.id}:samples`]}>{cardBusy[`${model.id}:samples`] ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />} Tải giọng mẫu</Button>
              <Button size="sm" variant="outline" onClick={() => moveModelOffline(model)} disabled={cardBusy[`${model.id}:offline`]}>{cardBusy[`${model.id}:offline`] ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FolderDown className="h-3.5 w-3.5" />} Chuyển sang offline</Button>
            </div>}
            {!isApi(model) && <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => openAdvanced(model)}><SlidersHorizontal className="h-3.5 w-3.5" /> Cấu hình nâng cao</Button>
            </div>}
            {cardMsg[`${model.id}:samples`] && <p className="rounded-md border px-2 py-1.5 text-muted-foreground">{cardMsg[`${model.id}:samples`]}</p>}
            {cardMsg[`${model.id}:offline`] && <p className="rounded-md border px-2 py-1.5 text-muted-foreground">{cardMsg[`${model.id}:offline`]}</p>}
            {job && <pre className={`max-h-28 overflow-auto whitespace-pre-wrap rounded p-2 text-[10px] ${job.state === "failed" ? "bg-destructive/10 text-destructive" : "bg-muted"}`}>{job.state === "running" ? "Đang tải…\n" : ""}{job.log || "Đang chờ dữ liệu..."}</pre>}
            {model.custom && <div className="flex flex-wrap items-center gap-2 border-t pt-2">
              <Button size="sm" variant="outline" onClick={() => openEdit(model.id)}><Pencil className="h-3.5 w-3.5" /> Sửa</Button>
              <Button size="sm" variant="outline" onClick={() => removeProvider(model.id)} disabled={deleting === providerIdOf(model.id)}>{deleting === providerIdOf(model.id) ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />} Xóa</Button>
              <span className="text-muted-foreground">{model.has_api_key ? "Đã lưu API key" : "Key qua biến môi trường"}</span>
            </div>}
          </CardContent>
        </Card>;
      })}
    </div>
    <Dialog open={advModel !== null} onOpenChange={(open) => { if (!open) setAdvModel(null); }}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Cấu hình nâng cao · {advModel?.name}</DialogTitle>
          <DialogDescription>Thông số kỹ thuật của model và tùy chọn suy luận (nếu có). Cấu hình đã lưu được Playground ở trang này tự dùng khi nghe thử.</DialogDescription>
        </DialogHeader>
        {advLoading && <LoadingState text="Đang tải cấu hình..." />}
        {!advLoading && advModel && <div className="space-y-4 text-xs">
          <div className="grid gap-2 rounded-md border bg-muted/30 p-3 sm:grid-cols-2">
            <InfoRow label="Engine ID" value={advModel.id} mono />
            <InfoRow label="Package" value={advModel.install.package ? `${advModel.install.package}${advModel.install.package_version ? ` v${advModel.install.package_version}` : " (chưa cài)"}` : "—"} mono />
            <InfoRow label="Model / repo" value={advModel.model_id} mono />
            <InfoRow label="Sample rate" value={advModel.sample_rate ? `${advModel.sample_rate.toLocaleString()} Hz` : "—"} />
            <InfoRow label="Giọng mặc định" value={advModel.default_voice || "—"} mono />
            <InfoRow label="Số giọng mẫu" value={String(advModel.voices?.length || 0)} />
            <InfoRow label="Tham chiếu clone" value={advModel.supports_reference ? "Có (dùng clip mẫu)" : "Không (giọng cố định)"} />
            <InfoRow label="Chế độ" value={(advModel.capabilities as { offline?: boolean; online?: boolean }).offline ? "Offline" : (advModel.capabilities as { offline?: boolean; online?: boolean }).online ? "Online" : "—"} />
          </div>
          {advSchema.length > 0 ? <div className="space-y-3 rounded-md border p-3">
            <p className="text-xs font-semibold">Tùy chọn suy luận</p>
            <div className="grid gap-3 sm:grid-cols-2">
              {advSchema.map((field) => <label key={field.key} className="space-y-1.5 font-medium">{field.label}
                {field.type === "select" ? (
                  <select className={inputClass} value={String(advOptions[field.key] ?? field.default)} onChange={(event) => setAdvOptions((prev) => ({ ...prev, [field.key]: event.target.value }))}>
                    {(field.choices || []).map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
                  </select>
                ) : (
                  <input className={inputClass} type="number" min={field.min} max={field.max} step={field.step} value={advOptions[field.key] ?? field.default} onChange={(event) => setAdvOptions((prev) => ({ ...prev, [field.key]: Number(event.target.value) }))} />
                )}
              </label>)}
            </div>
          </div> : <p className="rounded-md border border-dashed px-3 py-2 text-muted-foreground">Model này không có tham số suy luận mở — cấu hình nâng cao chỉ xem thông tin.</p>}
          {advMsg && <p className="rounded-md border px-3 py-2 text-muted-foreground">{advMsg}</p>}
        </div>}
        <DialogFooter className="gap-2">
          {advSchema.length > 0 && <Button onClick={saveAdvanced} disabled={advSaving || advLoading}>{advSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}Lưu cấu hình</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{editingId ? `Sửa provider ${editingId}` : "Thêm custom API TTS"}</DialogTitle>
          <DialogDescription>Google Cloud TTS, OpenAI-compatible custom endpoint, Gemini, ElevenLabs hoặc Vbee. Lưu xong dùng Playground để nghe thử.</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1.5 text-xs font-medium">Provider ID (slug){!editingId && <input className={`${inputClass} font-mono`} value={form.id} onChange={(event) => setField("id", event.target.value)} placeholder="vd: google-cloud-vi" />} {editingId && <p className="font-mono text-sm">{editingId}</p>}</label>
          <label className="space-y-1.5 text-xs font-medium">Tên hiển thị<input className={inputClass} value={form.name || ""} onChange={(event) => setField("name", event.target.value)} placeholder="vd: Google Cloud VI" /></label>
          <label className="space-y-1.5 text-xs font-medium sm:col-span-2">Adapter
            <select className={inputClass} value={form.adapter} onChange={(event) => setField("adapter", event.target.value)}>
              {ADAPTERS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
            {adapterHint && <span className="font-normal text-muted-foreground">{adapterHint}</span>}
          </label>
          <label className="space-y-1.5 text-xs font-medium sm:col-span-2">Base URL {form.adapter === "custom" ? <span className="text-destructive">(bắt buộc với adapter Custom)</span> : "(bỏ trống = mặc định của adapter)"}<input className={`${inputClass} font-mono`} value={form.base_url || ""} onChange={(event) => setField("base_url", event.target.value)} placeholder={form.adapter === "custom" ? "http://localhost:20128/v1" : "https://custom-tts.example.com/v1"} /></label>
          <label className="space-y-1.5 text-xs font-medium">Model / voice model {formModels.trim() && <span className="font-normal text-muted-foreground">(bỏ qua khi có danh sách model)</span>}<input className={`${inputClass} font-mono`} value={form.model || ""} onChange={(event) => setField("model", event.target.value)} placeholder={form.adapter === "google" ? "không bắt buộc" : "vd: gpt-4o-mini-tts"} /></label>
          <div className="space-y-1.5 text-xs font-medium">
             <label htmlFor="provider-default-voice">Voice mặc định</label>
             {providerVoiceOptions.length ? <Combobox
               id="provider-default-voice"
               className="w-full"
               value={form.voice || ""}
               options={[
                 { value: "", label: "Để trống / mặc định adapter" },
                 ...providerVoiceOptions,
               ]}
               onChange={(voice) => setField("voice", voice)}
               placeholder="Tìm hoặc chọn voice..."
             />
               : <input id="provider-default-voice" className={`${inputClass} font-mono`} value={form.voice || ""} onChange={(event) => setField("voice", event.target.value)} placeholder={form.adapter === "google" ? "vi-VN-Standard-A" : form.adapter === "elevenlabs" ? "voice-id" : "alloy / Kore"} />}
           </div>
          <label className="space-y-1.5 text-xs font-medium">API key (lưu local, để trống = dùng biến môi trường)<input type="password" className={`${inputClass} font-mono`} value={formKey} onChange={(event) => setFormKey(event.target.value)} placeholder={editingId ? "Để trống để giữ key cũ" : "sk-..."} autoComplete="off" /></label>
          <label className="space-y-1.5 text-xs font-medium">Biến môi trường key<input className={`${inputClass} font-mono`} value={form.api_key_env || ""} onChange={(event) => setField("api_key_env", event.target.value)} placeholder="vd: GOOGLE_TTS_API_KEY" /></label>
          {(form.adapter === "custom" || form.adapter === "openai") && <label className="space-y-1.5 text-xs font-medium sm:col-span-2">Instructions (system prompt giọng)<input className={inputClass} value={form.instructions || ""} onChange={(event) => setField("instructions", event.target.value)} placeholder="vd: Giọng nữ miền Bắc, kể chuyện chậm rãi" /></label>}
          {form.adapter === "google" && <>
            <label className="space-y-1.5 text-xs font-medium">Language code<input className={`${inputClass} font-mono`} value={form.language_code || ""} onChange={(event) => setField("language_code", event.target.value)} placeholder="vi-VN (tự suy từ voice nếu trống)" /></label>
            <label className="space-y-1.5 text-xs font-medium">Speaking rate<input type="number" min={0.25} max={4} step={0.05} className={inputClass} value={form.speaking_rate ?? 1} onChange={(event) => setField("speaking_rate", Number(event.target.value))} /></label>
          </>}
          {form.adapter === "vbee" && <>
            <label className="space-y-1.5 text-xs font-medium">App ID<input className={`${inputClass} font-mono`} value={form.app_id || ""} onChange={(event) => setField("app_id", event.target.value)} /></label>
            <label className="space-y-1.5 text-xs font-medium">Callback URL<input className={`${inputClass} font-mono`} value={form.callback_url || ""} onChange={(event) => setField("callback_url", event.target.value)} /></label>
          </>}
          <label className="space-y-1.5 text-xs font-medium">Sample rate (Hz)<input type="number" min={8000} max={48000} step={1000} className={inputClass} value={form.sample_rate ?? 24000} onChange={(event) => setField("sample_rate", Number(event.target.value))} /></label>
          <label className="space-y-1.5 text-xs font-medium">Timeout (giây)<input type="number" min={10} max={600} className={inputClass} value={form.timeout_seconds ?? 120} onChange={(event) => setField("timeout_seconds", Number(event.target.value))} /></label>
          <label className="space-y-1.5 text-xs font-medium sm:col-span-2">Danh sách model của provider (mỗi dòng: model|label) — mỗi model thành một mục riêng trong catalog, dùng chung base URL và API key<textarea className="min-h-16 w-full resize-y rounded-md border bg-background px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary/30" value={formModels} onChange={(event) => setFormModels(event.target.value)} placeholder={"google-tts/vi|Google VI\ngoogle-tts/en|Google EN"} /></label>
          <label className="space-y-1.5 text-xs font-medium sm:col-span-2">Danh sách voice (mỗi dòng: id|label|language)<textarea className="min-h-16 w-full resize-y rounded-md border bg-background px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary/30" value={formVoices} onChange={(event) => setFormVoices(event.target.value)} placeholder={"vi-VN-Standard-A|Nữ miền Nam|vi\nvi-VN-Wavenet-D|Nam miền Bắc|vi"} /></label>
        </div>
        {formError && <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{formError}</p>}
        {testError && <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{testError}</p>}
        {testAudio && <audio className="w-full" controls autoPlay src={testAudio} />}
        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={testProvider} disabled={testing || saving}>{testing ? <Loader2 className="h-4 w-4 animate-spin" /> : <FlaskConical className="h-4 w-4" />}{testing ? "Đang test..." : "Test trước khi lưu"}</Button>
          <Button onClick={saveProvider} disabled={saving}>{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : null}{editingId ? "Lưu thay đổi" : "Thêm provider"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><p className="text-muted-foreground">{label}</p><p className="mt-0.5 font-mono font-semibold text-foreground">{value}</p></div>;
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return <div className="min-w-0"><p className="text-muted-foreground">{label}</p><p className={`mt-0.5 break-all font-medium text-foreground ${mono ? "font-mono text-[11px]" : ""}`}>{value}</p></div>;
}