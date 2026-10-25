import csv
import json
from pathlib import Path

from . import constants as vc
from .types_defs import Outcome


class Ledger:
    """vigil's memory between nights, kept in the repo so it survives the
    machine: the session that runs a night is amnesiac, the files are not.

    - seen.json: every item ever tried, so a release is tested once.
    - results.tsv: one row per experiment, the lab notebook.
    - journal.md: the prose a later night reads before trying an idea.
    - nights/<night>.jsonl and .md: each night's outcomes and its report.
    """

    def __init__(self, repo: Path) -> None:
        self.root = repo / vc.STATE_DIR

    def _path(self, name: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / name

    def seen(self) -> dict[str, dict[str, str]]:
        path = self._path(vc.SEEN_FILE)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding=vc.ENCODING))

    def unseen(self, item_ids: list[str]) -> list[str]:
        seen = self.seen()
        return [i for i in item_ids if i not in seen]

    def record(self, outcome: Outcome) -> None:
        seen = self.seen()
        seen[outcome.item_id] = {"night": outcome.night, "verdict": outcome.verdict}
        self._path(vc.SEEN_FILE).write_text(
            json.dumps(seen, indent=2, sort_keys=True) + "\n", encoding=vc.ENCODING
        )
        results = self._path(vc.RESULTS_FILE)
        fresh = not results.exists()
        with results.open("a", encoding=vc.ENCODING, newline="") as fh:
            writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
            if fresh:
                writer.writerow(vc.RESULTS_HEADER)
            writer.writerow(
                [
                    outcome.night,
                    outcome.item_id,
                    outcome.experiment,
                    outcome.verdict,
                    outcome.headline,
                    outcome.branch or "",
                    f"{outcome.minutes:.1f}",
                ]
            )
        nights = self._path(vc.NIGHTS_DIR)
        nights.mkdir(exist_ok=True)
        with (nights / f"{outcome.night}.jsonl").open("a", encoding=vc.ENCODING) as fh:
            fh.write(outcome.model_dump_json() + "\n")
        with self._path(vc.JOURNAL_FILE).open("a", encoding=vc.ENCODING) as fh:
            fh.write(f"- {outcome.night} {outcome.item_id} [{outcome.experiment}] ")
            fh.write(f"{outcome.verdict}: {outcome.headline}\n")
            for line in outcome.evidence:
                fh.write(f"  - {line}\n")

    def night(self, night: str) -> list[Outcome]:
        path = self._path(vc.NIGHTS_DIR) / f"{night}.jsonl"
        if not path.exists():
            return []
        return [
            Outcome.model_validate_json(line)
            for line in path.read_text(encoding=vc.ENCODING).splitlines()
            if line.strip()
        ]

    def write_report(self, night: str, text: str) -> Path:
        path = self._path(vc.NIGHTS_DIR) / f"{night}.md"
        path.write_text(text, encoding=vc.ENCODING)
        return path
