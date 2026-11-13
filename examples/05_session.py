"""Follow-up questions that reuse earlier work.

Inside a Session the REPL stays alive between tasks, so what the first task
built (here: parsed rows) is still there for the next ones. Pass `path=` to
keep it across program runs as well.

    python examples/05_session.py
"""

import random

import rlm_ig as rlm

rng = random.Random(9)
csv = "date,region,product,units,unit_price\n" + "\n".join(
    f"2026-{rng.randrange(1, 13):02d}-{rng.randrange(1, 29):02d},{rng.choice(['north', 'south', 'east', 'west'])},"
    f"{rng.choice(['widget', 'gadget', 'gizmo'])},{rng.randrange(1, 60)},{rng.choice([4.5, 12.0, 30.0])}"
    for _ in range(5_000)
)

with rlm.Session() as session:
    first = session.run("Parse this CSV into a list of dicts called `rows` and tell me how many rows there are.", csv)
    print("rows:", first.answer)

    second = session.run("Using the rows you already parsed: which region had the highest revenue, and how much?")
    print("top region:", second.answer)

    third = session.run("And which month was the weakest for that region?")
    print("weakest month:", third.answer)

    print("\nvariables kept in the REPL:", [v["name"] for v in session.variables()])
