from pathlib import Path
from types import SimpleNamespace

import pytest

from app import image_overlay, video_gen


def _patch(index=0, image_path=None):
    return SimpleNamespace(id=index + 1, patch_index=index, image_path=image_path)


def test_shared_backgrounds_rotate_by_patch_index(tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    config = {"backgrounds": [str(first), str(second)], "background_mode": "sequential"}

    assert video_gen.resolve_configured_patch_image(_patch(0), config, "") == str(first)
    assert video_gen.resolve_configured_patch_image(_patch(1), config, "") == str(second)
    assert video_gen.resolve_configured_patch_image(_patch(2), config, "") == str(first)


def test_patch_background_overrides_shared_backgrounds(tmp_path):
    shared = tmp_path / "shared.jpg"
    own = tmp_path / "own.jpg"
    shared.write_bytes(b"shared")
    own.write_bytes(b"own")
    patch = _patch(0, str(own))

    assert video_gen.resolve_configured_patch_image(patch, {"backgrounds": [str(shared)]}, "") == str(own)


def test_random_background_order_is_stable_for_same_patch(tmp_path):
    paths = []
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        paths.append(str(path))
    config = {"backgrounds": paths, "background_mode": "random"}

    first = video_gen.resolve_configured_patch_image(_patch(4), config, "book-1")
    assert first == video_gen.resolve_configured_patch_image(_patch(4), config, "book-1")
    assert first in paths


def test_missing_shared_background_falls_back_to_default(tmp_path):
    fallback = tmp_path / "fallback.jpg"
    fallback.write_bytes(b"fallback")
    config = {"backgrounds": [str(tmp_path / "missing.jpg")]}

    assert video_gen.resolve_configured_patch_image(_patch(), config, str(fallback)) == str(fallback)


def test_full_video_uses_rotated_shared_background_for_each_patch(tmp_path, monkeypatch):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    patches = [_patch(0), _patch(1)]
    for patch in patches:
        patch.audio_path = str(tmp_path / f"{patch.id}.wav")
        Path(patch.audio_path).write_bytes(b"audio")
        patch.image_type = "none"
    book = SimpleNamespace(video_resolution="1280x720", video_fps=30, default_image_animation="none", background_image_path=None)
    seen = []

    # All three stubs go through monkeypatch: concat_segments and
    # ensure_patch_overlay used to be assigned bare and leaked into later tests.
    monkeypatch.setattr(
        video_gen, "generate_background_sequence",
        lambda backgrounds, *args, **kwargs: seen.append(kwargs["start_index"]),
    )
    monkeypatch.setattr(video_gen, "concat_segments", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        image_overlay, "ensure_patch_overlay",
        lambda book, patch, font_path=None, **kwargs: kwargs.get("background_path"),
    )
    video_gen.generate_full_video(patches, book, str(tmp_path / "out.mp4"), default_image=str(first), video_config={"backgrounds": [str(first), str(second)], "background_mode": "sequential"})

    assert seen == [0, 1]


def test_background_segment_plan_repeats_until_audio_duration():
    plan = video_gen.plan_background_segments(["a.jpg", "b.jpg"], 35, 15, "sequential")
    assert plan == [("a.jpg", 15), ("b.jpg", 15), ("a.jpg", 5)]


def test_random_background_segment_plan_is_stable():
    first = video_gen.plan_background_segments(["a.jpg", "b.jpg", "c.jpg"], 40, 10, "random", seed="book-1-patch-2")
    second = video_gen.plan_background_segments(["a.jpg", "b.jpg", "c.jpg"], 40, 10, "random", seed="book-1-patch-2")
    assert first == second
    assert sum(duration for _, duration in first) == 40


def test_full_video_orders_intro_main_outro_per_patch(tmp_path):
    image = tmp_path / "bg.jpg"
    image.write_bytes(b"bg")
    audio = tmp_path / "main.wav"
    intro = tmp_path / "intro.wav"
    outro = tmp_path / "outro.wav"
    for path in (audio, intro, outro):
        path.write_bytes(b"audio")
    patch = _patch(0)
    patch.audio_path = str(audio)
    patch.image_type = "none"
    patch.overlay_config = None
    book = SimpleNamespace(id=1, video_resolution="1280x720", video_fps=30, default_image_animation="none", background_image_path=None, overlay_config=None)
    seen = []
    original = video_gen.generate_segment
    original_concat = video_gen.concat_segments
    video_gen.generate_segment = lambda image_path, audio_path, *args, **kwargs: seen.append((image_path, audio_path, kwargs.get("music_path")))
    video_gen.concat_segments = lambda *args, **kwargs: None
    try:
        video_gen.generate_full_video([patch], book, str(tmp_path / "out.mp4"), default_image=str(image), intro_audio=str(intro), outro_audio=str(outro), music_path="music.mp3")
    finally:
        video_gen.generate_segment = original
        video_gen.concat_segments = original_concat
    assert [item[1] for item in seen] == [str(intro), str(audio), str(outro)]
    assert seen[0][2] is None and seen[1][2] == "music.mp3" and seen[2][2] is None


def test_background_sequence_adds_ken_burns_and_progress_filters(tmp_path, monkeypatch):
    image = tmp_path / "bg.jpg"
    audio = tmp_path / "audio.wav"
    image.write_bytes(b"bg")
    audio.write_bytes(b"audio")
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        class Result:
            stdout = "10"
        return Result()

    monkeypatch.setattr(video_gen.subprocess, "run", run)
    monkeypatch.setattr(video_gen, "concat_segments", lambda *args, **kwargs: None)
    video_gen.generate_background_sequence(
        [str(image)], str(audio), str(tmp_path / "out.mp4"),
        resolution=(1280, 720), fps=30, image_duration=15,
        ken_burns=True, progress_bar=True,
    )
    filters = " ".join(str(cmd) for cmd in commands)
    assert "zoompan" in filters
    assert "drawbox" in filters


def test_background_sequence_crossfade_uses_xfade(tmp_path, monkeypatch):
    images = []
    for name in ("a.jpg", "b.jpg"):
        path = tmp_path / name
        path.write_bytes(b"bg")
        images.append(str(path))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    commands = []
    graphs = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        if "-filter_complex_script" in cmd:
            # The script lives in a TemporaryDirectory that is gone by the time
            # the assertions run, so read it while ffmpeg would have.
            script = Path(kwargs["cwd"]) / cmd[cmd.index("-filter_complex_script") + 1]
            graphs.append(script.read_text(encoding="utf-8"))
        class Result:
            stdout = "20"
        return Result()

    monkeypatch.setattr(video_gen.subprocess, "run", run)
    # Must go through monkeypatch: a bare assignment here leaked the stub into
    # every later test in the session.
    monkeypatch.setattr(video_gen, "concat_segments", lambda *args, **kwargs: None)
    video_gen.generate_background_sequence(
        images, str(audio), str(tmp_path / "out.mp4"),
        resolution=(1280, 720), fps=30, image_duration=15,
        crossfade=True, crossfade_seconds=1,
    )
    assert graphs, "crossfade did not build an xfade graph"
    graph = graphs[0]
    assert "xfade" in graph
    # Every settb output must be consumed by the xfade chain: an unconnected
    # filter output makes ffmpeg abort before encoding anything.
    labels = {f"[v{i}]" for i in range(graph.count("settb"))}
    assert all(graph.count(label) == 2 for label in labels), graph


def test_background_sequence_crossfade_command_fits_windows_limit(tmp_path, monkeypatch):
    """A long patch makes hundreds of pieces; the ffmpeg argv must stay runnable.

    Windows CreateProcess rejects command lines over 32767 characters, which is
    how this path used to fail (WinError 206) instead of rendering.
    """
    images = []
    for name in ("a.jpg", "b.jpg"):
        path = tmp_path / name
        path.write_bytes(b"bg")
        images.append(str(path))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        class Result:
            stdout = "9000"  # 2.5 hours -> 600 pieces at 15s each
        return Result()

    monkeypatch.setattr(video_gen.subprocess, "run", run)
    monkeypatch.setattr(video_gen, "concat_segments", lambda *args, **kwargs: None)
    video_gen.generate_background_sequence(
        images, str(audio), str(tmp_path / "out.mp4"),
        resolution=(1280, 720), fps=30, image_duration=15,
        crossfade=True, crossfade_seconds=1,
    )
    xfade_cmd = next(cmd for cmd in commands if "-filter_complex_script" in cmd)
    assert len(" ".join(xfade_cmd)) < 32000, len(" ".join(xfade_cmd))


def test_background_sequence_crossfade_still_covers_the_narration(tmp_path, monkeypatch):
    """Overlapping pieces must not shorten the visual below the audio duration.

    Each xfade eats `fade` seconds of the timeline, so pieces planned end to end
    leave the final mux's -shortest clipping the tail off the narration.
    """
    images = []
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        path = tmp_path / name
        path.write_bytes(b"bg")
        images.append(str(path))
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        class Result:
            stdout = "45"
        return Result()

    monkeypatch.setattr(video_gen.subprocess, "run", run)
    monkeypatch.setattr(video_gen, "concat_segments", lambda *args, **kwargs: None)
    video_gen.generate_background_sequence(
        images, str(audio), str(tmp_path / "out.mp4"),
        resolution=(1280, 720), fps=30, image_duration=15,
        crossfade=True, crossfade_seconds=1,
    )
    lengths = [float(cmd[cmd.index("-t") + 1]) for cmd in commands if "-t" in cmd]
    assert len(lengths) == 3
    fade = 1.0
    assert sum(lengths) - fade * (len(lengths) - 1) == 45


def test_probe_raises_for_unreadable_video_background(monkeypatch):
    """A truncated clip (ftyp header, no moov atom) must not be mistaken for a
    still image: ffprobe fails, so the probe marks it unusable."""
    def boom(path):
        raise ValueError("moov atom not found")

    monkeypatch.setattr(video_gen, "_probe_duration", boom)
    probe = video_gen._video_background_duration_probe()

    assert probe("still.jpg") is None  # images are never probed
    for _ in range(2):  # failure is cached, not re-probed and swallowed
        with pytest.raises(video_gen.UnusableBackground):
            probe("clip.mp4")


def test_background_segments_skip_unreadable_video_clips():
    def clip_duration(path):
        if path == "corrupt.mp4":
            raise video_gen.UnusableBackground(path)
        return 30.0

    plan = video_gen.plan_background_segments(
        ["corrupt.mp4", "good.mp4"], 50, 15, clip_duration=clip_duration)

    assert plan == [("good.mp4", 30.0), ("good.mp4", 20.0)]


def test_background_segments_all_unreadable_is_empty():
    def clip_duration(path):
        raise video_gen.UnusableBackground(path)

    assert video_gen.plan_background_segments(
        ["a.mp4", "b.mp4"], 30, 15, clip_duration=clip_duration) == []


def test_background_sequence_skips_a_corrupt_video_background(tmp_path, monkeypatch):
    """One corrupt clip used to abort the whole piece with AVERROR_INVALIDDATA;
    it is now dropped and the still image covers the narration."""
    still = tmp_path / "bg.jpg"
    still.write_bytes(b"bg")
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    commands = []

    def fake_probe(path):
        if str(path) == str(corrupt):
            raise ValueError("moov atom not found")
        return 15.0

    def run(cmd, **kwargs):
        commands.append(cmd)
        class Result:
            stdout = "15"
        return Result()

    monkeypatch.setattr(video_gen, "_probe_duration", fake_probe)
    monkeypatch.setattr(video_gen.subprocess, "run", run)
    monkeypatch.setattr(video_gen, "concat_segments", lambda *args, **kwargs: None)

    video_gen.generate_background_sequence(
        [str(corrupt), str(still)], str(audio), str(tmp_path / "out.mp4"),
        resolution=(1280, 720), fps=30, image_duration=15,
    )

    argv = [arg for cmd in commands for arg in cmd]
    assert str(corrupt) not in argv
    assert argv, "no ffmpeg command was built"
    assert str(still) in argv
