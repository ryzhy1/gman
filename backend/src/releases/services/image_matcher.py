"""Сопоставление наблюдаемых экземпляров с целью релиза по образу: digest манифеста, иначе версия/тег."""
import re

from releases.entity import Release, RolloutInstance

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DIGEST_PREFIX = "sha256:"
DIGEST_SEPARATOR = "@"
TAG_SEPARATOR = ":"
PATH_SEPARATOR = "/"
LATEST_TAG = "latest"
BY_DIGEST = "digest"
BY_VERSION = "version"
NOT_FOUND = -1


class ImageMatcher:
    """Решает, работает ли экземпляр на целевом образе релиза, и какие репозитории образов относятся к раскатке."""

    def match(self, instance: RolloutInstance | None, release: Release) -> str | None:
        """Как экземпляр совпал с целью: "digest", "version" или None."""
        if instance is None:
            return None
        wanted = _digests(release.image_digest, release.image)
        if wanted:
            if wanted & _digests(instance.image, instance.image_id):
                return BY_DIGEST
            if _registry_digests(instance):
                # Runtime знает точный манифест, и он другой.
                return None
        seen = {image_tag(instance.image), instance.image_version, instance.app_version}
        method = BY_VERSION if _target_versions(release) & seen else None
        return method

    def repositories(self, release: Release, instances: list[RolloutInstance]) -> set[str]:
        """Репозитории образов раскатки: цели релиза и экземпляров, совпавших с целью."""
        repositories = {image_repository(release.image)}
        for instance in instances:
            if self.match(instance, release):
                repositories.add(image_repository(instance.image))
        repositories.discard("")
        return repositories


def image_repository(image: str) -> str:
    """Репозиторий образа без тега и digest; у ссылки только из digest репозитория нет."""
    reference = (image or "").split(DIGEST_SEPARATOR, 1)[0]
    if not reference or reference.startswith(DIGEST_PREFIX):
        return ""
    slash, colon = reference.rfind(PATH_SEPARATOR), reference.rfind(TAG_SEPARATOR)
    repository = reference[:colon] if colon > slash else reference
    return repository


def image_tag(image: str) -> str:
    """Тег образа или пусто."""
    reference = (image or "").split(DIGEST_SEPARATOR, 1)[0]
    slash, colon = reference.rfind(PATH_SEPARATOR), reference.rfind(TAG_SEPARATOR)
    tag = reference[colon + 1:] if colon > slash else ""
    return tag


def _digests(*values: str) -> set[str]:
    """Все digest манифестов, упомянутые в значениях."""
    found = set()
    for value in values:
        if value:
            found.update(DIGEST.findall(value.lower()))
    return found


def _registry_digests(instance: RolloutInstance) -> set[str]:
    """Digest манифеста, известные runtime: закрепленный образ или imageID Kubernetes (repo@sha256:...)."""
    pinned = []
    for value in (instance.image, instance.image_id):
        if value.find(DIGEST_SEPARATOR) != NOT_FOUND:
            pinned.append(value.split(DIGEST_SEPARATOR, 1)[1])
    digests = _digests(*pinned)
    return digests


def _target_versions(release: Release) -> set[str]:
    """Версии цели релиза: версия, релиз и тег образа (кроме latest)."""
    candidates = (release.version, release.release, image_tag(release.image))
    versions = {value for value in candidates if value and value != LATEST_TAG}
    return versions
