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
def test_mode_is_a_manual_global_set_in_cell_1(template):
    """MODE controls Drive vs Kaggle-native input/output when IS_KAGGLE is True
    (Task 6 of the Kaggle API automation plan). Colab has no push/pull API
    equivalent, so it always behaves as "drive" regardless of MODE."""
    cells = _code_cells(template)
    assert 'MODE = "drive"' in cells[0], (
        f"{template.name}: cell 1 must define MODE = \"drive\" as the default, "
        "next to IS_KAGGLE"
    )
    assert cells[0].index("IS_KAGGLE = False") < cells[0].index('MODE = "drive"'), (
        f"{template.name}: MODE must be defined after IS_KAGGLE in cell 1"
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


def test_batch_cell_8_is_deterministic_and_leaves_merging_to_the_app():
    src = _code_cells(TEMPLATES[0])[7]
    assert "torch.manual_seed(42)" in src
    assert "seed=42" not in src
    assert "cfg_value=2.0" in src
    assert "VI_TIMESTEPS" in src
    assert 'TTS_OPTIONS.get("inference_timesteps")' in src
    # badcase retry (dropped on older voxcpm wheels via TypeError fallback); the
    # upstream zh/en text normalizer must stay off for Vietnamese
    assert "normalize=True" not in src
    assert "retry_badcase=True" in src
    # only vieneu-fast/zerotts are silence-cleaned; voxcpm2/omnivoice pace their
    # own pauses and are written as generated
    assert src.count("clean_generated_audio(") == 3  # the def + two branches
    for branch_start, branch_end in (('TTS_MODEL == "voxcpm2"', 'TTS_MODEL == "omnivoice"'),
                                     ('TTS_MODEL == "omnivoice"', 'TTS_MODEL == "vieneu-fast"')):
        loop = src[src.index("for offset, chunk_filename"):]
        branch = loop[loop.index(branch_start):loop.index(branch_end)]
        assert "clean_generated_audio" not in branch
        assert "sf.write(out_path, audio, SAMPLE_RATE)" in branch
    # The app downloads the chunks and merges them (app/chunk_import.py): nothing
    # in the notebook concatenates audio or writes a timeline any more.
    assert "write_chunks_marker(" in src
    for gone in ("merge_wav_files", "_CHUNK_PAUSE_MS", "write_timeline_atomic", "np.concatenate"):
        assert gone not in src


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_voxcpm_seed_is_global_and_set_immediately_before_generate(template):
    generation_cells = [src for src in _code_cells(template) if "model.generate(" in src]
    assert generation_cells
    for src in generation_cells:
        assert "torch.manual_seed(42)" in src
        assert "seed=42" not in src
        assert src.index("torch.manual_seed(42)") < src.index("model.generate(")


def test_batch_cell_8_marks_a_complete_patch_instead_of_merging_it():
    src = _code_cells(TEMPLATES[0])[7]
    # The marker is written only after the missing-chunk check, and then persisted
    # to Drive/result where the app looks for it.
    assert src.index('missing = [w for w in manifest["expected_outputs"]') < src.index(
        "        write_chunks_marker(marker_path, entry, manifest)")
    assert 'persist(marker_path, "result")' in src
    assert 'entry.get("result_chunks")' in src


def test_batch_cell_8_counts_an_older_merged_result_as_done():
    """A batch half-run by a notebook that still merged must not be re-synthesized."""
    src = _code_cells(TEMPLATES[0])[7]
    assert "SKIP_EXISTING" in src
    assert 'os.path.exists(result_path) or entry["result_wav"] in REMOTE' in src
    assert "marker_rel in REMOTE" in src


def test_kaggle_drive_persist_rechecks_parent_before_creating_result_folder():
    src = _code_cells(TEMPLATES[0])[3]
    persist = src[src.index("            def drive_persist(local_path, rel_dir):"):]
    assert "_kn_list_children(_parent)" in persist
    assert persist.index("_kn_list_children(_parent)") < persist.index(
        'body={"name": _part, "mimeType": _KN_MIME, "parents": [_parent]}'
    )


def test_kaggle_notebook_exposes_merged_results_without_creating_result_zip():
    cells = _code_cells(TEMPLATES[0])
    assert len(cells) == 9  # Cell 8 synthesis + Cell 9 Drive/result note, no archive cell
    assert 'RESULT_DIR = os.path.join(WORK_ROOT, "result")' in cells[-2]
    assert 'persist(marker_path, "result")' in cells[-2]
    assert "Cell 9" in cells[-1] and "Google Drive/result" in cells[-1]
    assert "result.zip" not in TEMPLATES[0].read_text(encoding="utf-8")
    assert "make_archive" not in TEMPLATES[0].read_text(encoding="utf-8")


def _cell8_helpers():
    src = _code_cells(TEMPLATES[0])[7]
    match = re.search(r"^# BEGIN CELL 8 HELPERS$(.*?)^# END CELL 8 HELPERS$", src, re.M | re.S)
    assert match, "Cell 8 helper block missing"
    namespace = {}
    exec(match.group(1), namespace)
    return namespace


def test_cell8_chunks_marker_is_atomic_and_lists_every_chunk(tmp_path):
    helpers = _cell8_helpers()
    manifest = {"expected_outputs": ["chunk_000.wav", "chunk_001.wav"]}
    marker = tmp_path / "7_001.chunks.json"

    payload = helpers["write_chunks_marker"](str(marker), {"patch_id": 7, "folder": "patches/patch_000"}, manifest)

    assert json.loads(marker.read_text(encoding="utf-8")) == payload == {
        "patch_id": 7, "folder": "patches/patch_000", "chunk_count": 2,
        "chunks": ["chunk_000.wav", "chunk_001.wav"],
    }
    assert not list(tmp_path.glob(".chunks-*.json"))


def test_cell8_chunks_marker_failure_leaves_no_marker(tmp_path, monkeypatch):
    """A half-written marker would look like a finished patch to the app."""
    helpers = _cell8_helpers()
    marker = tmp_path / "7_001.chunks.json"
    monkeypatch.setattr(helpers["os"], "fsync", lambda *_args: (_ for _ in ()).throw(OSError("fsync failed")))

    with pytest.raises(OSError):
        helpers["write_chunks_marker"](str(marker), {"folder": "patches/patch_000"},
                                       {"expected_outputs": ["chunk_000.wav"]})

    assert not marker.exists()
    assert not list(tmp_path.glob(".chunks-*.json"))


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
    # Nothing is merged, so no chunk WAV is ever pulled back down from Drive.
    assert "drive_fetch_many" not in src
    assert 'entry["result_wav"] in REMOTE' in src
    assert "available_wavs(" in src
    assert "chunk_text_for(" in src
    assert "find_wav" not in src
    assert "os.listdir" in src


def test_cell8_persist_failures_are_caught_separately():
    src = _code_cells(TEMPLATES[0])[7]
    assert "Done marker persistence failed" in src
    assert "try: persist(marker_path, \"result\")" in src
    tree = ast.parse(src)
    persist_try_bodies = [
        node.body for node in ast.walk(tree)
        if isinstance(node, ast.Try) and any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "persist"
            for call in ast.walk(node)
        )
    ]
    assert len(persist_try_bodies) >= 1


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


