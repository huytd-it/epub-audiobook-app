"""Regression tests for the exported Colab/Kaggle notebook templates.

The platform is chosen by ONE manual global flag, IS_KAGGLE, defined in the
first code cell (True = Kaggle, False = Colab). No cell may auto-detect the
platform: Kaggle images ship the google.colab package, so `from google.colab
import drive` SUCCEEDS on Kaggle and drive.mount() then raises
NotImplementedError - `except ImportError` can never tell the two platforms
apart, and per-cell re-detection drifted between cells. Every cell that mounts
Drive must therefore be guarded by the IS_KAGGLE global instead.

The voice reference clip is also mandatory: the manifest-loading cell must stop
the run when the clip is missing instead of silently synthesizing with a random
(inconsistent) voice.
"""
from __future__ import annotations

import json
import re
import tempfile
import ast
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "app" / "assets"
TEMPLATES = [
    ASSETS / "colab_kaggle_batch_tts_template.ipynb",
]


def _code_cells(path: Path) -> list[str]:
    nb = json.loads(path.read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_is_kaggle_is_a_manual_global_set_in_cell_1(template):
    cells = _code_cells(template)
    assert "IS_KAGGLE = False" in cells[0], (
        f"{template.name}: the first code cell must define the global "
        "IS_KAGGLE = True/False flag used by every other cell"
    )
    for src in cells:
        assert 'os.path.isdir("/kaggle")' not in src, (
            f"{template.name}: cells must use the global IS_KAGGLE flag from "
            "Cell 1 instead of auto-detecting the platform per cell"
        )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_drive_mount_never_guarded_by_importerror(template):
    for src in _code_cells(template):
        if "drive.mount(" in src:
            assert "except ImportError" not in src, (
                f"{template.name}: google.colab imports fine on Kaggle, so "
                "except ImportError cannot distinguish Kaggle from Colab"
            )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_drive_mount_cells_guarded_by_is_kaggle(template):
    for src in _code_cells(template):
        if "drive.mount(" not in src:
            continue
        assert "IS_KAGGLE" in src, (
            f"{template.name}: a cell mounting Drive must branch on the "
            "global IS_KAGGLE flag so 'Run all' works on both platforms"
        )
        assert src.index("IS_KAGGLE") < src.index("drive.mount("), (
            f"{template.name}: the IS_KAGGLE check must come BEFORE drive.mount()"
        )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_manifest_cell_requires_reference_wav(template):
    cells = [
        src for src in _code_cells(template)
        if '.get("reference_wav")' in src
    ]
    assert cells, f"{template.name}: no manifest-loading cell found"
    for src in cells:
        assert "raise" in src, (
            f"{template.name}: the manifest cell must raise when the voice "
            "reference clip is missing (it is mandatory for consistent audio)"
        )


def test_batch_cell_8_is_deterministic_and_streams_atomic_merge():
    src = _code_cells(TEMPLATES[0])[7]
    assert "torch.manual_seed(42)" in src
    assert "seed=42" not in src
    assert "cfg_value=2.0" in src
    assert "inference_timesteps=10" in src
    assert "_CHUNK_PAUSE_MS = 300" in src
    assert "sf.SoundFile" in src
    assert "PCM_16" in src
    assert "NamedTemporaryFile" in src or "tempfile" in src
    assert "os.replace" in src
    assert "np.concatenate" not in src


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_voxcpm_seed_is_global_and_set_immediately_before_generate(template):
    generation_cells = [src for src in _code_cells(template) if "model.generate(" in src]
    assert generation_cells
    for src in generation_cells:
        assert "torch.manual_seed(42)" in src
        assert "seed=42" not in src
        assert src.index("torch.manual_seed(42)") < src.index("model.generate(")


def test_batch_cell_8_validates_metadata_and_writes_version_1_timeline():
    src = _code_cells(TEMPLATES[0])[7]
    for token in (
        "chunk_metadata", "chapter_index", "chapter_title", "is_chapter_start",
        "timeline", '"version": 1', "flush", "fsync", "warning",
    ):
        assert token in src
    assert "actual frame" in src.lower() or "frames" in src.lower()
    assert "drive_persist" in src
    assert '"total_frames"' in src
    assert '"title"' in src
    assert '"start_seconds"' in src
    assert "validate_chunk_metadata" in src
    assert "merge_wav_files" in src


def test_batch_cell_8_preserves_legacy_fallback_and_skip_warning():
    src = _code_cells(TEMPLATES[0])[7]
    assert 'manifest.get("chunk_metadata")' in src
    assert "SKIP_EXISTING" in src
    assert "delete result and rerun" in src.lower()
    assert "chunk_metadata missing" in src.lower()
    assert "finally" in src


def _cell8_helpers():
    src = _code_cells(TEMPLATES[0])[7]
    match = re.search(r"^# BEGIN CELL 8 HELPERS$(.*?)^# END CELL 8 HELPERS$", src, re.M | re.S)
    assert match, "Cell 8 helper block missing"
    namespace = {}
    exec(match.group(1), namespace)
    return namespace


def test_cell8_helpers_merge_mono_and_stereo_with_exact_pause_and_timeline():
    import numpy as np
    import soundfile as sf

    helpers = _cell8_helpers()
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for channels in (1, 2):
            paths.clear()
            for index, value in enumerate((1000, 2000)):
                path = Path(directory) / f"{channels}-{index}.wav"
                data = np.full((3, channels), value, dtype=np.int16) if channels == 2 else np.full(3, value, dtype=np.int16)
                sf.write(path, data, 10, subtype="PCM_16")
                paths.append(str(path))
            result = Path(directory) / f"result-{channels}.wav"
            timeline = helpers["merge_wav_files"](paths, str(result), 300, [{"filename": Path(p).name, "chapter_index": i, "chapter_title": f"C{i}", "is_chapter_start": True, "text": "hello"} for i, p in enumerate(paths)])
            audio, rate = sf.read(result, dtype="int16", always_2d=(channels == 2))
            assert rate == 10 and len(audio) == 9
            assert np.all(audio[3:6] == 0)
            assert timeline["total_frames"] == 9
            assert set(timeline) == {"version", "sample_rate", "total_frames", "chapters"}
            assert timeline["chapters"][1]["start_frame"] == 6
            assert timeline["chapters"][1]["start_seconds"] == 0.6


def test_cell8_helper_rejects_invalid_metadata_without_raising():
    validate = _cell8_helpers()["validate_chunk_metadata"]
    cases = [None, {}, True, [None], [{"filename": "x"}], [{"filename": "x", "chapter_index": True, "chapter_title": "", "is_chapter_start": True}]]
    for metadata in cases:
        assert validate(metadata, ["x", "y"]) is None


def test_cell8_merge_format_failures_preserve_result_and_clean_temp():
    import numpy as np
    import soundfile as sf

    merge = _cell8_helpers()["merge_wav_files"]
    with tempfile.TemporaryDirectory() as directory:
        result = Path(directory) / "result.wav"
        result.write_bytes(b"old-result")
        first = Path(directory) / "first.wav"
        second = Path(directory) / "second.wav"
        sf.write(first, np.ones(3, dtype=np.int16), 10, subtype="PCM_16")
        sf.write(second, np.ones((3, 2), dtype=np.int16), 10, subtype="PCM_16")
        for paths in ((first, second),):
            with pytest.raises(RuntimeError, match="sample rates/channels"):
                merge([str(path) for path in paths], str(result), 300)
        assert result.read_bytes() == b"old-result"
        assert not list(Path(directory).glob(".result-*.wav"))


def test_cell8_merge_sample_rate_and_corrupt_failures_preserve_result():
    import numpy as np
    import soundfile as sf

    merge = _cell8_helpers()["merge_wav_files"]
    with tempfile.TemporaryDirectory() as directory:
        result = Path(directory) / "result.wav"
        result.write_bytes(b"old-result")
        first = Path(directory) / "first.wav"
        second = Path(directory) / "second.wav"
        sf.write(first, np.ones(3, dtype=np.int16), 10, subtype="PCM_16")
        sf.write(second, np.ones(3, dtype=np.int16), 20, subtype="PCM_16")
        with pytest.raises(RuntimeError, match="sample rates/channels"):
            merge([str(first), str(second)], str(result), 300)
        corrupt = Path(directory) / "corrupt.wav"
        corrupt.write_bytes(b"not-a-wav")
        with pytest.raises(RuntimeError, match="preflight"):
            merge([str(first), str(corrupt)], str(result), 300)
        assert result.read_bytes() == b"old-result"
        assert not list(Path(directory).glob(".result-*.wav"))


def test_cell8_merge_failure_after_temp_creation_preserves_result_and_cleans_temp(monkeypatch):
    import numpy as np
    import soundfile as sf

    helpers = _cell8_helpers()
    merge = helpers["merge_wav_files"]
    with tempfile.TemporaryDirectory() as directory:
        result = Path(directory) / "result.wav"
        result.write_bytes(b"old-result")
        first = Path(directory) / "first.wav"
        sf.write(first, np.ones(3, dtype=np.int16), 10, subtype="PCM_16")
        original_replace = helpers["os"].replace
        monkeypatch.setattr(helpers["os"], "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
        try:
            with pytest.raises(OSError, match="replace failed"):
                merge([str(first)], str(result), 300)
        finally:
            helpers["os"].replace = original_replace
        assert result.read_bytes() == b"old-result"
        assert not list(Path(directory).glob(".result-*.wav"))


def test_cell8_sidecar_atomic_write_preserves_old_file_on_replace_failure():
    import json

    helpers = _cell8_helpers()
    assert "write_timeline_atomic" in helpers
    with tempfile.TemporaryDirectory() as directory:
        sidecar = Path(directory) / "result.timeline.json"
        sidecar.write_text('{"old": true}', encoding="utf-8")
        original_replace = helpers["os"].replace
        helpers["os"].replace = lambda *_args: (_ for _ in ()).throw(OSError("replace failed"))
        try:
            with pytest.raises(OSError):
                helpers["write_timeline_atomic"]({"version": 1}, str(sidecar))
        finally:
            helpers["os"].replace = original_replace
        assert json.loads(sidecar.read_text(encoding="utf-8")) == {"old": True}
        assert not list(Path(directory).glob(".timeline-*.json"))


@pytest.mark.parametrize("failure", ["dump", "fsync"])
def test_cell8_sidecar_pre_replace_failures_preserve_old_file_and_cleanup(monkeypatch, failure):
    helpers = _cell8_helpers()
    with tempfile.TemporaryDirectory() as directory:
        sidecar = Path(directory) / "result.timeline.json"
        sidecar.write_text('{"old": true}', encoding="utf-8")
        if failure == "dump":
            monkeypatch.setattr(helpers["json"], "dump", lambda *_args: (_ for _ in ()).throw(OSError("dump failed")))
        else:
            monkeypatch.setattr(helpers["os"], "fsync", lambda *_args: (_ for _ in ()).throw(OSError("fsync failed")))
        with pytest.raises(OSError):
            helpers["write_timeline_atomic"]({"version": 1}, str(sidecar))
        assert sidecar.read_text(encoding="utf-8") == '{"old": true}'
        assert not list(Path(directory).glob(".timeline-*.json"))


def test_cell8_validator_requires_non_empty_text():
    helpers = _cell8_helpers()
    validate = helpers["validate_chunk_metadata"]
    chunks = ["chunk_000.txt", "chunk_001.txt"]

    def entry(offset, **overrides):
        item = {
            "filename": chunks[offset],
            "chapter_index": 0,
            "chapter_title": "One",
            "is_chapter_start": offset == 0,
            "text": "hello",
        }
        item.update(overrides)
        return item

    assert validate([entry(0), entry(1)], chunks) is not None
    # four-field legacy metadata is no longer valid
    legacy = entry(0)
    del legacy["text"]
    assert validate([legacy, entry(1)], chunks) is None
    assert validate([entry(0, text=""), entry(1)], chunks) is None
    assert validate([entry(0, text="   "), entry(1)], chunks) is None
    assert validate([entry(0, text=123), entry(1)], chunks) is None


def test_cell8_normalize_chunk_manifest_rebuilds_derivable_fields():
    helpers = _cell8_helpers()
    normalize = helpers["normalize_chunk_manifest"]

    compact = {
        "chunk_count": 3,
        "chapter_titles": {"1": "One", "2": "Two"},
        "chunk_metadata": [
            {"chapter_index": 1, "is_chapter_start": True, "text": "a"},
            {"chapter_index": 1, "is_chapter_start": False, "text": "b"},
            {"chapter_index": 2, "is_chapter_start": True, "text": "c"},
        ],
    }
    assert normalize(compact) is True
    assert compact["chunks"] == ["chunk_000.txt", "chunk_001.txt", "chunk_002.txt"]
    assert compact["expected_outputs"] == ["chunk_000.wav", "chunk_001.wav", "chunk_002.wav"]
    assert compact["chunk_metadata"] == [
        {"chapter_index": 1, "is_chapter_start": True, "text": "a", "filename": "chunk_000.txt", "chapter_title": "One"},
        {"chapter_index": 1, "is_chapter_start": False, "text": "b", "filename": "chunk_001.txt", "chapter_title": "One"},
        {"chapter_index": 2, "is_chapter_start": True, "text": "c", "filename": "chunk_002.txt", "chapter_title": "Two"},
    ]

    # Legacy manifests (derivable fields already present) pass through untouched.
    legacy = {
        "chunk_count": 1,
        "chunks": ["chunk_000.txt"],
        "expected_outputs": ["chunk_000.wav"],
        "chunk_metadata": [
            {"filename": "chunk_000.txt", "chapter_index": 1, "chapter_title": "One", "is_chapter_start": True, "text": "a"},
        ],
    }
    before = json.dumps(legacy, sort_keys=True)
    assert normalize(legacy) is True
    assert json.dumps(legacy, sort_keys=True) == before


@pytest.mark.parametrize("manifest", [
    {}, {"chunk_count": 0}, {"chunk_count": "3"}, {"chunk_count": True}, {"chunk_count": None},
])
def test_cell8_normalize_chunk_manifest_reports_unusable_manifests(manifest):
    """A junk chunk_count must fail this one patch, not raise out of the batch loop -
    every other bad-manifest case in Cell 8 is contained the same way."""
    assert _cell8_helpers()["normalize_chunk_manifest"](manifest) is False


def test_cell8_chunk_text_prefers_manifest_and_falls_back_to_txt(tmp_path):
    helpers = _cell8_helpers()
    chunk_text_for = helpers["chunk_text_for"]

    inlined = {
        "chunks": ["chunk_000.txt"],
        "chunk_metadata": [{"filename": "chunk_000.txt", "text": "from manifest"}],
    }
    assert chunk_text_for(inlined, 0, str(tmp_path)) == "from manifest"

    (tmp_path / "chunk_000.txt").write_text("from disk", encoding="utf-8")
    legacy = {"chunks": ["chunk_000.txt"], "chunk_metadata": [{"filename": "chunk_000.txt"}]}
    assert chunk_text_for(legacy, 0, str(tmp_path)) == "from disk"
    assert chunk_text_for({"chunks": ["chunk_000.txt"]}, 0, str(tmp_path)) == "from disk"


def test_cell8_available_wavs_merges_local_dirs_and_remote_inventory(tmp_path):
    helpers = _cell8_helpers()
    available_wavs = helpers["available_wavs"]

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "chunk_000.wav").write_bytes(b"")
    (out_dir / "notes.txt").write_bytes(b"")

    remote = {
        "patches/patch_000/output/chunk_001.wav": "id1",
        "patches/patch_000/output/nested/chunk_009.wav": "id9",
        "patches/patch_001/output/chunk_002.wav": "id2",
        "result/000 - a.wav": "idr",
    }
    names = available_wavs(
        [str(out_dir), str(tmp_path / "missing")], remote, "patches/patch_000/output"
    )
    assert names == {"chunk_000.wav", "chunk_001.wav"}


def test_batch_cell_8_uses_remote_inventory_and_lazy_fetch():
    src = _code_cells(TEMPLATES[0])[7]
    assert "_drive_file_ids" in src
    assert "drive_fetch_many" in src
    assert 'entry["result_wav"] in REMOTE' in src
    assert "available_wavs(" in src
    assert "chunk_text_for(" in src
    assert "find_wav" not in src
    assert "os.listdir" in src


def test_cell8_persist_failures_are_caught_separately():
    src = _code_cells(TEMPLATES[0])[7]
    assert "Result persistence failed" in src
    assert "Timeline persistence failed after local install" in src
    assert "try: persist(result_path, \"result\")" in src
    tree = ast.parse(src)
    persist_try_bodies = [
        node.body for node in ast.walk(tree)
        if isinstance(node, ast.Try) and any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "persist"
            for call in ast.walk(node)
        )
    ]
    assert len(persist_try_bodies) >= 2


