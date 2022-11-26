#!/usr/bin/env bash
find cgf | entr -r poetry run python main.py
