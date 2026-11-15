#!/usr/bin/env bash
set -euo pipefail

# Deselect the roots behind the compromised eMudhra intermediates (October
# 2026) in a Debian or Ubuntu system store, then rebuild it. The intermediates
# chain to emSign ECC Root CA - G3 and, through Sectigo cross-signs, to AAA
# Certificate Services (Comodo_AAA_Services_root); Mozilla has also dropped
# emSign ECC Root CA - C3 and emSign Root CA - C1. Upgrading ca-certificates is
# not enough: Debian 13 and Ubuntu 24.04 still ship C3 and C1, and Ubuntu
# 20.04 ships all four. A `!` entry survives later upgrades, and a root the
# package has already dropped has no entry to mark.
#
# Run as root after ca-certificates is installed or upgraded.

ROOTS="emSign_ECC_Root_CA_-_G3 emSign_ECC_Root_CA_-_C3 emSign_Root_CA_-_C1 Comodo_AAA_Services_root"

for root in $ROOTS; do
    sed -i "s|^mozilla/$root\.crt$|!&|" /etc/ca-certificates.conf
done
update-ca-certificates --fresh > /dev/null
for root in $ROOTS; do
    test ! -e "/etc/ssl/certs/$root.pem"
done