def _cell4_helpers():
    src = _code_cells(TEMPLATES[0])[3]
    match = re.search(r"^# BEGIN CELL 4 HELPERS$(.*?)^# END CELL 4 HELPERS$", src, re.M | re.S)
    assert match, "Cell 4 helper block missing"
    namespace = {}
    exec(match.group(1), namespace)
    return namespace


def _fake_batch_inventory(patch_count=3, merged=0, chunks_per_patch=2):
    """Remote inventory for a batch where the first `merged` patches are finished."""
    manifest = {
        "reference_wav": "reference.wav",
        "patches": [
            {
                "patch_id": i,
                "patch_index": i,
                "folder": f"patches/patch_{i:03d}",
                "result_wav": f"result/{i:03d} - p.wav",
            }
            for i in range(patch_count)
        ],
    }
    # Current packages ship neither music nor backgrounds; batches exported before
    # that still have them on Drive, and the planner must keep ignoring them.
    remote = {"batch_manifest.json": "id", "reference.wav": "id", "music/bg.mp3": "id"}
    for i, entry in enumerate(manifest["patches"]):
        remote[f"{entry['folder']}/manifest.json"] = "id"
        remote[f"{entry['folder']}/background.jpg"] = "id"
        if i < merged:
            remote[entry["result_wav"]] = "id"
            for c in range(chunks_per_patch):
                remote[f"{entry['folder']}/output/chunk_{c:03d}.wav"] = "id"
    return manifest, remote


