#!/bin/sh
# Runs the template engine / loader / compiler unit tests.
# Usage: sh run_template_tests.sh
set -e
cd "$(dirname "$0")"

exec python3 -m unittest \
    tests.templates.engine \
    tests.templates.loader \
    tests.templates.cython_compiler \
    tests.templates.render \
    tests.templates.exceptions \
    tests.templates.nodes \
    -v
