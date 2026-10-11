"""Releases: Bamboo deployment events correlated with runtime observations.

A release is one Bamboo deployment of a service to an environment. Its status is derived, not
asserted: Bamboo says that it deployed something, the inventory shows whether every instance of
the service/environment actually runs the target image and is healthy.

  started      Bamboo reported the deployment; no instance runs the target yet.
  rolling-out  at least one instance runs the target (by image digest, else by version/tag).
  succeeded    every instance of the target image repository in the service/environment runs the
               target and is healthy (or completed with exit 0), with at least one instance
               created after the release started, or Bamboo reported success.
  failed       Bamboo reported failure, a new instance crash-loops / was OOM-killed, or the
               rollout did not converge within GMAN_RELEASE_TIMEOUT_MINUTES.
  superseded   a newer release of the same service/environment started before this one finished.

Evaluation happens on ingest (snapshots and Bamboo events, under a per service/environment lock)
and in the periodic housekeeping run after snapshots (timeouts). It is idempotent: replays of the
same snapshot or event are rejected earlier, and re-evaluation of unchanged data changes nothing.
"""
import base64
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from . import db as store

OPEN = ("started", "rolling-out")
STATUSES = ("started", "rolling-out", "succeeded", "failed", "superseded")
RUNTIME_TYPES = ("instance-appeared", "instance-removed", "config-changed", "instance-restarted", "state-changed")
# Runtime changes that a deployment causes; state changes and restarts are linked but not attributed.
ATTRIBUTED = ("instance-appeared", "config-changed")
CRASH_REASONS = ("CrashLoopBackOff", "OOMKilled")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def minutes(name, default):
    return timedelta(minutes=float(os.getenv(name, default)))


def timeout():
    return minutes("GMAN_RELEASE_TIMEOUT_MINUTES", "30")


def lead():
    """Runtime changes this long before Bamboo's "started" still belong to the release (clock skew)."""
    return minutes("GMAN_RELEASE_LEAD_MINUTES", "10")


def crash_restarts():
    return int(os.getenv("GMAN_RELEASE_CRASH_RESTARTS", "3"))


def lock(session, service_id, environment):
    if session.get_bind().dialect.name == "postgresql":
        name = f"release:{service_id}|{environment}"
        lock_id = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": lock_id})


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError:
        # Kubernetes/Docker may send nanoseconds; fromisoformat on older Pythons rejects them.
        match = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)", value)
        if not match:
            return None
        fraction = (match.group(2) or "")[:7]
        tz = match.group(3).replace("Z", "+00:00") or "+00:00"
        parsed = datetime.fromisoformat(match.group(1) + fraction + tz)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- image references -------------------------------------------------------------------------

def digests(*values):
    return {d for v in values if v for d in DIGEST.findall(v.lower())}


def registry_digests(config):
    """Manifest digests the runtime knows: a pinned image or Kubernetes imageID (repo@sha256:...)."""
    return digests(*(v.split("@", 1)[1] for v in (config.get("image", ""), config.get("image_id", "")) if "@" in v))


def repository(image):
    image = (image or "").split("@", 1)[0]
    if not image or image.startswith("sha256:"):
        return ""
    slash = image.rfind("/")
    colon = image.rfind(":")
    return image[:colon] if colon > slash else image


def tag(image):
    image = (image or "").split("@", 1)[0]
    slash, colon = image.rfind("/"), image.rfind(":")
    return image[colon + 1:] if colon > slash else ""


def target_versions(release):
    return {v for v in (release.version, release.release, tag(release.image)) if v and v != "latest"}


def match(instance, release):
    """How an observed instance matches the release target: "digest", "version" or None."""
    if not instance:
        return None
    config = instance.get("config") or {}
    wanted = digests(release.image_digest, release.image)
    if wanted:
        if wanted & digests(config.get("image", ""), config.get("image_id", "")):
            return "digest"
        if registry_digests(config):
            return None  # The runtime knows the exact manifest and it is a different one.
    origin = instance.get("origin") or {}
    seen = {tag(config.get("image", "")), origin.get("image_version", ""), origin.get("app_version", "")}
    return "version" if target_versions(release) & seen else None