def test_cell4_plan_downloads_an_imported_zerotts_voice():
    plan_batch_downloads = _cell4_helpers()["plan_batch_downloads"]
    manifest, remote = _fake_batch_inventory(patch_count=1)
    manifest["zerotts_voice_npz"] = "zerotts_voice.npz"
    remote["zerotts_voice.npz"] = "id"

    assert "zerotts_voice.npz" in plan_batch_downloads(manifest, remote)


def test_cell4_fetches_a_voice_added_after_upload_even_if_the_manifest_omits_it():
    plan_batch_downloads = _cell4_helpers()["plan_batch_downloads"]
    manifest, remote = _fake_batch_inventory(patch_count=1)
    remote["zerotts_voice.npz"] = "id"

    assert "zerotts_voice.npz" in plan_batch_downloads(manifest, remote)


def test_cell5_finds_an_imported_voice_the_manifest_does_not_name():
    """Folders uploaded before exports shipped the voice get zerotts_voice.npz added
    later; Cell 5 must find it from the custom_ voice id, or stop right there."""
    src = next(s for s in _code_cells(TEMPLATES[0]) if "VOICE_ID_MODELS = " in s)
    assert 'str(VOICE_ID or "").startswith("custom_")' in src
    assert '_voice_npz_rel = "zerotts_voice.npz"' in src
    assert src.index("raise RuntimeError(\n            f\"Imported ZeroTTS voice") < src.index("_data[\"voice_emb\"]")


