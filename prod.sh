#!/usr/bin/env bash

while sleep 1; do
  ./run.sh 2>&1 | tee -a cgf-log-$(date +%s).txt
done