# --- instance state ---------------------------------------------------------------------------

def healthy(instance):
    return (instance.get("status") == "running" and instance.get("health", "") in ("", "healthy")
            and instance.get("ready") is not False and not instance.get("reason"))


def completed(instance):
    return instance.get("status") in ("exited", "terminated") and instance.get("exit_code") == 0


def crashed(instance):
    if instance.get("restarts", 0) >= crash_restarts():
        return f"{instance.get('restarts')} перезапусков"
    text_ = " ".join((instance.get("reason") or "", instance.get("last_termination_reason") or ""))
    return next((r for r in CRASH_REASONS if r in text_), None)


def fresh(instance, release):
    created = parse_time(instance.get("created_at")) or parse_time(instance.get("started_at"))
    return created is not None and created >= release.window_start


# --- correlation ------------------------------------------------------------------------------

def current(session, release):
    return session.scalars(select(store.Instance).where(store.Instance.service_id == release.service_id,
                                                        store.Instance.environment == release.environment)
                           .order_by(store.Instance.source_id, store.Instance.position)).all()


def repositories(release, instances):
    repos = {repository(i.get("config", {}).get("image", "")) for i in instances if match(i, release)}
    repos.add(repository(release.image))
    return repos - {""}


def window_end(release):
    return release.finished_at or (release.started_at + timeout())


def link(event, release, method=None):
    data = {**event.data, "release_id": release.id}
    if method and event.type in ATTRIBUTED:
        data.update(actor=release.initiator, actor_kind=release.initiator_kind,
                    actor_source=f"Bamboo, correlated by {method}")
    event.data = data
    event.release_id = release.id


def correlate(release, events, instances):
    """Link runtime events of the release window; returns how many were linked."""
    repos = repositories(release, instances + [e.data.get("after") for e in events if e.data.get("after")])
    start, end = release.window_start, window_end(release)
    linked = 0
    for event in events:
        if event.release_id is not None or event.type not in RUNTIME_TYPES or event.occurred_at is None:
            continue
        if not start <= event.occurred_at <= end:
            continue
        after, before = event.data.get("after"), event.data.get("before")
        observed = after or before
        if not observed or observed.get("environment") != release.environment:
            continue
        if event.type == "instance-removed":
            # The old version going away is part of a rollout, but nothing proves who removed it.
            if repository(before.get("config", {}).get("image", "")) in repos and not match(before, release):
                link(event, release)
                linked += 1
            continue
        method = match(after, release)
        if method:
            link(event, release, method)
            linked += 1
    return linked


def finish(release, status, reason, now):
    release.status, release.status_reason, release.finished_at = status, reason, now


def evaluate(session, release, now):
    """Advance the status from Bamboo's report and the instances observed right now."""
    if release.status == "superseded":
        return
    if release.bamboo_status == "failed":
        if release.status != "failed":
            finish(release, "failed", "Bamboo сообщил об ошибке деплоя", release.bamboo_finished_at or now)
        return
    if release.status not in OPEN:
        return
    rows = [r.data for r in current(session, release)]
    matched = [(i, match(i, release)) for i in rows]
    matched = [(i, m) for i, m in matched if m]
    repos = repositories(release, rows)
    relevant = [i for i in rows if repository(i.get("config", {}).get("image", "")) in repos]
    ok = [i for i, _ in matched if healthy(i) or completed(i)]
    new = [i for i, _ in matched if fresh(i, release)]
    progress = {"total": len(relevant), "on_target": len(matched), "healthy": len(ok), "fresh": len(new)}
    if release.progress != progress:
        release.progress = progress
    if matched:
        method = "digest" if any(m == "digest" for _, m in matched) else "version"
        if release.correlation != method:
            release.correlation = method
        if release.status == "started":
            release.status, release.rollout_started_at = "rolling-out", now
    broken = [(i, crashed(i)) for i in new]
    broken = [(i, why) for i, why in broken if why]
    if broken:
        finish(release, "failed", "Сбой новых экземпляров: " + ", ".join(f"{i['name']} ({why})" for i, why in broken[:5]), now)
        return
    if relevant and len(matched) == len(relevant) and len(ok) == len(matched) and (new or release.bamboo_status == "succeeded"):
        finish(release, "succeeded", "", now)
        return
    if now - release.started_at > timeout():
        if not rows:
            reason = "экземпляры сервиса в этом окружении не наблюдаются"
        elif not matched:
            reason = "ни один экземпляр не запущен с целевым образом"
        else:
            reason = f"на целевой версии {len(matched)} из {len(relevant)}, здоровы {len(ok)}"
        finish(release, "failed", f"Тайм-аут {int(timeout().total_seconds() // 60)} мин: {reason}", now)