def test_cell4_plan_downloads_only_manifests_and_reference():
    plan_batch_downloads = _cell4_helpers()["plan_batch_downloads"]
    manifest, remote = _fake_batch_inventory(patch_count=3, merged=3)

    planned = plan_batch_downloads(manifest, remote)

    assert set(planned) == {
        "batch_manifest.json",
        "reference.wav",
        "patches/patch_000/manifest.json",
        "patches/patch_001/manifest.json",
        "patches/patch_002/manifest.json",
    }
    # A fully merged batch downloads no chunk WAV, no result, no background, no music.
    for rel in planned:
        assert "/output/" not in rel
        assert not rel.startswith("result/")
        assert not rel.startswith("music/")
        assert "background" not in rel


def test_cell4_plan_skips_paths_absent_from_the_inventory():
    plan_batch_downloads = _cell4_helpers()["plan_batch_downloads"]
    manifest, remote = _fake_batch_inventory(patch_count=2, merged=0)
    del remote["patches/patch_001/manifest.json"]
    del remote["reference.wav"]

    planned = plan_batch_downloads(manifest, remote)

    assert planned == ["batch_manifest.json", "patches/patch_000/manifest.json"]


def test_cell4_lists_before_downloading_and_is_thread_safe():
    src = _code_cells(TEMPLATES[0])[3]
    assert "plan_batch_downloads(" in src
    assert "ThreadPoolExecutor" in src
    assert "threading.local()" in src
    assert "def drive_fetch_many(" in src
    assert "def drive_persist(" in src
    # the old walk that downloaded while listing must be gone
    assert "def _sync_down(" not in src


