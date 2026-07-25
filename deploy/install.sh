#!/usr/bin/env bash
# Thin wrapper - all the logic lives in manage.sh.
#   bash deploy/install.sh    == botctl install
exec bash "$(cd "$(dirname "$0")" && pwd)/manage.sh" install
