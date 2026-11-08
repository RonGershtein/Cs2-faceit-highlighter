"""The fail package (name tag, freeze-frame, FAIL stamp, record scratch) for clips filmed outside the app, as a pure
function that returns the FFmpeg command (nothing renders here)."""
from __future__ import annotations

from pathlib import Path

from .cinematic import ACCENT, _drawtext


FAIL_HOLD = 1.0       # seconds the last frame is held
NAME_SECONDS = 2.4    # how long the name tag stays


def fail_args(ffmpeg: Path, src: Path, dst: Path, font: Path, scratch: Path, name: str, width: int, height: int,
              fps: int, seconds: float, has_audio: bool, tmp_base: Path, crf: int = 20) -> tuple[list[str], list[Path]]:
    """The fail package for a clip of `seconds`: a name tag sliding in at the start (when `name` is given), and at
    the end the last frame frozen for FAIL_HOLD seconds, slowly pushed in and drained of colour, a FAIL stamp
    slamming onto it and a record scratch under it. Returns (the FFmpeg command, text files to delete)."""
    files: list[Path] = []

    def tf(s: str) -> Path:
        p = tmp_base.with_name(f"{tmp_base.name}.f{len(files)}.txt")
        p.write_text(s, encoding="utf-8")
        files.append(p)
        return p
    n = max(2, round(FAIL_HOLD * fps))
    main = "[0:v]setpts=PTS-STARTPTS"
    if name:
        d = NAME_SECONDS
        slide = "(1-pow(1-min(1,max(0,t/0.3)),3))"
        env = f"min(1,max(0,({d}-t)/0.3))"
        size = int(height * 0.05)
        x0 = int(height * 0.06)
        y0 = int(height * 0.84)
        main += (f",drawbox=x={x0 - 14}:y={y0 - 10}:w=6:h={int(size * 1.4)}:color={ACCENT}@0.95:t=fill:enable='lt(t,{d})'")
        main += "," + _drawtext(font, tf(name), size, f"{x0}-{int(width * 0.08)}*(1-{slide})", str(y0),
                                alpha=f"{env}*{slide}", enable=f"lt(t,{d})")
    main += "[m]"
    stamp = int(height * 0.2)
    slam = "(1-pow(1-min(1,max(0,t/0.14)),3))"
    freeze = (f"[1:v]trim=end_frame=1,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={FAIL_HOLD},"
              f"scale={width}:{height},hue=s=0.25,eq=brightness=-0.08,"
              f"zoompan=z='1+0.07*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps},"
              + _drawtext(font, tf("FAIL"), f"{stamp}*(1+0.7*(1-{slam}))", "(w-text_w)/2", f"(h-text_h)/2-({height}*0.05)",
                          alpha=slam, color="0xFF3B30")
              + "[f]")
    audio_main = "[0:a]aresample=48000,aformat=channel_layouts=stereo[am]" if has_audio else \
        f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:{seconds:.3f}[am]"
    audio_f = f"[2:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{FAIL_HOLD},apad=whole_dur={FAIL_HOLD}[af]"
    graph = ";".join([main, freeze, audio_main, audio_f, "[m][am][f][af]concat=n=2:v=1:a=1[v][a]"])
    args = [str(ffmpeg), "-y", "-i", str(src), "-sseof", "-0.2", "-i", str(src), "-i", str(scratch),
            "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
    return args, files
