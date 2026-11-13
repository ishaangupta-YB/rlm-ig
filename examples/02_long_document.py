"""Ask a question about a file far larger than a context window.

    python examples/02_long_document.py                 # generates a sample log
    python examples/02_long_document.py big.log "What went wrong on the 14th?"

Uses whichever backend `rlm doctor` reports as the default. Set RLM_MODEL
(for example RLM_MODEL=claude-code:haiku) to choose another.
"""

import random
import sys

import rlm_ig as rlm


def sample_log() -> str:
    rng = random.Random(3)
    lines = [
        f"2026-05-{1 + i // 4000:02d} worker-{rng.randrange(8)} INFO job {rng.randrange(10**6)} finished in {rng.randrange(20, 4000)} ms"
        for i in range(60_000)
    ]
    lines[41_873] = "2026-05-11 worker-3 FATAL disk quota exceeded on volume vol-7; 214 jobs dropped"
    return "\n".join(lines)


path = sys.argv[1] if len(sys.argv) > 1 else None
question = (
    sys.argv[2] if len(sys.argv) > 2 else "Was there a fatal error? If so: when, on which worker, and what happened?"
)
data = rlm.load(path) if path else sample_log()

result = rlm.run(question, data, max_usd=1.00)

print()
print(result.answer)
print(f"\n{result.status} in {result.seconds:.1f}s, {result.usage}")
print(f"step-by-step: rlm trace {result.trace}")
