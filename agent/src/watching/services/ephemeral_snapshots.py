"""Снимки с короткоживущими экземплярами, которых к моменту снимка уже нет."""
from datetime import timedelta

from watching.entity import JsonObject, SnapshotDocument

INTERIM_SHIFT = timedelta(milliseconds=1)


def with_ephemeral(snapshot: SnapshotDocument, seen: dict[str, JsonObject]) -> list[SnapshotDocument]:
    """Снимки для отправки по одному сбору.

    Экземпляры, увиденные в событиях, но уже отсутствующие (удалены, например --rm), идут в промежуточный снимок
    с последним известным состоянием, а через 1 мс за ним — настоящий: API записывает и их появление/выход,
    и удаление.
    """
    present = snapshot.instance_ids
    gone = [instance for instance_id, instance in seen.items() if instance_id not in present]
    if not gone:
        return [snapshot]
    snapshots = [snapshot.with_instances(gone), snapshot.shifted(INTERIM_SHIFT)]
    return snapshots