def open_releases(session, service_id, environment):
    return session.scalars(select(store.Release).where(store.Release.service_id == service_id,
                                                       store.Release.environment == environment,
                                                       store.Release.status.in_(OPEN))
                           .order_by(store.Release.started_at.desc(), store.Release.id.desc())).all()


def on_snapshot(session, events, pairs, now):
    """After a snapshot was stored (same transaction): link its events and re-evaluate releases."""
    for service_id, environment in sorted(pairs):
        if not service_id:
            continue
        lock(session, service_id, environment)
        releases = open_releases(session, service_id, environment)
        if not releases:
            continue
        instances = [r.data for r in current(session, releases[0])]
        own = [e for e in events if e.service_id == service_id]
        for release in releases:  # newest first: it claims the events of overlapping windows
            correlate(release, own, instances)
        for release in releases:
            evaluate(session, release, now)


def supersede(session, release, now):
    others = [r for r in open_releases(session, release.service_id, release.environment) if r.id != release.id]
    newer = [r for r in others if (r.started_at, r.id) > (release.started_at, release.id)]
    for other in others:
        if other not in newer:
            other.superseded_by = release.id
            finish(other, "superseded", f"Начат релиз #{release.id}", now)
    if newer and release.status in OPEN:
        # A late event of an older deployment: the newer one already replaced it.
        release.superseded_by = newer[-1].id
        finish(release, "superseded", f"Начат релиз #{newer[-1].id}", now)


def on_deployment(session, request, event, received_at):
    """Create or update the release for a Bamboo event (same transaction as the event)."""
    service_id, environment, status = request["service_id"], request["environment"], request["status"]
    lock(session, service_id, environment)
    occurred = parse_time(request.get("occurred_at")) or received_at
    key = request.get("deployment_id") or f"{request['build_id']}|{request.get('release', '')}"
    latest = session.scalars(select(store.Release).where(store.Release.service_id == service_id,
                                                         store.Release.environment == environment,
                                                         store.Release.key == key)
                             .order_by(store.Release.id.desc()).limit(1)).first()
    # Without a deployment ID, a new "started" after a finished run of the same build is a redeploy.
    reuse = latest is not None and (request.get("deployment_id") or latest.bamboo_status in ("", "started")
                                    or status != "started")
    created = not reuse
    if created:
        release = store.Release(service_id=service_id, environment=environment, key=key, status="started",
                                started_at=occurred, progress={}, initiator="unknown", initiator_kind="unknown",
                                status_reason="", bamboo_status="", correlation="",
                                **{f: "" for f in ("release", "version", "build_id", "build_url", "commit",
                                                   "image", "image_digest")},
                                window_start=occurred - (lead() if status == "started" else timeout()))
        session.add(release)
    else:
        release = latest
    if status == "started":
        if created or release.bamboo_status != "started":
            release.started_at, release.window_start = occurred, min(release.window_start, occurred - lead())
        release.bamboo_status = release.bamboo_status or "started"
    else:
        release.bamboo_status, release.bamboo_finished_at = status, occurred
    for field in ("release", "version", "build_id", "build_url", "commit", "image", "image_digest"):
        if request.get(field) and not getattr(release, field):
            setattr(release, field, request[field])
    if not release.version:
        release.version = release.release
    if request.get("actor", "unknown") != "unknown" and release.initiator == "unknown":
        release.initiator, release.initiator_kind = request["actor"], request.get("actor_kind", "unknown")
    session.flush()
    event.release_id = release.id
    event.data = {**event.data, "release_id": release.id}
    if created:
        supersede(session, release, received_at)
    if release.status in OPEN:
        events = session.scalars(select(store.Event).where(
            store.Event.service_id == service_id, store.Event.release_id.is_(None),
            store.Event.type.in_(RUNTIME_TYPES), store.Event.occurred_at >= release.window_start,
            store.Event.occurred_at <= window_end(release)).order_by(store.Event.id)).all()
        correlate(release, events, [r.data for r in current(session, release)])
    evaluate(session, release, received_at)
    return release


