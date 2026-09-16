#!/usr/bin/env python3
"""Preserve the existing off-peak schedule; release only our own affordable hold."""
import datetime
import sys

import farm_guard_common as guard

MARK = "offpeak"
PEAK = [(1, 4), (6, 10)]


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    # Ceník DeepSeeku má špičku jen v pracovní dny (Po–Pá 01–04 a 06–10 UTC);
    # o víkendu je celý den levný, takže pauza by farmu brzdila zbytečně.
    in_peak = now.weekday() < 5 and any(start <= now.hour < end for start, end in PEAK)
    state = guard.snapshot()
    dry = "--dry-run" in sys.argv
    if in_peak:
        changed = guard.set_pause(state, MARK, dry)
    else:
        changed = guard.resume(state, MARK, dry)
    print(f"{now:%H:%M} UTC: {'peak' if in_peak else 'off-peak'}; "
          f"{'dry run' if dry else 'pause updated' if changed else 'existing decision preserved'}")
    return 0


if __name__ == "__main__":
    sys.exit(guard.exit_on_error(main))
