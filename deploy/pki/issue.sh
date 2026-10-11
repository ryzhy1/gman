#!/usr/bin/env bash
# Pilot PKI for GMan: a private CA, server certificates (portal nginx, OTLP gateway)
# and client certificates for node Collectors (mTLS). Requires the openssl CLI.
#
#   deploy/pki/issue.sh ca
#   deploy/pki/issue.sh server portal  DNS:gman.example.org IP:10.0.0.10
#   deploy/pki/issue.sh server gateway DNS:gman.example.org IP:10.0.0.10
#   deploy/pki/issue.sh client server-01
#
# Output goes to $PKI_DIR (default deploy/pki/out, ignored by git):
#   ca.pem / ca-key.pem, <name>.pem / <name>-key.pem, client-<name>.pem / client-<name>-key.pem.
# Existing files are never overwritten. Keys are EC P-256, mode 0600.
# The CA key must stay on the administrator's machine; servers only get ca.pem.
set -euo pipefail

PKI_DIR=${PKI_DIR:-"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/out"}
CA_DAYS=${CA_DAYS:-1825}
CERT_DAYS=${CERT_DAYS:-397}

usage() {
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

die() { echo "issue.sh: $*" >&2; exit 1; }

valid_name() {
    [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "invalid name: $1"
}

refuse_existing() {
    local file
    for file in "$@"; do
        [[ ! -e $file ]] || die "$file already exists; remove it explicitly to reissue"
    done
}

new_key() {
    (umask 077 && openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$1")
}

# sign <csr> <cert> <extfile>
sign() {
    openssl x509 -req -in "$1" -out "$2" -days "$CERT_DAYS" -sha256 \
        -CA "$PKI_DIR/ca.pem" -CAkey "$PKI_DIR/ca-key.pem" -CAcreateserial \
        -CAserial "$PKI_DIR/ca.srl" -extfile "$3" 2>"$tmp/sign.err" || { cat "$tmp/sign.err" >&2; return 1; }
}

issue_ca() {
    refuse_existing "$PKI_DIR/ca.pem" "$PKI_DIR/ca-key.pem"
    local ext="$tmp/ca.ext"
    cat >"$ext" <<'EOF'
[req]
distinguished_name = dn
[dn]
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF
    new_key "$PKI_DIR/ca-key.pem"
    openssl req -new -x509 -key "$PKI_DIR/ca-key.pem" -out "$PKI_DIR/ca.pem" -days "$CA_DAYS" -sha256 \
        -subj "/O=GMan pilot/CN=GMan pilot CA" -config "$ext" -extensions v3_ca
    echo "CA: $PKI_DIR/ca.pem (key $PKI_DIR/ca-key.pem — keep offline)"
}

# leaf <kind> <subject CN> <cert> <key> <extendedKeyUsage> <subjectAltName>
leaf() {
    local cert=$3 key=$4
    [[ -f $PKI_DIR/ca.pem && -f $PKI_DIR/ca-key.pem ]] || die "no CA in $PKI_DIR; run: issue.sh ca"
    refuse_existing "$cert" "$key"
    new_key "$key"
    openssl req -new -key "$key" -out "$tmp/leaf.csr" -subj "/O=GMan pilot/OU=$1/CN=$2"
    {
        echo "basicConstraints = critical, CA:FALSE"
        echo "keyUsage = critical, digitalSignature"
        echo "extendedKeyUsage = $5"
        echo "subjectKeyIdentifier = hash"
        echo "authorityKeyIdentifier = keyid"
        echo "subjectAltName = $6"
    } >"$tmp/leaf.ext"
    sign "$tmp/leaf.csr" "$cert" "$tmp/leaf.ext"
    openssl verify -CAfile "$PKI_DIR/ca.pem" "$cert" >/dev/null
    echo "Issued $cert (key $key), valid $CERT_DAYS days"
}

[[ $# -ge 1 ]] || usage
mkdir -p "$PKI_DIR"
chmod 700 "$PKI_DIR"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

case $1 in
    ca)
        [[ $# -eq 1 ]] || usage
        issue_ca
        ;;
    server)
        [[ $# -ge 3 ]] || usage
        name=$2
        valid_name "$name"
        shift 2
        sans=()
        for san in "$@"; do
            [[ $san =~ ^(DNS:[A-Za-z0-9*.-]+|IP:[0-9A-Fa-f.:]+)$ ]] || die "SAN must be DNS:<name> or IP:<addr>: $san"
            sans+=("$san")
        done
        cn=${sans[0]#*:}
        leaf server "$cn" "$PKI_DIR/$name.pem" "$PKI_DIR/$name-key.pem" serverAuth "$(IFS=,; echo "${sans[*]}")"
        ;;
    client)
        [[ $# -eq 2 ]] || usage
        valid_name "$2"
        # CN is the node's source id (GMAN_HOST_ID) for audit; the gateway checks only the CA.
        leaf client "$2" "$PKI_DIR/client-$2.pem" "$PKI_DIR/client-$2-key.pem" clientAuth "DNS:$2"
        ;;
    *)
        usage
        ;;
esac
