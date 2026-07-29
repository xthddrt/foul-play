"""Falsification oracle for samplers of Showdown random-battle teams.

The empirical sidecars are treated as generator draws, not as a set database.
That distinction matters: a sampler can reproduce every one-dimensional move
frequency while inventing combinations that Showdown never emits.  The
move-pair family below is therefore deliberately a joint-set test.

Only standard-library numerics are used.  The project environment does not
ship SciPy, and an in-repository survival-function implementation keeps the
oracle runnable in the same minimal environment as the search code.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import itertools
import json
import math
from pathlib import Path
import random
from typing import Callable, Iterable


FAMILY_COUNT = 5
MIN_EXPECTED = 5.0


class SamplerProtocol:
    """Documentation-only protocol for a future Phase-1 prior."""

    def sample_teams(self, n: int, seed: int) -> list[list[dict]]:
        """Return ``n`` teams in the generator-sidecar mon-dict format."""

        raise NotImplementedError


@dataclass(frozen=True)
class StatisticResult:
    """The decision and evidence for one Bonferroni family."""

    family: int
    name: str
    test: str
    statistic: float | None
    degrees_of_freedom: int | None
    p_value: float
    threshold: float
    verdict: str
    details: str

    @property
    def rejected(self) -> bool:
        return self.verdict == "REJECT"


@dataclass(frozen=True)
class OracleReport:
    """Complete oracle result, including the multiple-testing arithmetic."""

    statistics: tuple[StatisticResult, ...]
    alpha: float
    family_threshold: float
    correction: str
    verdict: str

    @property
    def rejecting_families(self) -> tuple[str, ...]:
        return tuple(result.name for result in self.statistics if result.rejected)

    def format_table(self) -> str:
        """Render stable text for the CLI and deterministic forensic logs."""

        rows = [
            (
                "Family",
                "Statistic",
                "Test",
                "p-value",
                "Threshold",
                "Verdict",
            )
        ]
        for result in self.statistics:
            if result.statistic is None:
                test = result.test
            elif result.degrees_of_freedom is None:
                test = f"{result.test}={result.statistic:.6g}"
            else:
                test = (
                    f"{result.test}={result.statistic:.6g}/"
                    f"df={result.degrees_of_freedom}"
                )
            rows.append(
                (
                    str(result.family),
                    result.name,
                    test,
                    f"{result.p_value:.6g}",
                    f"{result.threshold:.6g}",
                    result.verdict,
                )
            )

        widths = [
            max(len(row[column]) for row in rows) for column in range(len(rows[0]))
        ]
        lines = [
            "  ".join(value.ljust(widths[column]) for column, value in enumerate(row))
            for row in rows
        ]
        lines.append("")
        lines.append(self.correction)
        lines.append(f"Overall verdict: {self.verdict}")
        return "\n".join(lines)


def iter_sidecar_teams(
    directory: str | Path, limit: int | None = None
) -> Iterable[list[dict]]:
    """Yield both generator teams from each sidecar in stable filename order.

    ``limit`` counts games (sidecar files), not teams.  This mirrors the CLI's
    ``--limit-games`` name and avoids silently loading twice the requested
    number of large JSON documents.
    """

    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative or None")

    paths = sorted(Path(directory).glob("*.teams.json"))
    if limit is not None:
        paths = paths[:limit]
    for path in paths:
        with path.open(encoding="utf-8") as sidecar_file:
            sidecar = json.load(sidecar_file)
        for side in ("p1", "p2"):
            yield sidecar["teams"][side]["team"]


def load_teams(
    directory: str | Path, limit: int | None = None
) -> list[list[dict]]:
    """Materialize :func:`iter_sidecar_teams` for comparison and splitting."""

    return list(iter_sidecar_teams(directory, limit=limit))


def load_empirical_teams(
    directory: str | Path, limit_games: int | None = None
) -> list[list[dict]]:
    """Named loader alias whose argument matches the CLI terminology."""

    return load_teams(directory, limit=limit_games)


def split_halves(
    teams: list[list[dict]], seed: int = 0
) -> tuple[list[list[dict]], list[list[dict]]]:
    """Seeded random split used by the self-consistency control."""

    indices = list(range(len(teams)))
    random.Random(seed).shuffle(indices)
    midpoint = len(indices) // 2
    return (
        [teams[index] for index in indices[:midpoint]],
        [teams[index] for index in indices[midpoint:]],
    )


def _gammaincc(shape: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(shape, x).

    Pearson chi-square survival probabilities are ``Q(df / 2, chi2 / 2)``.
    The series and continued-fraction branches are the standard stable split:
    compute P below ``shape + 1`` and Q directly above it.
    """

    if shape <= 0.0 or x < 0.0:
        raise ValueError("gamma arguments must satisfy shape > 0 and x >= 0")
    if x == 0.0:
        return 1.0

    epsilon = 1e-14
    tiny = 1e-300
    max_iterations = 10000
    log_scale = -x + shape * math.log(x) - math.lgamma(shape)

    if x < shape + 1.0:
        term = 1.0 / shape
        total = term
        denominator = shape
        for _ in range(max_iterations):
            denominator += 1.0
            term *= x / denominator
            total += term
            if abs(term) <= abs(total) * epsilon:
                break
        else:  # pragma: no cover - protects the oracle from silent bad numerics
            raise ArithmeticError("incomplete-gamma series did not converge")
        return min(1.0, max(0.0, 1.0 - total * math.exp(log_scale)))

    b = x + 1.0 - shape
    c = 1.0 / tiny
    d = 1.0 / max(b, tiny)
    fraction = d
    for iteration in range(1, max_iterations + 1):
        coefficient = -iteration * (iteration - shape)
        b += 2.0
        d = coefficient * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + coefficient / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        fraction *= delta
        if abs(delta - 1.0) <= epsilon:
            break
    else:  # pragma: no cover - protects the oracle from silent bad numerics
        raise ArithmeticError("incomplete-gamma fraction did not converge")
    return min(1.0, max(0.0, math.exp(log_scale) * fraction))