def test_batch_notebook_has_no_result_zip_cell():
    nb = json.loads(TEMPLATES[0].read_text(encoding="utf-8"))
    assert len(nb["cells"]) == 9
    for cell in nb["cells"]:
        src = "".join(cell["source"])
        assert "make_archive" not in src
        assert "results.zip" not in src
        assert "Cell 9" not in src
    # Cell 8 must still be the eighth code cell for the other tests in this file
    assert "_CHUNK_PAUSE_MS = 300" in _code_cells(TEMPLATES[0])[7]


# ---------------------------------------------------------------------------
# Generic 4-model TTS dispatch. The runtime reads the model from the manifest's
# "tts" contract ({"model_id", "options", "voice_id"}) with a legacy fallback to
# the always-VoxCPM2 "voxcpm_model_id" field. Online models (edge-tts/gTTS) are
# intentionally unsupported here: the batch notebook only runs offline weights.
# ---------------------------------------------------------------------------

SUPPORTED_MODELS = {"voxcpm2", "omnivoice", "vieneu-fast", "zerotts"}


def _manifest_cell(template):
    """The code cell that loads the manifest and resolves the TTS model."""
    cells = _code_cells(template)
    for src in cells:
        # Batch runtime reads the selected model from batch_manifest.get("tts").
        if "get(\"tts\")" in src or "get('tts')" in src:
            return src
    raise AssertionError(f"{template.name}: no manifest cell resolves the tts contract")


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_manifest_cell_reads_generic_tts_contract_with_legacy_fallback(template):
    src = _manifest_cell(template)
    assert 'get("model_id")' in src
    assert "TTS_MODEL" in src
    # generic field wins; legacy voxcpm_model_id is the explicit fallback
    assert "voxcpm_model_id" in src
    assert "TTS_OPTIONS" in src
    assert "VOICE_ID" in src
    assert "SAMPLE_RATE" in src


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_sample_rate_constants_per_model(template):
    src = _manifest_cell(template)
    for k, v in (
        ("voxcpm2", 48000),
        ("omnivoice", 24000),
        ("vieneu-fast", 48000),
        ("zerotts", 48000),
    ):
        assert f'"{k}": {v}' in src


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_reference_required_only_for_cloning_models(template):
    src = _manifest_cell(template)
    # vieneu-fast can clone, but the app drives it with a named preset instead, so
    # a batch must not demand a reference clip for it.
    assert 'REFERENCE_MODELS = {"voxcpm2", "omnivoice"}' in src
    assert "REFERENCE_REQUIRED" in src
    assert "if REFERENCE_REQUIRED:" in src
    assert "raise RuntimeError" in src
    # the raise is gated on REFERENCE_REQUIRED, not unconditional
    assert "if not reference_wav_path" in src


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_voice_id_required_for_voice_id_models(template):
    src = _manifest_cell(template)
    assert 'VOICE_ID_MODELS = {"vieneu-fast", "zerotts"}' in src
    assert "if TTS_MODEL in VOICE_ID_MODELS:" in src
    assert "VOICE_ID" in src
    assert "raise RuntimeError" in src


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_gpu_check_rejects_unknown_models(template):
    gpu = next(src for src in _code_cells(template) if "torch.cuda.is_available()" in src)
    assert 'GPU_REQUIRED = {"voxcpm2", "omnivoice"}' in gpu
    assert 'GPU_OPTIONAL = {"vieneu-fast", "zerotts"}' in gpu
    assert "if TTS_MODEL in GPU_REQUIRED:" in gpu
    assert "elif TTS_MODEL in GPU_OPTIONAL:" in gpu
    # Every supported model takes the GPU_REQUIRED / GPU_OPTIONAL path; anything
    # else is an unknown model id and must raise, never silently continue.
    assert "else:" in gpu
    assert "raise RuntimeError" in gpu.split("else:")[-1]
    assert "edge-tts" not in gpu
    assert "gtts" not in gpu
    # The CPU-capable models must reach a print, never the raise.
    required_branch = gpu.split("elif TTS_MODEL in GPU_OPTIONAL:")
    assert len(required_branch) == 2, "GPU_OPTIONAL needs its own branch"
    assert "raise RuntimeError" not in required_branch[1].split("else:")[0]


