#!/bin/bash

echo "Container's IP address: `awk 'END{print $1}' /etc/hosts`"

function run_telegramer () {
  python manage.py run_telegramer &
}
function run_binancer () {
  python manage.py run_binancer
}

run_telegramer
run_binancer