def _chi_square_sf(chi_square: float, degrees_of_freedom: int) -> float:
    if degrees_of_freedom <= 0:
        return 1.0
    if math.isinf(chi_square):
        return 0.0
    return _gammaincc(degrees_of_freedom / 2.0, chi_square / 2.0)


def _pooled_contingency(
    empirical: Counter, candidate: Counter
) -> tuple[list[tuple[int, int]], int]:
    """Build a two-row table whose expected cells all meet the χ² guard.

    Categories are ordered by empirical frequency, so individually retained
    cells are the requested top-K and every lower-frequency category enters a
    single tail.  If that tail is still too small, the least common retained
    categories join it until both expected tail cells reach five.
    """

    empirical_total = sum(empirical.values())
    candidate_total = sum(candidate.values())
    if empirical_total == 0 or candidate_total == 0:
        return [], len(set(empirical) | set(candidate))

    ordered = sorted(
        set(empirical) | set(candidate),
        key=lambda key: (-empirical[key], repr(key)),
    )
    grand_total = empirical_total + candidate_total

    def expected_ok(empirical_count: int, candidate_count: int) -> bool:
        column_total = empirical_count + candidate_count
        return (
            column_total * empirical_total / grand_total >= MIN_EXPECTED
            and column_total * candidate_total / grand_total >= MIN_EXPECTED
        )

    retained: list[tuple[int, int]] = []
    tail_empirical = 0
    tail_candidate = 0
    in_tail = False
    for key in ordered:
        counts = (empirical[key], candidate[key])
        # Once a category fails, all less-common empirical categories are tail.
        if in_tail or not expected_ok(*counts):
            in_tail = True
            tail_empirical += counts[0]
            tail_candidate += counts[1]
        else:
            retained.append(counts)

    while (
        (tail_empirical or tail_candidate)
        and not expected_ok(tail_empirical, tail_candidate)
        and retained
    ):
        empirical_count, candidate_count = retained.pop()
        tail_empirical += empirical_count
        tail_candidate += candidate_count

    if tail_empirical or tail_candidate:
        retained.append((tail_empirical, tail_candidate))
    return retained, len(ordered)


def _chi_square_homogeneity(
    empirical: Counter, candidate: Counter
) -> tuple[float, int, int, int]:
    """Pearson two-sample homogeneity test with a pooled sparse tail."""

    cells, raw_cell_count = _pooled_contingency(empirical, candidate)
    if not cells:
        # A missing comparison row is maximally incompatible unless both are
        # empty; callers never intentionally test an all-empty distribution.
        if sum(empirical.values()) == sum(candidate.values()) == 0:
            return 0.0, 0, 0, raw_cell_count
        return math.inf, 1, 0, raw_cell_count
    if len(cells) == 1:
        return 0.0, 0, 1, raw_cell_count

    empirical_total = sum(first for first, _ in cells)
    candidate_total = sum(second for _, second in cells)
    grand_total = empirical_total + candidate_total
    chi_square = 0.0
    for empirical_count, candidate_count in cells:
        column_total = empirical_count + candidate_count
        expected_empirical = column_total * empirical_total / grand_total
        expected_candidate = column_total * candidate_total / grand_total
        chi_square += (
            (empirical_count - expected_empirical) ** 2 / expected_empirical
            + (candidate_count - expected_candidate) ** 2 / expected_candidate
        )
    return chi_square, len(cells) - 1, len(cells), raw_cell_count


