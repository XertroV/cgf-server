#!/usr/bin/env bash
export CFG_LOCAL_DEV=${CFG_LOCAL_DEV:-true}
find cgf | entr -r poetry run python main.py
