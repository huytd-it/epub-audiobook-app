import React from "react";
import { Composition } from "remotion";
import { ShortIntro } from "./ShortIntro";
import { defaultProps } from "./types";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="ShortIntro"
        component={ShortIntro}
        // Props thật do Python truyền qua --props; các giá trị dưới đây chỉ
        // là mặc định để `remotion studio` mở lên được ngay.
        durationInFrames={defaultProps.durationInFrames}
        fps={defaultProps.fps}
        width={defaultProps.width}
        height={defaultProps.height}
        defaultProps={defaultProps}
        calculateMetadata={({ props }) => ({
          width: props.width,
          height: props.height,
          fps: props.fps,
          durationInFrames: props.durationInFrames,
        })}
      />
    </>
  );
};