def _generator_species(mon: dict) -> str:
    """Recover Ditto from sidecars captured after an Imposter transformation.

    A small number of corpus sidecars retain the copied species name even
    though the remaining generator fields unambiguously identify Ditto:
    level 87, Imposter, Choice Scarf, and Transform.  Treating that battle-state
    mutation as a generated species creates singleton level anomalies and makes
    truthful split halves fail the exact level-set control.
    """

    if mon.get("ability") == "Imposter" and mon.get("moves") == ["transform"]:
        return "Ditto"
    return mon["species"]


def _mons_by_species(teams: list[list[dict]]) -> dict[str, list[dict]]:
    by_species: dict[str, list[dict]] = defaultdict(list)
    for team in teams:
        for mon in team:
            by_species[_generator_species(mon)].append(mon)
    return by_species


def _common_species(
    empirical_by_species: dict[str, list[dict]]
) -> tuple[list[str], int]:
    """Select species with enough draws for conditional joint-set tests.

    A fixed 200-draw cutoff would select no species in the required 10k-team
    control (the corpus maximum is below 200).  Fifty draws supply 300 move
    pairs, while the 0.1%-of-slots floor scales the guard for larger corpora.
    """

    total_mons = sum(len(mons) for mons in empirical_by_species.values())
    cutoff = max(50, math.ceil(total_mons / 1000))
    species = sorted(
        (
            name
            for name, mons in empirical_by_species.items()
            if len(mons) >= cutoff
        ),
        key=lambda name: (-len(empirical_by_species[name]), name),
    )
    return species, cutoff


def _stratified_chi_square(
    empirical_by_species: dict[str, list[dict]],
    candidate_by_species: dict[str, list[dict]],
    species: list[str],
    observations: Callable[[dict], Iterable[object]],
) -> tuple[float, int, int, int]:
    """Combine independent per-species tables into one named family test."""

    total_chi_square = 0.0
    total_degrees_of_freedom = 0
    tested_species = 0
    pooled_cells = 0
    for name in species:
        if name not in candidate_by_species:
            # Species marginals already catch a wholly absent species.  A
            # conditional distribution is undefined without both samples.
            continue
        empirical_counts = Counter(
            value
            for mon in empirical_by_species[name]
            for value in observations(mon)
        )
        candidate_counts = Counter(
            value
            for mon in candidate_by_species[name]
            for value in observations(mon)
        )
        chi_square, degrees_of_freedom, cell_count, _ = _chi_square_homogeneity(
            empirical_counts, candidate_counts
        )
        if degrees_of_freedom:
            total_chi_square += chi_square
            total_degrees_of_freedom += degrees_of_freedom
            tested_species += 1
            pooled_cells += cell_count
    return (
        total_chi_square,
        total_degrees_of_freedom,
        tested_species,
        pooled_cells,
    )


def _result(
    family: int,
    name: str,
    test: str,
    statistic: float | None,
    degrees_of_freedom: int | None,
    p_value: float,
    threshold: float,
    details: str,
) -> StatisticResult:
    return StatisticResult(
        family=family,
        name=name,
        test=test,
        statistic=statistic,
        degrees_of_freedom=degrees_of_freedom,
        p_value=p_value,
        threshold=threshold,
        verdict="REJECT" if p_value < threshold else "PASS",
        details=details,
    )


