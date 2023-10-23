#!/bin/bash
set -e

echo Setting up for vespa

mkdir -p $(dirname $VESPA_KEY_LOCATION)
echo ${VESPA_PRIVATE_KEY} > ${VESPA_KEY_LOCATION}

mkdir -p $(dirname $VESPA_PUBLIC_CERT_LOCATION)
echo ${VESPA_PUBLIC_CERT} > ${VESPA_PUBLIC_CERT_LOCATION}

python -m cli.index_data --s3 "${INDEXER_INPUT_PREFIX}" --index-type vespa
