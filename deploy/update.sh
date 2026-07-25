#!/usr/bin/env bash
# Thin wrapper - all the logic lives in manage.sh.
#   bash deploy/update.sh    == botctl update
exec bash "$(cd "$(dirname "$0")" && pwd)/manage.sh" update
