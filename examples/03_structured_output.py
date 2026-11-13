"""Get the answer back as a typed object.

`schema` accepts a JSON Schema dict, a type hint, a dataclass, a TypedDict or
a Pydantic model. The value the model submits is validated; if it does not
fit, the model is shown the errors and fixes it.

    python examples/03_structured_output.py
"""

from dataclasses import dataclass

import rlm_ig as rlm

REVIEWS = [
    {"user": "asha", "stars": 5, "text": "Fast shipping, the kettle looks great and boils quickly."},
    {"user": "bo", "stars": 2, "text": "Stopped working after three weeks. Support never replied."},
    {"user": "cy", "stars": 4, "text": "Good value. The lid is a little stiff."},
    {"user": "dee", "stars": 1, "text": "Arrived with a cracked base and leaked on first use."},
    {"user": "eli", "stars": 5, "text": "Exactly as described, would buy again."},
]


@dataclass
class Summary:
    average_stars: float
    complaints: list[str]
    verdict: str


result = rlm.run(
    "Summarise these product reviews: the exact average star rating, the distinct complaints, and a one-sentence verdict.",
    REVIEWS,
    schema=Summary,
)
summary: Summary = result.parsed
print(summary.average_stars, summary.complaints, summary.verdict, sep="\n")
