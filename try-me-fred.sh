#!/bin/bash

set -e

if [ -f ../SLFO ]; then
	echo "This script requires that you have a copy of SLFO.git in ../SLFO"
	exit 1
fi

# This downloads the latest rpmhdrs from OBS for each repository/arch combination,
# and builds a solver file from it
./monkey --log download.log download

./monkey --log prepare.log prep

./monkey --log label.log label

# right now, calling it with ignore-errors because of an issue in patterns-base-base
./monkey compose --ignore-errors

# If you have a checkout of products/SLES, we can compare our results with what the Release Managers are currently doing
if [ -d ../SLES ]; then
	./monkey cdiff ../SLES/000productcompose/default.productcompose
fi
