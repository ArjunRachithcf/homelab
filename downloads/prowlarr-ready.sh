#!/bin/bash
# LinuxServer's readiness check for Prowlarr (s6), mounted over the image's: it asks the address Prowlarr listens on,
# gluetun's (PROWLARR__SERVER__BINDADDRESS, docker-compose.yml), where the image's asks localhost. Without it s6
# never sees Prowlarr ready, and a stop ends in SIGKILL.
[[ $(curl -s -m 5 -H 'Host: localhost' "http://${PROWLARR__SERVER__BINDADDRESS:-localhost}:9696/ping" \
     | jq -r .status 2>/dev/null) == OK ]]
