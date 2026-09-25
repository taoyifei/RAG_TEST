#!/bin/sh
set -eu

REDISCLI_AUTH="$(cat /run/secrets/redis_password)" redis-cli ping | grep -qx PONG