def compare(
    empirical: list[list[dict]],
    candidate: list[list[dict]],
    alpha: float = 0.01,
) -> OracleReport:
    """Compare candidate teams with real generator draws.

    The five named families receive ``alpha / 5`` each.  Per-species property
    and move tables are stratified and their χ²/df values summed, yielding one
    p-value per family rather than performing uncorrected species-level tests.
    """

    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be between zero and one")
    threshold = alpha / FAMILY_COUNT
    empirical_by_species = _mons_by_species(empirical)
    candidate_by_species = _mons_by_species(candidate)
    results: list[StatisticResult] = []

    empirical_species = Counter(
        _generator_species(mon) for team in empirical for mon in team
    )
    candidate_species = Counter(
        _generator_species(mon) for team in candidate for mon in team
    )
    chi_square, degrees_of_freedom, cells, raw_cells = _chi_square_homogeneity(
        empirical_species, candidate_species
    )
    p_value = _chi_square_sf(chi_square, degrees_of_freedom)
    results.append(
        _result(
            1,
            "Species marginals",
            "chi2",
            chi_square,
            degrees_of_freedom,
            p_value,
            threshold,
            f"{cells} pooled cells from {raw_cells} species",
        )
    )

    shared_species = sorted(set(empirical_by_species) & set(candidate_by_species))
    level_mismatches = []
    for species in shared_species:
        empirical_levels = {mon["level"] for mon in empirical_by_species[species]}
        candidate_levels = {mon["level"] for mon in candidate_by_species[species]}
        if empirical_levels != candidate_levels:
            level_mismatches.append(
                f"{species}: {sorted(empirical_levels)} != {sorted(candidate_levels)}"
            )
    level_p_value = 0.0 if level_mismatches else 1.0
    results.append(
        _result(
            2,
            "Level per species",
            "exact-set",
            None,
            None,
            level_p_value,
            threshold,
            (
                f"{len(shared_species)} shared species; "
                f"{len(level_mismatches)} mismatches"
                + (
                    f" ({'; '.join(level_mismatches[:3])})"
                    if level_mismatches
                    else ""
                )
            ),
        )
    )

    common_species, cutoff = _common_species(empirical_by_species)
    property_chi_square, property_df, property_species, property_cells = (
        _stratified_chi_square(
            empirical_by_species,
            candidate_by_species,
            common_species,
            lambda mon: [
                (mon["item"], mon["ability"], mon["teraType"])
            ],
        )
    )
    property_p_value = _chi_square_sf(property_chi_square, property_df)
    results.append(
        _result(
            3,
            "Per-species set properties",
            "chi2",
            property_chi_square,
            property_df,
            property_p_value,
            threshold,
            (
                f"{property_species}/{len(common_species)} species informative "
                f"at cutoff {cutoff}; {property_cells} pooled cells"
            ),
        )
    )

    def move_pairs(mon: dict) -> Iterable[tuple[str, str]]:
        return itertools.combinations(sorted(mon["moves"]), 2)

    move_chi_square, move_df, move_species, move_cells = _stratified_chi_square(
        empirical_by_species,
        candidate_by_species,
        common_species,
        move_pairs,
    )
    move_p_value = _chi_square_sf(move_chi_square, move_df)
    results.append(
        _result(
            4,
            "Move co-occurrence",
            "chi2",
            move_chi_square,
            move_df,
            move_p_value,
            threshold,
            (
                f"{move_species}/{len(common_species)} species informative "
                f"at cutoff {cutoff}; {move_cells} pooled cells"
            ),
        )
    )

    def violations(teams: list[list[dict]]) -> tuple[int, int]:
        wrong_size = sum(len(team) != 6 for team in teams)
        duplicate_species = sum(
            len({_generator_species(mon) for mon in team}) != len(team)
            for team in teams
        )
        return wrong_size, duplicate_species

    empirical_wrong_size, empirical_duplicates = violations(empirical)
    candidate_wrong_size, candidate_duplicates = violations(candidate)
    constraint_reject = (
        empirical_wrong_size == 0 < candidate_wrong_size
        or empirical_duplicates == 0 < candidate_duplicates
    )
    results.append(
        _result(
            5,
            "Team-level constraints",
            "exact-zero",
            None,
            None,
            0.0 if constraint_reject else 1.0,
            threshold,
            (
                "size!=6 empirical/candidate="
                f"{empirical_wrong_size}/{candidate_wrong_size}; "
                "duplicate-species empirical/candidate="
                f"{empirical_duplicates}/{candidate_duplicates}"
            ),
        )
    )

    verdict = "REJECT" if any(result.rejected for result in results) else "PASS"
    correction = (
        f"Bonferroni familywise correction: alpha={alpha:g} / "
        f"{FAMILY_COUNT} families = {threshold:g} per family."
    )
    return OracleReport(
        statistics=tuple(results),
        alpha=alpha,
        family_threshold=threshold,
        correction=correction,
        verdict=verdict,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare a team sampler with Showdown generator sidecars."
    )
    parser.add_argument("--empirical-dir", required=True)
    parser.add_argument("--limit-games", type=int)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="compare seeded random halves of the empirical sample",
    )
    parser.add_argument("--seed", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.self_test:
        raise SystemExit("--self-test is required until a candidate sampler exists")
    teams = load_empirical_teams(args.empirical_dir, args.limit_games)
    if len(teams) < 2:
        raise SystemExit("self-test requires at least one loaded team in each half")
    empirical, candidate = split_halves(teams, seed=args.seed)
    report = compare(empirical, candidate)
    print(report.format_table())
    return 0 if report.verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
