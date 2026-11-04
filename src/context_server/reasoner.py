"""Bounded positive-rule inference in a disposable subprocess; no raw ASP input."""
import json
import sys

import clingo


def solve(payload):
    facts, rules = payload["facts"], payload["rules"]
    definitions = [json.loads(r["definition"]) for r in rules]  # validated by put_rule
    constants = {f[k] for f in facts for k in ("subject", "predicate", "object")}
    constants.update(t for r in definitions for triple in [r["head"], *r["body"]]
                     for t in triple if not t.startswith("?"))
    # shortcut: bound the domain to contain grounding costs; use a dedicated reasoning service for larger graphs.
    if len(constants) > 128:
        raise ValueError("inference supports at most 128 distinct terms, including predicates")
    estimate = sum(max(1, len(constants)) ** len({t for triple in r["body"]
                   for t in triple if t.startswith("?")}) for r in definitions)
    if estimate > 1_000_000:
        raise ValueError("grounding estimate exceeds 1000000 variable assignments; reduce terms or rules")
    terms = {value: f"c{i}" for i, value in enumerate(sorted(constants))}
    reverse = {v: k for k, v in terms.items()}
    program = ["t(" + ",".join(terms[f[k]] for k in ("subject", "predicate", "object")) + ")."
               for f in facts]
    for rule in definitions:
        variables = {v: f"V{i}" for i, v in enumerate(sorted(
            {t for triple in rule["body"] for t in triple if t.startswith("?")}))}

        def atom(triple):
            return "t(" + ",".join(variables[t] if t.startswith("?") else terms[t] for t in triple) + ")"

        program.append(atom(rule["head"]) + ":-" + ",".join(atom(t) for t in rule["body"]) + ".")
    program.append("#show t/3.")
    control = clingo.Control(["--warn=none", "--models=1"])
    control.add("base", [], "\n".join(program))
    control.ground([("base", [])])
    symbols = []  # a positive program has exactly one model
    control.solve(on_model=lambda model: symbols.extend(model.symbols(shown=True)))
    if len(symbols) > 10_000:
        raise ValueError("inference exceeded 10000 results; narrow the rules")
    return sorted([[reverse[str(a)] for a in symbol.arguments] for symbol in symbols])


if __name__ == "__main__":
    try:
        print(json.dumps(solve(json.load(sys.stdin))))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
