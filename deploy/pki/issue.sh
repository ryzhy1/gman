#!/usr/bin/env bash
# Pilot PKI for GMan: a private CA, server certificates (portal nginx, OTLP endpoint),
# client certificates for node Collectors (mTLS) and a revocation list. Requires the openssl CLI.
#
#   deploy/pki/issue.sh ca
#   deploy/pki/issue.sh server portal  DNS:gman.example.org IP:10.0.0.10
#   deploy/pki/issue.sh server gateway DNS:gman.example.org IP:10.0.0.10
#   deploy/pki/issue.sh client server-01    Docker host: may send only host.id=server-01
#   deploy/pki/issue.sh cluster cluster-01  Kubernetes OTel DaemonSet: k8s.cluster.name=cluster-01
#   deploy/pki/issue.sh revoke client-server-01 [reason]  revoke <name>.pem (or a path), update crl.pem
#   deploy/pki/issue.sh crl                 re-sign crl.pem before it expires (CRL_DAYS, default 365)
#
# Output goes to $PKI_DIR (default deploy/pki/out, ignored by git): ca.pem / ca-key.pem,
# <name>.pem / <name>-key.pem, client-<host>.pem, cluster-<cluster>.pem (+ -key.pem), crl.pem,
# the CA database index.txt / crlnumber and revoked/. Client subjects are
# /O=GMan pilot/OU=client|cluster/CN=<id>: the OTLP endpoint (deploy/nginx/gman.conf) binds
# host.id (client) or k8s.cluster.name (cluster) to the CN.
# Existing files are never overwritten. Keys are EC P-256, mode 0600.
# The CA key must stay on the administrator's machine; servers only get ca.pem and crl.pem.
set -euo pipefail

PKI_DIR=${PKI_DIR:-"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/out"}
CA_DAYS=${CA_DAYS:-1825}
CERT_DAYS=${CERT_DAYS:-397}
CRL_DAYS=${CRL_DAYS:-365}

usage() {
    sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
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

need_ca() {
    [[ -f $PKI_DIR/ca.pem && -f $PKI_DIR/ca-key.pem ]] || die "no CA in $PKI_DIR; run: issue.sh ca"
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

# Minimal `openssl ca` setup, used only for revocation and CRLs (certificates are signed by `sign`).
# `openssl ca -revoke` adds a certificate missing from index.txt, so certificates issued before
# the database existed can be revoked too.
ca_config() {
    [[ -f $PKI_DIR/index.txt ]] || : >"$PKI_DIR/index.txt"
    [[ -f $PKI_DIR/crlnumber ]] || echo 1000 >"$PKI_DIR/crlnumber"
    cat >"$tmp/ca.cnf" <<EOF
[ca]
default_ca = gman
[gman]
database = $PKI_DIR/index.txt
crlnumber = $PKI_DIR/crlnumber
certificate = $PKI_DIR/ca.pem
private_key = $PKI_DIR/ca-key.pem
default_md = sha256
default_crl_days = $CRL_DAYS
crl_extensions = crl_ext
[crl_ext]
authorityKeyIdentifier = keyid:always
EOF
}

# nginx rejects every client certificate when crl.pem is missing or past its nextUpdate,
# so the file is replaced atomically and only after it verifies.
gencrl() {
    need_ca
    ca_config
    openssl ca -config "$tmp/ca.cnf" -gencrl -out "$tmp/crl.pem" 2>"$tmp/crl.err" || { cat "$tmp/crl.err" >&2; return 1; }
    openssl crl -in "$tmp/crl.pem" -CAfile "$PKI_DIR/ca.pem" -noout 2>/dev/null || die "generated CRL does not verify"
    chmod 0644 "$tmp/crl.pem"
    mv "$tmp/crl.pem" "$PKI_DIR/crl.pem"
    echo "CRL: $PKI_DIR/crl.pem, $(openssl crl -in "$PKI_DIR/crl.pem" -noout -nextupdate)"
    echo "Copy crl.pem to GMAN_TLS_DIR on the server and reload nginx (README, «Отзыв сертификатов»)."
}

# revoke <name|path> [reason]
revoke() {
    local cert=$1 reason=${2:-unspecified} serial base
    need_ca
    [[ -f $cert ]] || cert="$PKI_DIR/$1.pem"
    [[ -f $cert ]] || die "no certificate $1 (a path or a name such as client-server-01)"
    case $reason in
        unspecified | keyCompromise | affiliationChanged | superseded | cessationOfOperation) ;;
        *) die "reason: unspecified, keyCompromise, affiliationChanged, superseded or cessationOfOperation" ;;
    esac
    openssl verify -CAfile "$PKI_DIR/ca.pem" "$cert" >/dev/null 2>&1 || die "$cert is not a valid certificate of this CA"
    ca_config
    openssl ca -config "$tmp/ca.cnf" -revoke "$cert" -crl_reason "$reason" 2>"$tmp/revoke.err" \
        || { cat "$tmp/revoke.err" >&2; return 1; }
    serial=$(openssl x509 -in "$cert" -noout -serial | cut -d= -f2)
    echo "Revoked $cert (serial $serial, $reason)"
    # Move the pair aside, so the same name can be issued again.
    base=${cert%.pem}
    mkdir -p "$PKI_DIR/revoked"
    mv "$cert" "$PKI_DIR/revoked/$(basename "$base")-$serial.pem"
    [[ ! -f $base-key.pem ]] || mv "$base-key.pem" "$PKI_DIR/revoked/$(basename "$base")-$serial-key.pem"
    gencrl
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
    gencrl
}

# leaf <kind> <subject CN> <cert> <key> <extendedKeyUsage> [subjectAltName]
leaf() {
    local cert=$3 key=$4
    need_ca
    refuse_existing "$cert" "$key"
    new_key "$key"
    openssl req -new -key "$key" -out "$tmp/leaf.csr" -subj "/O=GMan pilot/OU=$1/CN=$2"
    {
        echo "basicConstraints = critical, CA:FALSE"
        echo "keyUsage = critical, digitalSignature"
        echo "extendedKeyUsage = $5"
        echo "subjectKeyIdentifier = hash"
        echo "authorityKeyIdentifier = keyid"
        [[ -z ${6:-} ]] || echo "subjectAltName = $6"
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
        # CN is the node's host.id (GMAN_HOST_ID = GMAN_SOURCE_ID): the OTLP endpoint drops any other host.id.
        leaf client "$2" "$PKI_DIR/client-$2.pem" "$PKI_DIR/client-$2-key.pem" clientAuth "DNS:$2"
        ;;
    cluster)
        [[ $# -eq 2 ]] || usage
        valid_name "$2"
        # CN is the cluster source ID (K8S_CLUSTER_NAME in deploy/otel-k8s.yaml). One certificate is shared by
        # the DaemonSet pods, so host.id (the node name) is not bound; k8s.cluster.name is set from the CN.
        leaf cluster "$2" "$PKI_DIR/cluster-$2.pem" "$PKI_DIR/cluster-$2-key.pem" clientAuth
        ;;
    revoke)
        [[ $# -ge 2 && $# -le 3 ]] || usage
        revoke "$2" "${3:-}"
        ;;
    crl)
        [[ $# -eq 1 ]] || usage
        gencrl
        ;;
    *)
        usage
        ;;
esac