def test_model_load_cell_dispatches_per_model():
    for template in TEMPLATES:
        load = next(src for src in _code_cells(template) if "VoxCPM.from_pretrained" in src)
        assert "OmniVoice.from_pretrained" in load
        assert "Vieneu(" in load
        assert "ZeroTTS.from_pretrained" in load
        # VieNeu ships several modes; only v3turbo works on the base install.
        assert 'mode="v3turbo"' in load
        assert "pnnbao-ump/VieNeu-TTS-v3-Turbo" in load
        assert "zeroweight-ai/ZeroTTS" in load
        # Online models are unsupported in the batch notebook: no model=None
        # shortcut and no online references in the load cell.
        assert "model = None" not in load
        assert "edge-tts" not in load
        assert "gtts" not in load


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_generation_cells_dispatch_every_model(template):
    gen = "\n".join(
        src for src in _code_cells(template)
        if "model.generate(" in src
    )
    for model in SUPPORTED_MODELS:
        assert f'"{model}"' in gen
    assert "edge-tts" not in gen
    assert "gtts" not in gen
    assert "save_online_mp3" not in gen
    assert "model.generate(" in gen  # voxcpm2 / omnivoice
    assert "model.infer(" in gen     # vieneu-fast
    assert "model.synthesize(" in gen  # zerotts
    # v3 Turbo ignores the style argument, so it must not be passed any more.
    assert "style=TTS_OPTIONS" not in gen
    # infer() resolves ref_audio before voice, so a clip would override the preset
    # the app picked. vieneu's branch must pass the voice and no clip - omnivoice
    # legitimately still uses ref_audio, so scope this to vieneu and ignore the
    # comment lines that mention ref_audio to explain why it is absent.
    assert "model.infer(text, voice=VOICE_ID)" in gen
    branch = gen.split('elif TTS_MODEL == "vieneu-fast":')[1].split("elif TTS_MODEL ==")[0]
    code = [l for l in branch.splitlines() if not l.lstrip().startswith("#")]
    assert not any("ref_audio" in l for l in code), f"vieneu branch passes a clip: {code}"
    # OmniVoice normalizes its list output to audio[0]
    assert "result[0]" in gen or "audio[0]" in gen


def test_batch_notebook_has_no_online_path():
    for template in TEMPLATES:
        src = "\n".join(_code_cells(template))
        assert "def save_online_mp3(" not in src
        assert "def mp3_to_wav(" not in src
        assert "edge_tts.Communicate(" not in src
        assert "gTTS(" not in src
        assert "edge-tts" not in src
        assert "gtts" not in src


def test_generation_branches_use_the_manifest_voice():
    # The fixed-cast models synthesize from the manifest's voice id.
    for template in TEMPLATES:
        generation = [
            src for src in _code_cells(template)
            if "model.generate(" in src
        ]
        assert generation
        assert "VOICE_ID" in "\n".join(generation)
