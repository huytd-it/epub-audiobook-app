"""Pre-render one video-only gameplay clip from its frozen replay."""
from pathlib import Path

from app import gameplay_renderer
from app.gameplay_repository import apply_replay_stats, load_replay
from app.jobqueue.models import JobFatalError
from app.video_integrity import VideoExpectation, validate_video
from app.video_publish import publish_validated_video


def handle(ctx) -> dict:
    clip_id = ctx.job.payload.get("clip_id")
    row = ctx.conn.execute("SELECT * FROM gameplay_clip WHERE id=?", (clip_id,)).fetchone()
    if row is None:
        raise JobFatalError(f"gameplay clip {clip_id} not found")
    replay = load_replay(ctx.conn, row["replay_id"])
    # Profile dimensions are frozen in the replay job's clip profile via route defaults.
    dims = ctx.job.payload.get("resolution") or [1920, 1080]
    fps = int(ctx.job.payload.get("fps") or 30)
    path = row["file_path"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    expected = VideoExpectation(duration_seconds=replay.duration_seconds, width=int(dims[0]),
                                height=int(dims[1]), fps=fps, pixel_format="yuv420p", rotation=0,
                                require_audio=False)
    try:
        with ctx.keep_alive():
            publish_validated_video(path, lambda temp: gameplay_renderer.render_replay(
                replay, temp, resolution=(int(dims[0]), int(dims[1])), fps=fps),
                validator=lambda output: validate_video(output, expected=expected))
        ctx.conn.execute("UPDATE gameplay_clip SET status='available', error_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (clip_id,))
        ctx.conn.commit()
        apply_replay_stats(ctx.conn, row["replay_id"])
    except Exception as exc:
        ctx.conn.execute("UPDATE gameplay_clip SET status='failed', error_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (str(exc)[:2000], clip_id))
        ctx.conn.commit()
        raise
    return {"clip_id": clip_id, "output_path": path, "duration_seconds": replay.duration_seconds}