def expire(engine, now):
    """Periodic: time out releases whose rollout never converged."""
    with Session(engine) as session:
        stale = session.execute(select(store.Release.service_id, store.Release.environment).where(
            store.Release.status.in_(OPEN), store.Release.started_at < now - timeout()).distinct()).all()
        for service_id, environment in sorted(stale):
            lock(session, service_id, environment)
            for release in open_releases(session, service_id, environment):
                evaluate(session, release, now)
        session.commit()


# --- API views --------------------------------------------------------------------------------

def view(release, now=None):
    end = release.finished_at or now
    return {
        "id": release.id, "service_id": release.service_id, "environment": release.environment,
        "release": release.release, "version": release.version, "build_id": release.build_id,
        "build_url": release.build_url, "commit": release.commit, "image": release.image,
        "image_digest": release.image_digest, "initiator": release.initiator,
        "initiator_kind": release.initiator_kind, "status": release.status,
        "status_reason": release.status_reason, "bamboo_status": release.bamboo_status,
        "correlation": release.correlation, "superseded_by": release.superseded_by,
        "progress": release.progress or {},
        **{k: (getattr(release, k).isoformat() if getattr(release, k) else None)
           for k in ("started_at", "rollout_started_at", "bamboo_finished_at", "finished_at")},
        "duration_seconds": (end - release.started_at).total_seconds() if end else None,
    }


def cursor(row):
    raw = f"{row.started_at.isoformat()}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def listing(session, limit, service_id=None, environment=None, status=None, before=None, now=None):
    query = select(store.Release)
    if service_id is not None:
        query = query.where(store.Release.service_id == service_id)
    if environment is not None:
        query = query.where(store.Release.environment == environment)
    if status is not None:
        query = query.where(store.Release.status == status)
    if before:
        query = query.where(tuple_(store.Release.started_at, store.Release.id) < store.parse_cursor(before))
    rows = session.scalars(query.order_by(store.Release.started_at.desc(), store.Release.id.desc())
                           .limit(limit + 1)).all()
    page = rows[:limit]
    return {"releases": [view(r, now) for r in page], "next": cursor(page[-1]) if len(rows) > limit else None}


def detail(session, release_id, now):
    release = session.get(store.Release, release_id)
    if release is None:
        return None
    events = session.scalars(select(store.Event).where(store.Event.release_id == release.id)
                             .order_by(store.Event.received_at.desc(), store.Event.id.desc()).limit(500)).all()
    rows = current(session, release)
    repos = repositories(release, [r.data for r in rows])
    instances = []
    for row in rows:
        data = row.data
        method = match(data, release)
        if not method and repository(data.get("config", {}).get("image", "")) not in repos:
            continue  # Sidecars and other images of the service are not part of this rollout.
        instances.append({"source_id": row.source_id, "id": data["id"], "name": data["name"],
                          "node": data.get("node", ""), "status": data.get("status", ""),
                          "health": data.get("health", ""), "ready": data.get("ready"),
                          "reason": data.get("reason", ""), "restarts": data.get("restarts", 0),
                          "image": data.get("config", {}).get("image", ""),
                          "image_id": data.get("config", {}).get("image_id", ""),
                          "created_at": data.get("created_at"), "started_at": data.get("started_at"),
                          "on_target": method, "healthy": healthy(data) or completed(data),
                          "new": fresh(data, release), "crash": crashed(data) if method else None})
    return {"release": view(release, now), "events": [store.ordered(e.data) for e in events],
            "instances": instances}
