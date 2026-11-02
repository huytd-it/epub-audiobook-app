import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
ort = pytest.importorskip("onnxruntime")
from onnx import TensorProto, helper, numpy_helper  # noqa: E402

from app import zerotts_gpu  # noqa: E402


def _sampling_graph(model_dir):
    """Two inverse-CDF samplers shaped like ZeroTTS's local_frame_decode (N=2 and N=1024)."""
    nodes, inits, outputs = [], [numpy_helper.from_array(np.array([-1], dtype=np.int64), "axis")], []
    for i, n in enumerate((2, 1024)):
        nodes += [
            helper.make_node("Softmax", [f"logits{i}"], [f"p{i}"], axis=-1),
            helper.make_node("CumSum", [f"p{i}", "axis"], [f"c{i}"]),
            helper.make_node("Greater", [f"c{i}", "u"], [f"g{i}"]),
            helper.make_node("Cast", [f"g{i}"], [f"gi{i}"], to=TensorProto.INT64),
            helper.make_node("ArgMax", [f"gi{i}"], [f"idx{i}"], axis=-1, keepdims=0, name=f"/ArgMax_{i}"),
        ]
        outputs.append(helper.make_tensor_value_info(f"idx{i}", TensorProto.INT64, ["B"]))
    graph = helper.make_graph(
        nodes, "sampler",
        [helper.make_tensor_value_info("logits0", TensorProto.FLOAT, ["B", 2]),
         helper.make_tensor_value_info("logits1", TensorProto.FLOAT, ["B", 1024]),
         helper.make_tensor_value_info("u", TensorProto.FLOAT, ["B", 1])],
        outputs, inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    (model_dir / "onnx").mkdir(parents=True)
    onnx.save(model, str(model_dir / "onnx" / "local_frame_decode.onnx"))


def test_rewrite_matches_argmax_sampling_exactly(tmp_path):
    _sampling_graph(tmp_path)
    built = zerotts_gpu.build_frame_decoder(tmp_path)
    assert "ArgMax" not in {n.op_type for n in onnx.load(str(built)).graph.node}
    original = ort.InferenceSession(str(tmp_path / "onnx" / "local_frame_decode.onnx"), providers=["CPUExecutionProvider"])
    rewritten = ort.InferenceSession(str(built), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(0)
    # u >= 1 can leave every cumsum <= u: ArgMax of all-zero returns 0, so must the rewrite.
    for u in [*rng.random(200), 0.0, 0.999999, 1.0, 1.5]:
        feeds = {"logits0": rng.normal(size=(1, 2)).astype(np.float32) * 3,
                 "logits1": rng.normal(size=(1, 1024)).astype(np.float32) * 3,
                 "u": np.array([[u]], dtype=np.float32)}
        for a, b in zip(original.run(None, feeds), rewritten.run(None, feeds)):
            assert a.dtype == b.dtype == np.int64 and np.array_equal(a, b)


def test_build_is_cached_until_the_source_changes(tmp_path):
    _sampling_graph(tmp_path)
    first = zerotts_gpu.build_frame_decoder(tmp_path)
    stamp = first.stat().st_mtime_ns
    assert zerotts_gpu.built_graph(tmp_path) == first
    assert zerotts_gpu.build_frame_decoder(tmp_path).stat().st_mtime_ns == stamp
    # Only the result is left behind (plus its .data on real, large weights).
    assert {p.name for p in zerotts_gpu.gpu_dir(tmp_path).iterdir()} - {"local_frame_decode.data"} == {
        "build.json", "local_frame_decode.onnx"}
    # New weights (e.g. "Cập nhật model") invalidate the cached build.
    src = tmp_path / "onnx" / "local_frame_decode.onnx"
    src.write_bytes(src.read_bytes() + b"\0")
    assert zerotts_gpu.built_graph(tmp_path) is None


def test_unexpected_argmax_is_refused(tmp_path):
    graph = helper.make_graph(
        [helper.make_node("ArgMax", ["x"], ["y"], axis=-1, keepdims=0)], "g",
        [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 4])],
        [helper.make_tensor_value_info("y", TensorProto.INT64, [1])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 8
    (tmp_path / "onnx").mkdir()
    onnx.save(model, str(tmp_path / "onnx" / "local_frame_decode.onnx"))
    with pytest.raises(RuntimeError, match="sampling"):
        zerotts_gpu.build_frame_decoder(tmp_path)


def test_accelerate_leaves_cpu_sessions_alone(tmp_path):
    class Session:
        def get_providers(self):
            return ["CPUExecutionProvider"]

    model = type("M", (), {"prefix_step_sess": Session(), "local_frame_decode_sess": Session()})()
    before = model.local_frame_decode_sess
    assert zerotts_gpu.accelerate(model, tmp_path) is False
    assert model.local_frame_decode_sess is before