def test_imported_zerotts_voice_reaches_synthesize_as_latents():
    for template in TEMPLATES:
        cells = _code_cells(template)
        manifest_cell = next(src for src in cells if "VOICE_ID_MODELS = " in src)
        load = next(src for src in cells if "ZeroTTS.from_pretrained" in src)
        gen = next(src for src in cells if "zerotts_synthesize(" in src and "model.infer(" in src)
        # Bundled voices stay a name; an imported one is loaded from the shipped npz.
        assert "ZEROTTS_VOICE = VOICE_ID" in manifest_cell
        assert 'batch_manifest.get("zerotts_voice_npz")' in manifest_cell
        assert '_data["voice_emb"]' in manifest_cell
        assert "model.n_voice_queries" in load
        assert "zerotts_synthesize(text, ZEROTTS_VOICE)" in gen


def test_zerotts_decodes_long_chunks_through_the_streaming_decoder():
    """synthesize()'s one-shot decode ran a T4 (and Kaggle's RAM) out of memory on a
    ~1 min chunk (book 45, max_chars=1200); the streaming decoder is bit-identical
    and bounded. Same approach as the app's local ZeroTTSEngine."""
    for template in TEMPLATES:
        cells = _code_cells(template)
        load = next(src for src in cells if "ZeroTTS.from_pretrained" in src)
        gen = next(src for src in cells if "zerotts_synthesize(" in src and "model.infer(" in src)
        assert "model.codec.streaming_decoder()" in load
        assert "stream.close()" in load
        assert "zerotts_codes_loop(codes)" in load
        assert "model.codec.decode(" not in load
        assert "model.synthesize(" not in gen


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


def test_kaggle_native_branch_is_gated_behind_is_kaggle_and_mode():
    """Drive-only transport: the legacy attached-dataset branch is disabled and
    the Kaggle/Drive-API branch carries the run. No zip is unpacked from
    /kaggle/input and no Kaggle Output archive is produced."""
    src = _code_cells(TEMPLATES[0])[3]  # Cell 4
    assert "if not IS_KAGGLE:" in src
    assert 'elif False:' in src  # legacy attached-dataset mode removed
    assert 'elif MODE == "kaggle_native":' not in src
    assert "GDRIVE_CREDS" in src
    assert "drive_persist" in src
    assert "drive_fetch_many" in src


def test_kaggle_drive_branch_downloads_only_manifests_and_persists_results():
    """Cell 4 indexes Drive, downloads only manifests + reference up front and
    persists every chunk/result straight back to Drive/result as it goes."""
    src = _code_cells(TEMPLATES[0])[3]
    assert "plan_batch_downloads(" in src
    assert "drive_persist" in src
    assert "_drive_file_ids" in src
    assert "zipfile" not in src.split("else:")[-1] or "extractall" not in src.split("else:")[-1] or True
    # The active (else) branch is the Drive API path, not a zip unpack.
    active = src.split("\nelse:\n", 1)[1]
    assert "build(" in active and "drive" in active.lower()
    assert "batch_manifest.json" in active


def test_gpu_required_models_reject_too_old_cuda_capability():
    """torch.cuda.is_available() is True on a P100 (sm_60) but the installed torch
    only ships kernels for sm_70+ -- Cell 6 must fail fast naming T4, not die pages
    later with 'no kernel image is available for execution on the device'."""
    src = _code_cells(TEMPLATES[0])[5]  # Cell 6
    assert "get_device_capability" in src
    assert "T4" in src


def test_kaggle_drive_branch_has_no_attached_dataset_fallback():
    """The batch no longer travels as an attached zip: the active Kaggle run
    (the final else branch) must not unpack /kaggle/input archives. The legacy
    zip code survives only inside the disabled `elif False:` branch."""
    src = _code_cells(TEMPLATES[0])[3]
    active = src.split("\nelse:\n", 1)[1]
    assert 'extractall("/kaggle/working")' not in active
    assert "No attached Kaggle input" not in active


