import argparse
import sys
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthesize a short audio file with Kokoro ONNX.")
    parser.add_argument("--model", required=True, help="Path to kokoro-v1.0.onnx.")
    parser.add_argument("--voices", required=True, help="Path to voices-v1.0.bin.")
    parser.add_argument("--voice", required=True, help="Kokoro voice ID, such as af_sarah or am_adam.")
    parser.add_argument("--language", default="en-us", help="Kokoro language code.")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech speed.")
    parser.add_argument("--format", default="wav", choices=("wav", "mp3"), help="Output audio format.")
    parser.add_argument("--output", required=True, help="Output audio path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    text = sys.stdin.read().strip()
    if not text:
        print("No text provided on stdin.", file=sys.stderr)
        return 2

    kokoro = Kokoro(args.model, args.voices)
    samples, sample_rate = kokoro.create(
        text,
        voice=args.voice,
        speed=args.speed,
        lang=args.language,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), samples, sample_rate, format=args.format.upper())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
