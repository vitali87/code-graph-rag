from pydantic import BaseModel

from . import constants as vc


class Item(BaseModel):
    """One thing that changed in the world overnight and may reach cgr."""

    id: str
    package: str
    scope: vc.Scope
    scope_name: str | None = None
    requirement: str
    locked: str
    latest: str
    # Whether the range cgr publishes already admits `latest`: if it does,
    # every fresh install resolves to it today, whatever uv.lock pins.
    in_range: bool
    released: str | None = None
    links: dict[str, str] = {}


class Cone(BaseModel):
    """The part of cgr a package reaches, read from cgr's own graph."""

    package: str
    imports: list[str]
    source_files: list[str]
    test_files: list[str]


class Measurement(BaseModel):
    corpus: str
    build_ms: float
    peak_rss_mib: float
    nodes: int
    rels: int
    digest: str


class Comparison(BaseModel):
    corpus: str
    baseline_ms: list[float]
    candidate_ms: list[float]
    median_delta_ms: float
    relative: float
    wins: int
    losses: int
    p_value: float
    speed: vc.Speed
    rss_ratio: float


class SuiteRun(BaseModel):
    selected: int
    passed: int
    failed: list[str]

    @property
    def ok(self) -> bool:
        return not self.failed


class GraphDelta(BaseModel):
    corpus: str
    baseline_digest: str
    candidate_digest: str
    nodes_added: int
    nodes_removed: int
    rels_added: int
    rels_removed: int
    examples: list[str]


class Outcome(BaseModel):
    item_id: str
    experiment: vc.Experiment
    verdict: vc.Verdict
    headline: str
    evidence: list[str] = []
    branch: str | None = None
    night: str
    minutes: float = 0.0