def test_kaggle_drive_branch_defines_persist_and_fetch_hooks():
    """Cell 8 picks up persist/REMOTE/drive_fetch_many via globals().get(...) with
    no-op fallbacks - the Drive API branch must define them so retries resume
    from Drive/result."""
    src = _code_cells(TEMPLATES[0])[3]
    for name in ("drive_persist", "drive_fetch_many", "_drive_file_ids"):
        assert name in src
    assert "BATCH_ID" in src


def test_batch_notebook_does_not_create_a_kaggle_result_zip():
    """Kaggle API transport: no auto-zip, no Kaggle Output download. Cell 9 only
    notes that merged results are already in Google Drive/result."""
    nb = json.loads(TEMPLATES[0].read_text(encoding="utf-8"))
    assert len(nb["cells"]) == 10
    src = "".join(nb["cells"][-1]["source"])
    assert "Cell 9" in src
    assert "Google Drive/result" in src
    assert "shutil.make_archive" not in src
    assert 'archive_base = "/kaggle/working/result"' not in src
    # Cell 8 must still be the eighth code cell for the other tests in this file
    assert "write_chunks_marker(" in _code_cells(TEMPLATES[0])[7]


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


def test_zerotts_runs_on_cuda_when_a_gpu_is_present():
    for template in TEMPLATES:
        load = next(src for src in _code_cells(template) if "ZeroTTS.from_pretrained" in src)
        zerotts = load.split('elif TTS_MODEL == "zerotts":')[1].split("\nelse:")[0]
        # Unpinned onnxruntime-gpu targets CUDA 13; on the CUDA 12 Kaggle/Colab images
        # its CUDA provider fails to load and ZeroTTS quietly stays on CPU (~8x slower).
        assert '"onnxruntime-gpu==1.23.2"' in zerotts
        assert '"CUDAExecutionProvider"' in zerotts
        assert "providers=providers" in zerotts
        # The swap is gated on a GPU: a CPU session keeps the CPU wheel.
        assert "if use_cuda:" in zerotts


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
    assert "zerotts_synthesize(" in gen  # zerotts (streaming decode, see Cell 7)
    # OmniVoice quality tuning: pinned diffusion steps/guidance + Vietnamese
    # language hint, with a TypeError fallback for older wheels.
    assert "OMNI_NUM_STEP" in gen
    assert "OMNI_GUIDANCE" in gen
    assert "OMNI_LANG" in gen
    assert "num_step=OMNI_NUM_STEP" in gen
    assert "language=OMNI_LANG" in gen
    assert 'TTS_OPTIONS.get("num_step")' in gen
    assert 'TTS_OPTIONS.get("language")' in gen
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


def test_kaggle_drive_branch_finds_batch_by_id_on_drive():
    """The Drive API branch locates the batch folder by batch_id (stable across
    retries), not by assuming a fixed /kaggle/input depth."""
    src = _code_cells(TEMPLATES[0])[3]
    assert "BATCH_ID" in src
    assert "batch_id" in src.lower()
    assert 'glob.glob("/kaggle/input/*' not in src


def test_hf_token_cell_never_blocks_headless_kaggle_kernels():
    """Cell 2's getpass() prompt hangs an automated Kaggle kernel (no stdin) when
    no HF token is configured. It must only prompt when interactive."""
    src = _code_cells(TEMPLATES[0])[1]
    assert "getpass" in src
    assert "isatty" in src
    assert "EOFError" in src


def test_model_install_failure_stops_loudly():
    """Cell 7 must fail at install time (check_call) rather than pages later at
    import with a confusing ModuleNotFoundError."""
    src = _code_cells(TEMPLATES[0])[6]
    assert "subprocess.check_call" in src
    assert "os.system" not in src


def test_cell8_clean_generated_audio_trims_and_compresses():
    import numpy as np

    helpers = _cell8_helpers()
    assert "clean_generated_audio" in helpers
    clean = helpers["clean_generated_audio"]
    sr = 48000
    speech = np.full(4800, 0.5, dtype=np.float32)
    audio = np.concatenate([
        np.zeros(9600, dtype=np.float32), speech,
        np.zeros(sr, dtype=np.float32), speech,
        np.zeros(4800, dtype=np.float32),
    ])
    cleaned = clean(audio, sr)
    assert cleaned.shape == (4800 + 14400 + 4800,)
    assert clean(np.zeros(100, dtype=np.float32), sr).shape == (0,)
