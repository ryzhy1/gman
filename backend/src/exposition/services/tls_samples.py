"""Выборки сроков TLS-файлов (алерты GManTls*): notAfter сертификатов, nextUpdate CRL и ошибки чтения."""
from exposition.entity import MetricSample
from exposition.services.tls_watch import TlsWatch


class TlsSamples:
    """Выборки по каждому наблюдаемому файлу."""

    def __init__(self, watch: TlsWatch) -> None:
        """Запоминает наблюдение за файлами."""
        self._watch = watch

    def samples(self) -> list[MetricSample]:
        """Ошибка чтения и сроки каждого файла; по одному ряду на субъект или издателя."""
        samples = []
        for path in self._watch.paths:
            contents = self._watch.contents(path)
            error = MetricSample(name="gman_tls_file_error", labels={"file": path}, value=0 if contents else 1)
            samples.append(error)
            if contents is None:
                continue
            for subject, not_after in _earliest(contents.certificates):
                labels = {"file": path, "subject": subject}
                certificate = MetricSample("gman_tls_cert_not_after_timestamp_seconds", labels, not_after)
                samples.append(certificate)
            for issuer, next_update in _earliest(contents.crls):
                labels = {"file": path, "issuer": issuer}
                crl = MetricSample("gman_tls_crl_next_update_timestamp_seconds", labels, next_update)
                samples.append(crl)
        return samples


def _earliest(entries: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """По одному сроку на имя — самый ранний (связка может повторять субъект), упорядочено по имени."""
    earliest: dict[str, float] = {}
    for name, moment in entries:
        if name not in earliest or moment < earliest[name]:
            earliest[name] = moment
    ordered = sorted(earliest.items())
    return ordered
