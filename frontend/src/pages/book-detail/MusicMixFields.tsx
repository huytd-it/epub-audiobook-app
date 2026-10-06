import { CheckField, Field, fieldClass, selectClass } from "./parts";
import { MusicMixState, MusicPlacementSettings } from "./types";

/** Vị trí chèn nhạc nền: phủ dưới N giây cuối mỗi chương — dùng chung cho cấu
 * hình sách và cấu hình sản xuất mặc định. `disabled` khoá toàn bộ khi sách
 * không chọn nhạc. */
export function MusicPlacementFields({
  value,
  onChange,
  disabled,
}: {
  value: MusicPlacementSettings;
  onChange: (patch: Partial<MusicPlacementSettings>) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-3">
      <Field label="Chèn vào cuối mỗi chương (giây)" hint="1–300 · mặc định 15">
        <input
          className={fieldClass}
          type="number"
          min="1"
          max="300"
          step="1"
          disabled={disabled}
          value={value.music_chapter_end_seconds}
          onChange={(event) =>
            onChange({ music_chapter_end_seconds: Math.max(1, Math.min(300, Number(event.target.value) || 1)) })
          }
        />
      </Field>
      <p className="text-xs text-muted-foreground">
        Nhạc phát dưới giọng đọc ở những giây cuối của mỗi chương và kết thúc đúng lúc chương mới bắt đầu. Độ dài
        video, phụ đề và timeline YouTube không đổi.
      </p>
      <div className="flex flex-wrap gap-4">
        <CheckField
          checked={value.music_random_start}
          disabled={disabled}
          onChange={(checked) => onChange({ music_random_start: checked })}
          label="Chèn ngẫu nhiên (mỗi chương phát từ một đoạn bất kỳ trong bài)"
        />
        <CheckField
          checked={value.music_fade_enabled}
          disabled={disabled}
          onChange={(checked) => onChange({ music_fade_enabled: checked })}
          label="Fade nhạc vào/ra"
        />
      </div>
    </div>
  );
}

/** Mix nhạc nền của một sách: chọn bản nhạc + âm lượng (lưu qua /music-json) và
 * vị trí chèn (nằm trong VideoConfig). */
export function MusicMixFields({
  music,
  onMusicChange,
  placement,
  onPlacementChange,
}: {
  music: MusicMixState;
  onMusicChange: (next: MusicMixState) => void;
  placement: MusicPlacementSettings;
  onPlacementChange: (patch: Partial<MusicPlacementSettings>) => void;
}) {
  const noMusic = music.music_id == null;
  return (
    <div className="space-y-4 rounded-md border border-border p-3">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field label="Mix nhạc nền">
          <select
            className={selectClass}
            value={music.music_id ?? ""}
            onChange={(event) =>
              onMusicChange({ ...music, music_id: event.target.value ? Number(event.target.value) : null })
            }
          >
            <option value="">Không dùng nhạc</option>
            {music.tracks.map((track) => (
              <option key={track.id} value={track.id}>{track.name}</option>
            ))}
          </select>
        </Field>
        <Field label={`Âm lượng nhạc: ${music.music_volume}%`}>
          <input
            className="w-full accent-primary"
            type="range"
            min="0"
            max="100"
            value={music.music_volume}
            disabled={noMusic}
            onChange={(event) => onMusicChange({ ...music, music_volume: Number(event.target.value) })}
          />
        </Field>
      </div>
      <MusicPlacementFields value={placement} onChange={onPlacementChange} disabled={noMusic} />
    </div>
  );
}
