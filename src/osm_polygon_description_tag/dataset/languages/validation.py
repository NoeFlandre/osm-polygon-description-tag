"""Read-only completeness validation for a local language run.

Validation never writes, repairs, or deletes anything: it reports what is on
disk so an operator can decide. Every committed part is checked against its
receipt hash, its declared row count, and the frozen annotation schema, and any
artifact the checkpoints do not account for is reported rather than ignored.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from osm_polygon_description_tag.dataset.languages.annotations import (
    AnnotationError,
    read_annotation_part,
    validate_annotation_table,
)
from osm_polygon_description_tag.dataset.languages.checkpoint import (
    CheckpointError,
    PartReceipt,
    ShardCheckpoint,
    ShardPaths,
    ShardStatus,
    read_checkpoint,
    read_receipt,
    receipt_name_for_part,
    shard_paths,
    validate_receipt_chain,
)
from osm_polygon_description_tag.dataset.languages.snapshot import (
    SnapshotManifest,
    SourceFileSnapshot,
    read_snapshot,
)
from osm_polygon_description_tag.dataset.manifest import file_sha256


@dataclass(frozen=True, slots=True)
class ShardReport:
    """What validation observed for one shard, without changing anything."""

    shard: str
    status: str
    input_row_count: int
    input_cursor: int
    annotation_count: int
    part_count: int
    issues: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Return whether the shard is complete and free of issues."""
        return self.status == str(ShardStatus.COMPLETE) and not self.issues

    def to_payload(self) -> dict[str, object]:
        return {
            "shard": self.shard,
            "status": self.status,
            "input_row_count": self.input_row_count,
            "input_cursor": self.input_cursor,
            "annotation_count": self.annotation_count,
            "part_count": self.part_count,
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class RunReport:
    """Aggregate completeness of one run directory."""

    snapshot_id: str
    model_config_fingerprint: str
    shard_count: int
    complete_shard_count: int
    annotation_count: int
    input_row_count: int
    shards: tuple[ShardReport, ...]
    issues: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Return whether every shard is complete and nothing is unaccounted for."""
        return (
            self.shard_count == self.complete_shard_count
            and not self.issues
            and all(not shard.issues for shard in self.shards)
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "model_config_fingerprint": self.model_config_fingerprint,
            "shard_count": self.shard_count,
            "complete_shard_count": self.complete_shard_count,
            "annotation_count": self.annotation_count,
            "input_row_count": self.input_row_count,
            "complete": self.is_complete,
            "shards": [shard.to_payload() for shard in self.shards],
            "issues": list(self.issues),
        }


def _missing_shard_report(shard: str, expected_rows: int) -> ShardReport:
    return ShardReport(
        shard=shard,
        status="missing",
        input_row_count=expected_rows,
        input_cursor=0,
        annotation_count=0,
        part_count=0,
        issues=("no checkpoint has been written for this shard",),
    )


def _identity_issues(
    checkpoint: ShardCheckpoint,
    snapshot: SnapshotManifest,
    shard: str,
    expected_rows: int,
) -> list[str]:
    issues: list[str] = []
    if checkpoint.snapshot_id != snapshot.snapshot_id:
        issues.append("checkpoint snapshot identity does not match the run snapshot")
    if checkpoint.model_config_fingerprint != snapshot.model_config_fingerprint:
        issues.append("checkpoint detector configuration does not match the run snapshot")
    if checkpoint.shard != shard:
        issues.append(f"checkpoint records a different shard: {checkpoint.shard}")
    if checkpoint.input_row_count != expected_rows:
        issues.append("checkpoint input row count does not match the snapshot source file")
    return issues


def _part_issues(
    paths: ShardPaths,
    checkpoint: ShardCheckpoint,
    expected: SourceFileSnapshot,
    seen_identities: set[str],
) -> tuple[list[str], int]:
    issues, counted, receipts = _validate_parts(paths, checkpoint, expected, seen_identities)
    if len(receipts) == len(checkpoint.completed_parts):
        issues.extend(_receipt_chain_issues(checkpoint, receipts, expected))
    if not issues and counted != checkpoint.annotation_count:
        issues.append(
            f"committed parts hold {counted} annotations "
            f"but the checkpoint records {checkpoint.annotation_count}"
        )
    return issues, counted


def _validate_parts(
    paths: ShardPaths,
    checkpoint: ShardCheckpoint,
    expected: SourceFileSnapshot,
    seen_identities: set[str],
) -> tuple[list[str], int, list[PartReceipt]]:
    issues: list[str] = []
    counted = 0
    receipts: list[PartReceipt] = []
    for part_name in checkpoint.completed_parts:
        rows, problems, receipt = _validate_part(
            paths, part_name, checkpoint, expected, seen_identities
        )
        counted += rows
        issues.extend(problems)
        if receipt is not None:
            receipts.append(receipt)
    return issues, counted, receipts


def _receipt_chain_issues(
    checkpoint: ShardCheckpoint,
    receipts: list[PartReceipt],
    expected: SourceFileSnapshot,
) -> list[str]:
    try:
        validate_receipt_chain(
            checkpoint,
            iter(receipts),
            source_sha256=expected.sha256,
            source_schema_fingerprint=expected.schema_fingerprint,
        )
    except CheckpointError as error:
        return [str(error)]
    return []


def _receipt_binding_issue(
    receipt: PartReceipt,
    part_name: str,
    checkpoint: ShardCheckpoint,
    expected: SourceFileSnapshot,
) -> str | None:
    checks = (
        (
            receipt.snapshot_id != checkpoint.snapshot_id,
            f"receipt belongs to another run: {part_name}",
        ),
        (
            receipt.model_config_fingerprint != checkpoint.model_config_fingerprint,
            f"receipt model does not match checkpoint: {part_name}",
        ),
        (
            receipt.shard != checkpoint.shard,
            f"receipt shard does not match checkpoint: {part_name}",
        ),
        (
            receipt.part_name != part_name,
            f"receipt part name does not match checkpoint: {part_name}",
        ),
        (
            receipt.source_sha256 != expected.sha256,
            f"receipt source does not match snapshot: {part_name}",
        ),
        (
            receipt.source_schema_fingerprint != expected.schema_fingerprint,
            f"receipt source schema does not match snapshot: {part_name}",
        ),
    )
    return next((message for mismatch, message in checks if mismatch), None)


def _matched_receipt(
    paths: ShardPaths,
    part_name: str,
    part_path: Path,
    checkpoint: ShardCheckpoint,
    expected: SourceFileSnapshot,
) -> tuple[PartReceipt | None, list[str]]:
    receipt_path = paths.receipt(part_name)
    receipt, issues = _read_receipt(receipt_path, part_name)
    if receipt is None:
        return None, issues
    binding_issue = _receipt_binding_issue(receipt, part_name, checkpoint, expected)
    if binding_issue is not None:
        return None, [binding_issue]
    if receipt.part_sha256 != file_sha256(part_path):
        return None, [f"committed part does not match its receipt hash: {part_name}"]
    return receipt, []


def _read_receipt(path: Path, part_name: str) -> tuple[PartReceipt | None, list[str]]:
    if path.is_symlink() or not path.is_file():
        return None, [f"receipt is unusable for {part_name}: not a regular file"]
    try:
        receipt = read_receipt(path)
    except CheckpointError as error:
        return None, [f"receipt is unusable for {part_name}: {error}"]
    return receipt, []


def _counted_rows(
    part_path: Path,
    part_name: str,
    receipt: PartReceipt,
    seen_identities: set[str],
) -> tuple[int, list[str]]:
    try:
        table = read_annotation_part(part_path)
        identities = validate_annotation_table(
            table,
            snapshot_id=receipt.snapshot_id,
            model_config_fingerprint=receipt.model_config_fingerprint,
            seen_identities=seen_identities,
            merge_seen=False,
        )
    except AnnotationError as error:
        return 0, [f"committed part is unreadable: {part_name}: {error}"]
    if table.num_rows != receipt.row_count:
        return 0, [f"committed part row count does not match its receipt: {part_name}"]
    seen_identities.update(identities)
    return table.num_rows, []


def _validate_part(
    paths: ShardPaths,
    part_name: str,
    checkpoint: ShardCheckpoint,
    expected: SourceFileSnapshot,
    seen_identities: set[str],
) -> tuple[int, list[str], PartReceipt | None]:
    part_path = paths.part(part_name)
    if part_path.is_symlink():
        return 0, [f"committed part is not a regular file: {part_name}"], None
    if not part_path.is_file():
        return 0, [f"committed part is missing: {part_name}"], None
    receipt, issues = _matched_receipt(paths, part_name, part_path, checkpoint, expected)
    if receipt is None:
        return 0, issues, None
    rows, row_issues = _counted_rows(part_path, part_name, receipt, seen_identities)
    return rows, row_issues, receipt


def _unlisted(directory: Path, expected: set[str], kind: str) -> list[str]:
    return [
        f"unexpected {kind} file not recorded by the checkpoint: {path.name}"
        for path in _sorted_files(directory)
        if path.name not in expected
    ]


def _unexpected_artifacts(paths: ShardPaths, checkpoint: ShardCheckpoint) -> list[str]:
    expected_parts = set(checkpoint.completed_parts)
    expected_receipts = {receipt_name_for_part(name) for name in expected_parts}
    return _unlisted(paths.parts, expected_parts, "part") + _unlisted(
        paths.receipts, expected_receipts, "receipt"
    )


def _sorted_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file() or path.is_symlink())


def _shard_report(
    run_dir: Path,
    snapshot: SnapshotManifest,
    shard: str,
    seen_identities: set[str],
) -> ShardReport:
    expected_rows = snapshot.source_file(shard).row_count
    paths = shard_paths(run_dir, shard)
    if not paths.checkpoint.exists() and not paths.checkpoint.is_symlink():
        return _missing_shard_report(shard, expected_rows)
    try:
        checkpoint = read_checkpoint(paths.checkpoint)
    except CheckpointError as error:
        return ShardReport(shard, "unreadable", expected_rows, 0, 0, 0, (str(error),))
    issues = _identity_issues(checkpoint, snapshot, shard, expected_rows)
    part_problems, counted = _part_issues(
        paths, checkpoint, snapshot.source_file(shard), seen_identities
    )
    issues.extend(part_problems)
    issues.extend(_unexpected_artifacts(paths, checkpoint))
    return ShardReport(
        shard=shard,
        status=str(checkpoint.status),
        input_row_count=checkpoint.input_row_count,
        input_cursor=checkpoint.input_cursor,
        annotation_count=counted,
        part_count=len(checkpoint.completed_parts),
        issues=tuple(issues),
    )


def _expected_shard_directories(run_dir: Path, snapshot: SnapshotManifest) -> set[str]:
    return {shard_paths(run_dir, item.relative_path).root.name for item in snapshot.source_files}


def _is_unexpected_directory(path: Path, expected: set[str]) -> bool:
    return path.is_dir() and path.name not in expected


def _run_issues(run_dir: Path, snapshot: SnapshotManifest) -> list[str]:
    shards_root = run_dir / "shards"
    if not shards_root.is_dir():
        return []
    expected = _expected_shard_directories(run_dir, snapshot)
    return [
        f"unexpected shard directory not described by the snapshot: {path.name}"
        for path in sorted(shards_root.iterdir())
        if _is_unexpected_directory(path, expected)
    ]


def _selected_shards(snapshot: SnapshotManifest, shards: Iterable[str] | None) -> tuple[str, ...]:
    if shards is None:
        return tuple(item.relative_path for item in snapshot.source_files)
    return tuple(snapshot.source_file(shard).relative_path for shard in shards)


def _completed(reports: tuple[ShardReport, ...]) -> int:
    return sum(1 for report in reports if report.is_complete)


def validate_run(run_dir: Path, *, shards: Iterable[str] | None = None) -> RunReport:
    """Report the completeness of ``run_dir`` without modifying it.

    Passing ``shards`` restricts the report to those snapshot-listed shards,
    which is how a single staged worker checks only what it produced.
    """
    snapshot = read_snapshot(run_dir)
    selected = _selected_shards(snapshot, shards)
    seen_identities: set[str] = set()
    reports = tuple(_shard_report(run_dir, snapshot, shard, seen_identities) for shard in selected)
    return RunReport(
        snapshot_id=snapshot.snapshot_id,
        model_config_fingerprint=snapshot.model_config_fingerprint,
        shard_count=len(reports),
        complete_shard_count=_completed(reports),
        annotation_count=sum(report.annotation_count for report in reports),
        input_row_count=sum(report.input_row_count for report in reports),
        shards=reports,
        issues=tuple(_run_issues(run_dir, snapshot)),
    )


__all__ = ["RunReport", "ShardReport", "validate_run"]
