"""cast_to_raw.py -- Convert an ai_terminal .cast recording to a raw byte stream.

Concatenates every "o" (output) event's data, in order, with no slicing or
selection -- the whole session, byte-for-byte what the child process wrote.
Feed the result to escape-artist for a live annotated view:

    python tools/cast_to_raw.py <session>.cast raw_output.txt
    escape-artist --replay-file raw_output.txt

Usage: python cast_to_raw.py <input.cast> <output.txt>
"""
import json
import sys


def main():
    if len(sys.argv) != 3:
        print("usage: python cast_to_raw.py <input.cast> <output.txt>")
        return 1

    in_path, out_path = sys.argv[1], sys.argv[2]

    with open(in_path, "r", encoding="utf-8") as f:
        next(f)  # asciicast v3 header line
        out = open(out_path, "w", encoding="utf-8", newline="")
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if len(event) >= 3 and event[1] == "o":
                    out.write(event[2])
        finally:
            out.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
