import argparse
import json
from pathlib import Path

from . import cold, disposition, line, meal, report, sample
from .store import connect, read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "meal",
            "sample",
            "cold",
            "line",
            "portion",
            "closeout",
            "registrations",
            "servable",
            "preview",
            "report",
            "demo",
        ],
    )
    parser.add_argument("--db", default="mealhold.sqlite")
    parser.add_argument("--input")
    parser.add_argument("--meal")
    args = parser.parse_args()
    db = connect(args.db)
    try:
        if args.command == "report":
            result = report.build(db)
        elif args.command == "servable":
            if not args.meal:
                parser.error("--meal required")
            result = meal.servable_check(db, args.meal)
        elif args.command == "preview":
            if not args.meal:
                parser.error("--meal required")
            result = disposition.preview(db, args.meal)
        elif args.command == "demo":
            root = Path.cwd() / "fixtures"
            result = []
            for command in ("meal", "sample", "cold", "line"):
                with db:
                    result.append(
                        globals()[command].run(db, read(root / f"{command}-ok.json"))
                    )
            for name in ("disposition-ok.json", "closeout-ok.json"):
                path = root / name
                if not path.exists():
                    continue
                with db:
                    if name.startswith("closeout"):
                        result.append(disposition.closeout(db, read(path)))
                    else:
                        result.append(disposition.run(db, read(path)))
            result = {"runs": result, "report": report.build(db)}
        else:
            if not args.input:
                parser.error("--input required")
            payload = read(args.input)
            if args.command == "portion":
                func = disposition.run
            elif args.command == "closeout":
                func = disposition.closeout
            elif args.command == "registrations":
                func = meal.registrations
            else:
                func = globals()[args.command].run
            with db:
                result = func(db, payload)
        print(
            json.dumps(
                {"module_file": __file__, "result": result},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
