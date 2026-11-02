"""Make ZeroTTS actually faster on a CUDA GPU.

Running the stock ONNX graphs under the CUDA execution provider is barely quicker
than the CPU (measured on an RTX 3050 Laptop: ~6.3 s vs ~6.4 s for 4.7 s of speech),
for two reasons this module removes:

1. ``local_frame_decode`` is one unrolled graph of ~2,500 tiny kernels per frame, so
   it is bound by kernel-launch overhead (worse under Windows WDDM than on a Linux
   T4). A CUDA graph replays all of them in one launch, but ORT only captures a graph
   when every node lives on the GPU, and its 17 ``ArgMax`` nodes run on int64
   (``Cast(Greater(CumSum(p), u))`` — inverse-CDF sampling), which has no CUDA kernel.
   ``build_frame_decoder`` rewrites each one exactly: CumSum of non-negative values
   never decreases, so the first index where it exceeds ``u`` equals
   ``N - count(True)`` (and 0 when nothing is True, as ArgMax returns). The rewritten
   graph gives bit-identical codes and is cached in ``<model_dir>/onnx_gpu``.
2. ``prefix_step`` returns the whole growing KV cache every frame; with numpy feeds
   that cache crosses PCIe twice per frame. ``generate_frames`` binds it as a device
   OrtValue so it never leaves the GPU.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Bump when the rewrite changes so cached graphs from an older build are rebuilt.
BUILD_VERSION = 1
_GRAPH = "local_frame_decode"


def gpu_dir(model_dir: Path) -> Path:
    return Path(model_dir) / "onnx_gpu"


def _source(model_dir: Path) -> Path:
    return Path(model_dir) / "onnx" / f"{_GRAPH}.onnx"


def _stamp(model_dir: Path) -> dict:
    src = _source(model_dir)
    data = [p for p in src.parent.glob(f"{_GRAPH}.onnx*") if p != src]
    return {"version": BUILD_VERSION, "size": src.stat().st_size,
            "mtime": int(src.stat().st_mtime), "data": sorted((p.name, p.stat().st_size) for p in data)}


def built_graph(model_dir: Path) -> Path | None:
    out = gpu_dir(model_dir) / f"{_GRAPH}.onnx"
    meta = gpu_dir(model_dir) / "build.json"
    try:
        if out.is_file() and json.loads(meta.read_text(encoding="utf-8")) == _stamp(model_dir):
            return out
    except (OSError, ValueError):
        pass
    return None


def build_frame_decoder(model_dir: Path, *, force: bool = False) -> Path:
    """Write the CUDA-graph-capturable local_frame_decode (batch 1); returns its path."""
    if not force and (cached := built_graph(model_dir)):
        return cached
    import onnx
    import onnxruntime as ort
    from onnx import TensorProto, helper, numpy_helper, shape_inference

    started = time.perf_counter()
    out_dir = gpu_dir(model_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*"):
        stale.unlink()

    # 1. Pin the batch dim: every per-frame shape is then static, which is what lets
    #    shape inference size the sampling rows below and the CUDA graph be replayed.
    model = onnx.load(str(_source(model_dir)))
    for value in model.graph.input:
        for dim in value.type.tensor_type.shape.dim:
            if dim.dim_param:
                dim.ClearField("dim_param")
                dim.dim_value = 1
    fixed = out_dir / "fixed.onnx"
    onnx.save(model, str(fixed), save_as_external_data=True, location="fixed.data")
    del model

    # 2. Fold constants/shapes with ORT's basic (provider-independent) passes.
    folded = out_dir / "folded.onnx"
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.optimized_model_filepath = str(folded)
    options.add_session_config_entry("session.optimized_model_external_initializers_file_name", "folded.data")
    options.add_session_config_entry("session.optimized_model_external_initializers_min_size_in_bytes", "1024")
    ort.InferenceSession(str(fixed), options, providers=["CPUExecutionProvider"])

    # 3. Replace Cast(bool->int64) + ArgMax with the equivalent all-float expression.
    model = onnx.load(str(folded))
    graph = model.graph
    shapes = shape_inference.infer_shapes(onnx.load(str(folded), load_external_data=False))
    infos = {v.name: v for v in list(shapes.graph.value_info) + list(shapes.graph.input)}
    producer = {o: n for n in graph.node for o in n.output}
    consumers: dict[str, int] = {}
    for node in graph.node:
        for name in node.input:
            consumers[name] = consumers.get(name, 0) + 1

    def const(name: str, value) -> str:
        graph.initializer.append(numpy_helper.from_array(np.asarray(value), name))
        return name

    axes = const("zg_axes", np.array([-1], dtype=np.int64))
    zero = const("zg_zero", np.array(0, dtype=np.float32))
    # Keyed by node identity, not name: exported graphs may leave names empty.
    replaced: dict[int, list] = {}
    dropped: set[int] = set()
    for node in graph.node:
        if node.op_type != "ArgMax":
            continue
        attrs = {a.name: helper.get_attribute_value(a) for a in node.attribute}
        cast = producer.get(node.input[0])
        mask = cast.input[0] if cast is not None and cast.op_type == "Cast" else None
        if (mask is None or producer.get(mask) is None or producer[mask].op_type != "Greater"
                or attrs.get("axis", 0) != -1 or attrs.get("keepdims", 1) != 0
                or attrs.get("select_last_index", 0)):
            raise RuntimeError(f"ZeroTTS GPU build: {node.name} không đúng dạng sampling dự kiến")
        n = infos[mask].type.tensor_type.shape.dim[-1].dim_value
        if n <= 0:
            raise RuntimeError(f"ZeroTTS GPU build: không xác định được kích thước của {mask}")
        p = node.name or f"zg_argmax_{len(replaced)}"
        replaced[id(node)] = [
            helper.make_node("Cast", [mask], [p + "_f"], to=TensorProto.FLOAT, name=p + "_castf"),
            helper.make_node("ReduceSum", [p + "_f", axes], [p + "_s"], keepdims=0, name=p + "_sum"),
            helper.make_node("Sub", [const(p + "_n", np.array(n, dtype=np.float32)), p + "_s"],
                             [p + "_d"], name=p + "_sub"),
            helper.make_node("Greater", [p + "_s", zero], [p + "_any"], name=p + "_any"),
            helper.make_node("Where", [p + "_any", p + "_d", zero], [p + "_w"], name=p + "_where"),
            helper.make_node("Cast", [p + "_w"], [node.output[0]], to=TensorProto.INT64, name=p + "_casti"),
        ]
        if consumers.get(cast.output[0]) == 1:
            dropped.add(id(cast))
    nodes = []
    for node in graph.node:
        if id(node) in replaced:
            nodes += replaced[id(node)]
        elif id(node) not in dropped:
            nodes.append(node)
    del graph.node[:]
    graph.node.extend(nodes)
    out = out_dir / f"{_GRAPH}.onnx"
    onnx.save(model, str(out), save_as_external_data=True, location=f"{_GRAPH}.data")
    del model
    for tmp in ("fixed.onnx", "fixed.data", "folded.onnx", "folded.data"):
        (out_dir / tmp).unlink(missing_ok=True)
    (out_dir / "build.json").write_text(json.dumps(_stamp(model_dir)), encoding="utf-8")
    logger.info("ZeroTTS GPU build: %d ArgMax thay thế, %.1fs -> %s",
                len(replaced), time.perf_counter() - started, out)
    return out


class CudaGraphSession:
    """A drop-in ``.run(output_names, feeds)`` for a fixed-shape graph, replayed as a
    CUDA graph. Inputs are copied into persistent device buffers (the graph is bound
    to their addresses); a call with other shapes goes to ``fallback``."""

    def __init__(self, path: Path, fallback, device_id: int = 0):
        import onnxruntime as ort

        self._ort = ort
        self.fallback = fallback
        self.session = ort.InferenceSession(
            str(path), ort.SessionOptions(),
            providers=[("CUDAExecutionProvider", {"device_id": device_id, "enable_cuda_graph": "1"})],
        )
        self.device_id = device_id
        self._names = [o.name for o in self.session.get_outputs()]
        self._inputs: dict | None = None
        self._outputs: dict | None = None
        self._binding = None

    def get_providers(self):
        return self.session.get_providers()

    def _bind(self, feeds: dict) -> None:
        ort = self._ort
        probe = self.fallback.run(None, feeds)
        self._dtypes = {k: np.asarray(v).dtype for k, v in feeds.items()}
        self._inputs = {k: ort.OrtValue.ortvalue_from_numpy(np.ascontiguousarray(v), "cuda", self.device_id)
                        for k, v in feeds.items()}
        self._outputs = {name: ort.OrtValue.ortvalue_from_numpy(np.zeros_like(value), "cuda", self.device_id)
                         for name, value in zip(self._names, probe)}
        self._binding = self.session.io_binding()
        for name, value in self._inputs.items():
            self._binding.bind_ortvalue_input(name, value)
        for name, value in self._outputs.items():
            self._binding.bind_ortvalue_output(name, value)

    def run(self, output_names, feeds, run_options=None):
        if self._inputs is None:
            self._bind(feeds)
        elif any(tuple(np.shape(v)) != tuple(self._inputs[k].shape()) for k, v in feeds.items()):
            return self.fallback.run(output_names, feeds, run_options)
        for name, value in feeds.items():
            self._inputs[name].update_inplace(np.ascontiguousarray(value, dtype=self._dtypes[name]))
        self.session.run_with_iobinding(self._binding)
        return [self._outputs[name].numpy() for name in (output_names or self._names)]


def accelerate(model, model_dir: Path) -> bool:
    """Swap in the CUDA-graph frame decoder. Returns False (model untouched) when the
    session is not on CUDA or the build fails, so the caller just keeps going."""
    if "CUDAExecutionProvider" not in model.prefix_step_sess.get_providers():
        return False
    try:
        path = build_frame_decoder(Path(model_dir))
        model.local_frame_decode_sess = CudaGraphSession(path, model.local_frame_decode_sess)
    except Exception:
        logger.exception("ZeroTTS GPU: không dựng được graph tối ưu — dùng CUDA thường")
        return False
    return True


def generate_frames(model, text, min_frames, max_frames, voice_emb, eoa_extra_frames=1):
    """``zerotts.Synthesizer._generate_frames`` (same defaults, cfg_scale 1) with the
    KV cache kept on the GPU between frames. Yields (1, K) int64 frames."""
    import onnxruntime as ort

    n_voice = voice_emb.shape[1]
    text_ids = model.tokenizer(text)[None, :].astype(np.int64)
    txt_lengths = np.array([text_ids.shape[1]], dtype=np.int64)
    h, packed_kv, full_valid, cross_kv, text_valid = model._prefix_step_init(text_ids, txt_lengths, voice_emb)

    session = model.prefix_step_sess
    device = ort.OrtValue.ortvalue_from_numpy
    kv = device(np.ascontiguousarray(packed_kv), "cuda", 0)
    valid = device(np.ascontiguousarray(full_valid), "cuda", 0)
    cross = device(np.ascontiguousarray(cross_kv), "cuda", 0)
    text_mask = device(np.ascontiguousarray(text_valid), "cuda", 0)
    static = {
        "external_embed": np.zeros((1, 1, model.d_model), dtype=np.float32),
        "use_external_embed": np.zeros((1, 1), dtype=bool),
        "new_valid": np.ones((1, 1), dtype=bool),
        "new_bidirectional": np.zeros((1, 1), dtype=bool),
    }
    seen_mask = np.zeros((1, model.num_codebooks, model.codebook_size), dtype=bool)
    tail_left = None
    t = 0
    while True:
        ctrl, codes = model._local_decode_frame(
            h, forbid_eoa=(t < min_frames or tail_left is not None),
            text_temperature=1.0, text_topk=50, audio_temperature=0.8, audio_topk=25,
            audio_topp=0.95, audio_repetition_penalty=1.2, seen_mask=seen_mask, cfg_scale=1.0)
        if tail_left is None and ctrl == model.eoa_id:
            tail_left = max(0, int(eoa_extra_frames))
        if (tail_left is not None and tail_left <= 0) or t >= max_frames:
            break
        yield codes
        if tail_left is not None:
            tail_left -= 1
            if tail_left <= 0:
                break
        binding = session.io_binding()
        for name, value in static.items():
            binding.bind_cpu_input(name, value)
        binding.bind_cpu_input("frame_codes", np.ascontiguousarray(codes[:, None, :], dtype=np.int64))
        binding.bind_cpu_input("new_pos", np.array([[n_voice + 1 + t]], dtype=np.int64))
        binding.bind_ortvalue_input("packed_kv", kv)
        binding.bind_ortvalue_input("past_valid", valid)
        binding.bind_ortvalue_input("cross_kv", cross)
        binding.bind_ortvalue_input("text_valid", text_mask)
        binding.bind_output("hidden", "cpu")
        binding.bind_output("new_packed_kv", "cuda")
        binding.bind_output("full_valid", "cuda")
        session.run_with_iobinding(binding)
        hidden, kv, valid = binding.get_outputs()
        h = hidden.numpy()[:, -1, :]
        t += 1
