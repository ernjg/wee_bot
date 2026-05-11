import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthesize a short audio file with F5-TTS.")
    parser.add_argument("--model", default="F5TTS_v1_Base", help="F5-TTS model name or checkpoint preset.")
    parser.add_argument("--ref-audio", required=True, help="Reference audio path for the custom voice.")
    parser.add_argument("--ref-text", required=True, help="Transcript for the reference audio.")
    parser.add_argument("--voice", required=True, help="Friendly voice label for logs/config readability.")
    parser.add_argument("--language", default="en-us", help="Reserved for config parity; F5-TTS infers from text.")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed.")
    parser.add_argument("--format", default="wav", choices=("wav", "mp3"), help="Output audio format.")
    parser.add_argument("--output", required=True, help="Output audio path.")
    return parser.parse_args()


def find_cli() -> str | None:
    executable_dir = Path(sys.executable).resolve().parent
    for name in ("f5-tts_infer-cli.exe", "f5-tts_infer-cli"):
        candidate = executable_dir / name
        if candidate.exists():
            return str(candidate)
    return shutil.which("f5-tts_infer-cli")


def main() -> int:
    args = parse_args()
    text = sys.stdin.read().strip()
    if not text:
        print("No text provided on stdin.", file=sys.stderr)
        return 2

    if args.format != "wav":
        print("F5-TTS helper currently expects wav output. Set voice.format to wav.", file=sys.stderr)
        return 2

    ref_audio = Path(args.ref_audio)
    if not ref_audio.exists():
        print(f"Missing reference audio: {ref_audio}", file=sys.stderr)
        return 2

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cli = find_cli()
    if not cli:
        print("Could not find f5-tts_infer-cli. Install f5-tts in the configured Python environment.", file=sys.stderr)
        return 2

    command = [
        cli,
        "--model",
        args.model,
        "--ref_audio",
        str(ref_audio),
        "--ref_text",
        args.ref_text,
        "--gen_text",
        text,
        "--output_dir",
        str(output_path.parent),
        "--output_file",
        output_path.name,
        "--speed",
        str(args.speed),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout or "F5-TTS failed without output.")
        return completed.returncode
    if not output_path.exists() or output_path.stat().st_size == 0:
        sys.stderr.write("F5-TTS completed but did not create the expected output file.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
