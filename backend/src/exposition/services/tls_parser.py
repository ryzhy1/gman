"""Разбор TLS-файлов: notAfter сертификатов и nextUpdate CRL из PEM (в том числе связок) или DER."""
from cryptography import x509

from exposition.entity import TlsContents

PEM_MARKER = b"-----BEGIN"
PEM_CERTIFICATE = b"-----BEGIN CERTIFICATE-----"
PEM_CRL_BEGIN = b"-----BEGIN X509 CRL-----"
PEM_CRL_END = b"-----END X509 CRL-----"
NOT_FOUND = -1


class TlsParser:
    """Извлекает сроки действия из содержимого файла; нечитаемое содержимое — ValueError."""

    def parse(self, data: bytes) -> TlsContents:
        """Сертификаты и CRL файла."""
        if PEM_MARKER in data:
            certificates, crls = _pem_certificates(data), _pem_crls(data)
        else:
            certificates, crls = _der_contents(data)
        if not certificates and not crls:
            raise ValueError("no certificate or CRL")
        cert_times = [(cert.subject.rfc4514_string(), cert.not_valid_after_utc.timestamp()) for cert in certificates]
        crl_times = [(crl.issuer.rfc4514_string(), crl.next_update_utc.timestamp()) for crl in crls
                     if crl.next_update_utc]
        contents = TlsContents(certificates=cert_times, crls=crl_times)
        return contents


def _pem_certificates(data: bytes) -> list[x509.Certificate]:
    """Все сертификаты PEM-файла (связка допускается)."""
    if PEM_CERTIFICATE not in data:
        return []
    certificates = x509.load_pem_x509_certificates(data)
    return certificates


def _pem_crls(data: bytes) -> list[x509.CertificateRevocationList]:
    """Все CRL PEM-файла: библиотека читает по одному, поэтому блоки выделяются вручную."""
    crls = []
    start = data.find(PEM_CRL_BEGIN)
    while start != NOT_FOUND:
        end = data.index(PEM_CRL_END, start) + len(PEM_CRL_END)
        crl = x509.load_pem_x509_crl(data[start:end])
        crls.append(crl)
        start = data.find(PEM_CRL_BEGIN, end)
    return crls


def _der_contents(data: bytes) -> tuple[list[x509.Certificate], list[x509.CertificateRevocationList]]:
    """DER-файл: сертификат, иначе CRL."""
    try:
        certificate = x509.load_der_x509_certificate(data)
    except ValueError:
        crl = x509.load_der_x509_crl(data)
        contents = ([], [crl])
        return contents
    contents = ([certificate], [])
    return contents
